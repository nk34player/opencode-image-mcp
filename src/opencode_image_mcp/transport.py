"""HTTP transport: retries, backoff, timeouts and async job polling.

Every provider call goes through :class:`Transport`, which owns a single
thread-safe ``httpx.Client``. It retries transient failures and can wait for
providers that answer with a job id instead of an image.
"""

from __future__ import annotations

import datetime
import email.utils
import logging
import random
import time
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .config import PollConfig, ProviderConfig, Settings
from .errors import ImageMCPError, JobFailedError, friendly_http_error

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

JOB_ID_FIELDS = ("request_id", "id", "task_id", "job_id", "generation_id", "operation_id", "prediction_id")
STATUS_FIELDS = ("status", "state")


def _first(payload: Mapping[str, Any], *fields: str) -> Any:
    for field_name in fields:
        if field_name in payload and payload[field_name] is not None:
            return payload[field_name]
    return None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


class Transport:
    """Thin, retrying wrapper around ``httpx.Client``."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_transport: httpx.BaseTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger("opencode-image-mcp")
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_s, connect=settings.connect_timeout_s),
            transport=http_transport,
            follow_redirects=True,
        )

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- headers -----------------------------------------------------------
    def headers_for(
        self,
        provider: ProviderConfig,
        *,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        key = provider.api_key_value()
        if key:
            headers[provider.auth_header] = f"{provider.auth_scheme} {key}".strip() if provider.auth_scheme else key
        headers.update(provider.extra_headers)
        if extra:
            headers.update(extra)
        return headers

    # -- requests ----------------------------------------------------------
    def send(
        self,
        method: str,
        url: str,
        *,
        provider: ProviderConfig,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        files: Any = None,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> httpx.Response:
        merged_params = dict(provider.extra_query)
        if params:
            merged_params.update(params)
        request_headers = self.headers_for(provider, extra=headers)
        request_timeout = timeout_s if timeout_s else None

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.request(
                    method,
                    url,
                    json=json,
                    params=merged_params or None,
                    files=files,
                    data=data,
                    headers=request_headers,
                    timeout=request_timeout,
                )
            except httpx.HTTPError as exc:
                if attempt <= self.settings.retries:
                    delay = self._backoff_delay(attempt)
                    self.log.warning(
                        "network error calling %s (%s/%s), retrying in %.1fs",
                        provider.id, method, url, delay,
                    )
                    time.sleep(delay)
                    continue
                raise ImageMCPError(
                    f"{provider.display_name}: network error calling {method} {url}: {exc}",
                    provider=provider.id,
                ) from exc

            if response.status_code in RETRY_STATUS and attempt <= self.settings.retries:
                delay = _retry_after_seconds(response) or self._backoff_delay(attempt)
                self.log.warning(
                    "%s returned HTTP %s, retrying in %.1fs (attempt %s/%s)",
                    provider.id, response.status_code, delay, attempt, self.settings.retries,
                )
                time.sleep(delay)
                continue

            return response

    def _backoff_delay(self, attempt: int) -> float:
        base = self.settings.backoff_base_s * (2 ** (attempt - 1))
        capped = min(base, self.settings.backoff_max_s)
        return capped * (0.7 + random.random() * 0.6)

    def post_json(self, provider: ProviderConfig, path: str, body: Any, **kwargs: Any) -> httpx.Response:
        url = self.provider_url(provider, path)
        self.log.info("POST %s (provider=%s)", url, provider.id)
        return self.send("POST", url, provider=provider, json=body, **kwargs)

    def get(self, provider: ProviderConfig, path: str, **kwargs: Any) -> httpx.Response:
        url = self.provider_url(provider, path)
        self.log.debug("GET %s (provider=%s)", url, provider.id)
        return self.send("GET", url, provider=provider, **kwargs)

    @staticmethod
    def provider_url(provider: ProviderConfig, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        base = provider.base_url.rstrip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        return f"{base}{suffix}"

    # -- downloads ---------------------------------------------------------
    def download(self, url: str, provider: ProviderConfig | None = None) -> tuple[bytes, str | None]:
        """Fetch an image hosted at ``url``, sending auth only to the provider host."""
        headers: dict[str, str] = {}
        if provider is not None:
            same_host = urlparse(url).netloc == urlparse(provider.base_url).netloc
            if same_host:
                headers = self.headers_for(provider)
        try:
            response = self._client.get(url, headers=headers or None)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ImageMCPError(f"Failed to download generated image from {url}: {exc}") from exc
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip() or None
        return response.content, content_type

    # -- async jobs --------------------------------------------------------
    def poll_job(
        self,
        provider: ProviderConfig,
        payload: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Poll a provider job until it finishes, then return the result payload."""
        poll: PollConfig | None = provider.poll
        if poll is None:
            return dict(payload)

        job_id = _first(payload, *( (poll.id_field,) if poll.id_field else JOB_ID_FIELDS ))
        if job_id is None:
            return dict(payload)

        url = poll.url_template.format(
            base_url=provider.base_url.rstrip("/"),
            id=job_id,
            model="",
        )
        deadline = time.monotonic() + (timeout_s or self.settings.timeout_s)
        interval = poll.interval_s
        self.log.info("polling job %s at %s", job_id, url)

        while True:
            if time.monotonic() > deadline:
                raise JobFailedError(
                    f"{provider.display_name}: job {job_id} did not finish within "
                    f"{timeout_s or self.settings.timeout_s:.0f}s.",
                    provider=provider.id,
                )
            response = self.send("GET", url, provider=provider, headers=headers)
            if response.status_code >= 400:
                raise friendly_http_error(
                    provider.id, None, response.status_code, response.text,
                    url=url, key_env=provider.key_env_names[0] if provider.key_env_names else None,
                )
            try:
                data = response.json()
            except ValueError:
                raise ImageMCPError(f"{provider.display_name}: job {job_id} returned non-JSON status.") from None

            if not isinstance(data, dict):
                return {"data": data}

            result_url = data.get(poll.result_url_field)
            if result_url:
                return {"data": [{"url": result_url}], "_job": data}

            status_value = _first(data, poll.status_field, *STATUS_FIELDS)
            status = str(status_value).lower() if status_value is not None else ""
            if status in poll.done_values:
                return data
            if status in poll.failed_values:
                raise JobFailedError(
                    f"{provider.display_name}: job {job_id} failed with status '{status}'. {data}",
                    provider=provider.id,
                )

            time.sleep(interval)
            interval = min(interval * 1.5, poll.max_interval_s)
