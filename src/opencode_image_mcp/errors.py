"""Human-friendly error types and HTTP failure mapping.

Providers fail in wildly different ways; this module funnels them into a
small set of exceptions whose messages tell the caller exactly what to fix.
"""

from __future__ import annotations

import json
from typing import Any


class ImageMCPError(RuntimeError):
    """Base class for expected, user-facing failures."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.status = status


class MissingAPIKeyError(ImageMCPError):
    """No API key is available for the selected provider."""


class UnknownProviderError(ImageMCPError):
    """The provider prefix in a model spec does not exist."""


class ModelNotFoundError(ImageMCPError):
    """The requested model could not be resolved to a provider."""


class NoImagesError(ImageMCPError):
    """The provider answered successfully but returned no image data."""


class JobFailedError(ImageMCPError):
    """A polled async job reported failure, or never finished in time."""


def _dig(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("message", "error", "detail", "error_message", "reason", "title"):
            if key in data:
                found = _dig(data[key])
                if found:
                    return found
        try:
            return json.dumps(data)
        except (TypeError, ValueError):
            return str(data)
    if isinstance(data, list) and data:
        return _dig(data[0])
    return str(data)


def extract_error_message(body: str) -> str:
    """Pull a readable message out of an arbitrary error body."""
    if not body:
        return ""
    text = body.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text[:400]
    return _dig(parsed)[:400]


def friendly_http_error(
    provider_id: str,
    model: str | None,
    status: int,
    body: str,
    *,
    url: str = "",
    key_env: str | None = None,
) -> ImageMCPError:
    """Map an upstream HTTP status onto a descriptive :class:`ImageMCPError`."""
    where = provider_id if not model else f"{provider_id} / {model}"
    detail = extract_error_message(body)
    if detail:
        detail = f" Upstream said: {detail}"

    key_hint = f" Set {key_env}." if key_env else ""

    if status in (401, 403):
        message = (
            f"{where}: authentication failed (HTTP {status}).{key_hint} "
            f"Check that the API key is valid and allowed to use this model.{detail}"
        )
    elif status in (402,):
        message = (
            f"{where}: payment required (HTTP 402). The provider account is out "
            f"of credits or the plan does not cover this model.{detail}"
        )
    elif status == 404:
        message = (
            f"{where}: not found (HTTP 404). The model id or endpoint may not "
            f"exist on this provider. Try list_models to see what is available.{detail}"
        )
    elif status in (405, 415, 501):
        message = (
            f"{where}: the provider rejected this endpoint (HTTP {status}). "
            f"This model probably uses a different API dialect — try another model "
            f"or provider.{detail}"
        )
    elif status == 413:
        message = (
            f"{where}: payload too large (HTTP 413). Try a smaller reference "
            f"image or a shorter prompt.{detail}"
        )
    elif status == 429:
        message = (
            f"{where}: rate limited (HTTP 429). Wait a moment and retry, or "
            f"switch provider.{detail}"
        )
    elif 500 <= status < 600:
        message = (
            f"{where}: provider server error (HTTP {status}). This is usually "
            f"transient — retry, or try another provider.{detail}"
        )
    elif 400 <= status < 500:
        message = (
            f"{where}: request rejected (HTTP {status}). Check the parameters "
            f"for this model (aspect ratio, size, quality) and try again.{detail}"
        )
    else:
        suffix = f" ({url})" if url else ""
        message = f"{where}: unexpected HTTP {status}{suffix}.{detail}"

    return ImageMCPError(message, provider=provider_id, model=model, status=status)
