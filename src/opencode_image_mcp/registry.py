"""Model discovery, caching and resolution.

The server never hardcodes a model catalogue: it asks each configured provider
for its model list, filters down to image-capable entries, caches the result on
disk, and resolves a user's model spec against that catalogue.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from .config import Config, ProviderConfig
from .errors import ImageMCPError, MissingAPIKeyError, ModelNotFoundError, UnknownProviderError
from .transport import Transport

log = logging.getLogger("opencode-image-mcp")

# Model ids containing any of these are assumed to produce images.
IMAGE_HINTS = (
    "image",
    "dall-e",
    "dalle",
    "gpt-image",
    "flux",
    "seedream",
    "imagen",
    "nano-banana",
    "nano_banana",
    "grok-imagine",
    "qwen-image",
    "qwen_image",
    "kolors",
    "recraft",
    "ideogram",
    "sdxl",
    "stable-diffusion",
    "sd3",
    "sd-turbo",
    "wan2",
    "hidream",
    "lumina",
    "pixart",
    "playground-v",
    "janus",
    "emu3",
    "bagel",
    "z-image",
    "photon",
)
IMAGE_RE = re.compile("|".join(re.escape(hint) for hint in IMAGE_HINTS), re.IGNORECASE)

# Non-image model families that can otherwise trip the hints above.
NEGATIVE_RE = re.compile(
    r"embed|rerank|reranker|whisper|tts|speech|audio|moderation|guard|ocr|"
    r"transcribe|realtime|vision-only|clip",
    re.IGNORECASE,
)

CACHE_VERSION = 1


@dataclass
class ModelInfo:
    provider: str
    id: str
    name: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    pricing: Any = None
    created: int | None = None
    image_capable: bool = True
    source: str = "heuristic"

    @property
    def full_id(self) -> str:
        return f"{self.provider}:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "capabilities": self.capabilities,
            "pricing": self.pricing,
            "created": self.created,
            "image_capable": self.image_capable,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, provider_id: str, data: dict[str, Any]) -> "ModelInfo":
        return cls(
            provider=provider_id,
            id=str(data.get("id", "")),
            name=data.get("name"),
            capabilities=data.get("capabilities") or {},
            pricing=data.get("pricing"),
            created=data.get("created"),
            image_capable=bool(data.get("image_capable", True)),
            source=data.get("source", "cache"),
        )


class ModelCache:
    """A tiny JSON cache of discovered model lists."""

    def __init__(self, path: Path, ttl_s: int) -> None:
        self.path = path
        self.ttl_s = ttl_s
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": CACHE_VERSION, "providers": {}}
        if raw.get("version") != CACHE_VERSION:
            return {"version": CACHE_VERSION, "providers": {}}
        return raw

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError as exc:  # caching is best-effort
            log.debug("could not write model cache %s: %s", self.path, exc)

    def get(self, provider_id: str) -> list[ModelInfo] | None:
        entry = (self._data.get("providers") or {}).get(provider_id)
        if not entry:
            return None
        if time.time() - float(entry.get("fetched_at", 0)) > self.ttl_s:
            return None
        return [ModelInfo.from_dict(provider_id, item) for item in entry.get("models", [])]

    def get_stale(self, provider_id: str) -> list[ModelInfo] | None:
        entry = (self._data.get("providers") or {}).get(provider_id)
        if not entry:
            return None
        return [ModelInfo.from_dict(provider_id, item) for item in entry.get("models", [])]

    def put(self, provider_id: str, models: list[ModelInfo]) -> None:
        self._data.setdefault("version", CACHE_VERSION)
        self._data.setdefault("providers", {})[provider_id] = {
            "fetched_at": time.time(),
            "models": [model.to_dict() for model in models],
        }
        self._save()


def _match_any(patterns: list[str], text: str) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in text.lower():
                return True
    return False


def _extract_model_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "models", "result", "items", "results"):
            sequence = payload.get(key)
            if isinstance(sequence, list):
                return [item for item in sequence if isinstance(item, dict)]
    return []


def _is_image_model(model_id: str, provider: ProviderConfig) -> bool:
    if provider.include_models and _match_any(provider.include_models, model_id):
        return True
    if provider.exclude_models and _match_any(provider.exclude_models, model_id):
        return False
    if NEGATIVE_RE.search(model_id) and not IMAGE_RE.search(model_id):
        return False
    return bool(IMAGE_RE.search(model_id))


def discover_models(
    transport: Transport,
    provider: ProviderConfig,
    *,
    image_only: bool = True,
    timeout_s: float | None = None,
) -> list[ModelInfo]:
    """Ask a provider what models it offers and normalise the answer."""
    path = provider.image_models_path or provider.models_path
    response = transport.get(provider, path, timeout_s=timeout_s)
    if response.status_code >= 400:
        raise ImageMCPError(
            f"{provider.display_name}: could not list models (HTTP {response.status_code}): "
            f"{response.text[:200]}",
            provider=provider.id,
            status=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ImageMCPError(f"{provider.display_name}: model list was not valid JSON") from exc

    authoritative = provider.image_models_path is not None
    results: list[ModelInfo] = []

    for entry in _extract_model_list(payload):
        model_id = entry.get("id") or entry.get("name")
        if not isinstance(model_id, str) or not model_id:
            continue

        capabilities: dict[str, Any] = {}
        architecture = entry.get("architecture")
        image_capable = True

        if isinstance(architecture, dict):
            output_modalities = architecture.get("output_modalities")
            input_modalities = architecture.get("input_modalities")
            if isinstance(output_modalities, list):
                image_capable = any(str(mod).lower() == "image" for mod in output_modalities)
                capabilities["output_modalities"] = output_modalities
            if isinstance(input_modalities, list):
                capabilities["input_modalities"] = input_modalities

        supported = entry.get("supported_parameters")
        if isinstance(supported, dict):
            capabilities["supported_parameters"] = supported

        if entry.get("supports_streaming") is not None:
            capabilities["supports_streaming"] = entry.get("supports_streaming")

        if authoritative and not image_capable:
            continue
        if image_only and not authoritative and not _is_image_model(model_id, provider):
            continue
        if provider.exclude_models and _match_any(provider.exclude_models, model_id):
            continue

        results.append(
            ModelInfo(
                provider=provider.id,
                id=model_id,
                name=entry.get("name") if isinstance(entry.get("name"), str) else None,
                capabilities=capabilities,
                pricing=entry.get("pricing"),
                created=entry.get("created") if isinstance(entry.get("created"), int) else None,
                image_capable=image_capable,
                source="provider-api" if authoritative else "heuristic",
            )
        )

    results.sort(key=lambda model: model.id)
    return results


def get_models(
    config: Config,
    transport: Transport,
    provider_id: str | None = None,
    *,
    refresh: bool = False,
) -> tuple[dict[str, list[ModelInfo]], dict[str, str]]:
    """Return ``({provider: [models]}, {provider: error})`` using the cache."""
    cache = ModelCache(config.settings.cache_dir / "models.json", config.settings.model_ttl_s)

    if provider_id is not None:
        provider = config.providers.get(provider_id)
        if provider is None:
            raise UnknownProviderError(
                f"Unknown provider '{provider_id}'. Known providers: {sorted(config.providers)}",
                provider=provider_id,
            )
        targets = [provider]
    else:
        targets = config.configured_providers()

    models: dict[str, list[ModelInfo]] = {}
    errors: dict[str, str] = {}

    for provider in targets:
        if not provider.is_configured():
            env_hint = ", ".join(provider.key_env_names) or "an API key"
            errors[provider.id] = f"not configured (set {env_hint})"
            continue
        if not refresh:
            cached = cache.get(provider.id)
            if cached is not None:
                models[provider.id] = cached
                continue
        try:
            discovered = discover_models(transport, provider)
            cache.put(provider.id, discovered)
            models[provider.id] = discovered
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as text
            stale = cache.get_stale(provider.id)
            if stale is not None:
                models[provider.id] = stale
                errors[provider.id] = f"live discovery failed ({exc}); showing cached list"
            else:
                errors[provider.id] = str(exc)

    return models, errors


def _all_model_ids(config: Config, transport: Transport) -> list[str]:
    models, _ = get_models(config, transport)
    ids: list[str] = []
    for provider_id, provider_models in models.items():
        for model in provider_models:
            ids.append(model.full_id)
            ids.append(model.id)
    return ids


def resolve_model(
    spec: str,
    config: Config,
    transport: Transport,
    *,
    refresh: bool = False,
) -> tuple[ProviderConfig, str]:
    """Resolve ``provider:model``, an alias, or a bare model id to a provider."""
    if not spec:
        raise ModelNotFoundError("No model was specified and no default is configured.")

    if spec in config.aliases:
        spec = config.aliases[spec]

    if ":" in spec:
        provider_id, _, model_id = spec.partition(":")
        provider = config.providers.get(provider_id)
        if provider is None:
            raise UnknownProviderError(
                f"Unknown provider '{provider_id}' in model spec '{spec}'. "
                f"Known providers: {sorted(config.providers)}",
                provider=provider_id,
            )
        if not model_id:
            raise ModelNotFoundError(f"Model spec '{spec}' is missing a model id after the colon.")
        if not provider.is_configured():
            env_hint = ", ".join(provider.key_env_names) or "an API key"
            raise MissingAPIKeyError(
                f"Provider '{provider_id}' is not configured. Set {env_hint}.",
                provider=provider_id,
                model=model_id,
            )
        return provider, model_id

    # Bare model id: search every configured provider's catalogue.
    models, errors = get_models(config, transport, refresh=refresh)
    if not models:
        detail = "; ".join(f"{pid}: {msg}" for pid, msg in errors.items()) or "no providers are configured"
        raise ModelNotFoundError(
            f"Could not search for model '{spec}' because no provider catalogue is available ({detail}). "
            f"Use a 'provider:model' spec instead."
        )

    matches: list[ModelInfo] = []
    for provider_models in models.values():
        for model in provider_models:
            if model.id == spec:
                matches.append(model)
    if not matches:
        lowered = spec.lower()
        for provider_models in models.values():
            for model in provider_models:
                if model.id.lower() == lowered:
                    matches.append(model)
    if not matches:
        suffix = f"/{spec}"
        for provider_models in models.values():
            for model in provider_models:
                if model.id.endswith(suffix):
                    matches.append(model)

    if len(matches) == 1:
        match = matches[0]
        return config.providers[match.provider], match.id
    if len(matches) > 1:
        candidates = ", ".join(sorted(match.full_id for match in matches))
        raise ModelNotFoundError(
            f"Model '{spec}' exists on multiple providers: {candidates}. "
            f"Pick one with a 'provider:model' spec."
        )

    suggestions = get_close_matches(spec, _all_model_ids(config, transport), n=5, cutoff=0.5)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else " Try list_models to see what is available."
    raise ModelNotFoundError(f"No provider offers a model matching '{spec}'.{hint}")
