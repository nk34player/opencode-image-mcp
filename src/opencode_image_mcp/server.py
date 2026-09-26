"""The MCP server: tools for listing providers/models and generating images.

Works against any OpenAI-compatible image API. The flow is always the same:
pick a provider, discover its models, resolve the model the user asked for,
generate, wait for the result, and save it to disk.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from .config import (
    BUILTIN_PROVIDER_IDS,
    Config,
    ProviderConfig,
    default_target,
    load_config,
    provider_config_dict,
    remove_provider_from_file,
    set_default_model_in_file,
    upsert_provider,
    writable_config_path,
)
from .dialects import (
    GenerationRequest,
    build_request_body,
    extract_text,
    parse_response,
    request_path,
)
from .errors import (
    ImageMCPError,
    MissingAPIKeyError,
    NoImagesError,
    friendly_http_error,
)
from .images import load_image_file, resolve_mime, save_blobs
from .registry import discover_models, get_models, resolve_model
from .transport import Transport

logger = logging.getLogger("opencode-image-mcp")

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

_LOCK = threading.RLock()
_STATE: dict[str, Any] = {}


def _configure_logging(level: str) -> None:
    try:
        logging.getLogger("opencode-image-mcp").setLevel(level.upper())
    except (ValueError, AttributeError):
        pass


def _get_config() -> Config:
    with _LOCK:
        if "config" not in _STATE:
            config = load_config()
            _configure_logging(config.settings.log_level)
            _STATE["config"] = config
        return _STATE["config"]


def _get_transport() -> Transport:
    with _LOCK:
        if "transport" not in _STATE:
            _STATE["transport"] = Transport(_get_config().settings, logger=logger)
        return _STATE["transport"]


def reset_state(config: Config | None = None, transport: Transport | None = None) -> None:
    """Test seam: drop cached config/transport."""
    with _LOCK:
        _STATE.clear()
        if config is not None:
            _STATE["config"] = config
        if transport is not None:
            _STATE["transport"] = transport


def _dialect_for(request: GenerationRequest) -> str:
    if request.mode == "edit":
        provider = request.provider
        if provider.edit_style == "chat" or provider.edit_path is None:
            return "chat"
        return "images"
    return request.provider.dialect


def _image_content(path: Path) -> ImageContent:
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or resolve_mime(data) or "image/png"
    return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type=mime)


mcp = MCPServer(
    "opencode-image",
    instructions=(
        "Generate and edit images through any OpenAI-compatible provider. "
        "Use list_providers to see configured providers, list_models to discover "
        "image models, then generate_image with a 'provider:model' spec."
    ),
)


@mcp.tool(
    title="List image providers",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def list_providers() -> dict[str, Any]:
    """List configured image providers and whether their API key is present.

    A provider only appears as ``configured: true`` once its key env var is set.
    Use the returned ``id`` values as the prefix in ``provider:model`` specs.
    """
    config = _get_config()
    target = writable_config_path(config.settings.config_path)
    providers = []
    for provider in config.providers.values():
        providers.append(
            {
                "id": provider.id,
                "label": provider.display_name,
                "base_url": provider.base_url,
                "dialect": provider.dialect,
                "configured": provider.is_configured(),
                "key_source": provider.key_source(),
                "key_env": provider.key_env_names,
                "custom": provider.id not in BUILTIN_PROVIDER_IDS,
                "default_model": provider.default_model,
                "supports_edit": bool(provider.edit_path) or provider.dialect == "chat",
                "polls_async_jobs": provider.poll is not None,
            }
        )
    return {
        "providers": providers,
        "configured_count": sum(1 for entry in providers if entry["configured"]),
        "default_model": config.default_model or default_target(config),
        "output_dir": str(config.settings.output_dir),
        "config_file": str(target),
        "aliases": config.aliases,
    }


@mcp.tool(
    title="List image models",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def list_models(provider: str | None = None, refresh: bool = False) -> dict[str, Any]:
    """Discover image-capable models offered by configured providers.

    Queries each provider's model endpoint, filters to image models, and caches
    the result on disk. Set ``refresh=true`` to bypass the cache. Pass
    ``provider`` to query just one provider.
    """
    config = _get_config()
    transport = _get_transport()
    models, errors = get_models(config, transport, provider, refresh=refresh)

    flat: list[dict[str, Any]] = []
    for provider_id, provider_models in models.items():
        for model in provider_models:
            flat.append(
                {
                    "provider": provider_id,
                    "id": model.id,
                    "full_id": model.full_id,
                    "name": model.name,
                    "source": model.source,
                    "capabilities": model.capabilities or None,
                    "pricing": model.pricing,
                }
            )

    return {
        "count": len(flat),
        "models": flat,
        "errors": errors,
        "hint": "Pass a full_id such as 'myendpoint:my-model' to generate_image.",
    }


_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@mcp.tool(
    title="Add a custom provider",
    annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=True),
)
def add_provider(
    id: str,
    base_url: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    default_model: str | None = None,
    dialect: str = "auto",
    make_default: bool = False,
    extra_headers: dict[str, str] | None = None,
    include_models: list[str] | None = None,
    exclude_models: list[str] | None = None,
    aspect_ratio_mode: str | None = None,
    size_style: str | None = None,
    discover: bool = True,
) -> dict[str, Any]:
    """Register any OpenAI-compatible image endpoint and remember it for later.

    Use this when the user names an endpoint like ``https://api.example.com/v1``.
    The server probes ``{base_url}/models``, figures out which models can produce
    images, saves the provider to the config file, and makes it available to
    ``generate_image`` immediately and on every future run.

    Parameters
    ----------
    id:
        Short handle used in ``provider:model`` specs (e.g. ``example``).
        Lowercase letters, digits, ``-`` and ``_``.
    base_url:
        The OpenAI-compatible API root, e.g. ``https://api.example.com/v1``.
    api_key:
        Optional bearer token, stored in the gitignored config file so it
        survives restarts. Prefer ``api_key_env`` if you would rather keep the
        secret in an environment variable.
    api_key_env:
        Name of an env var holding the key (ignored when ``api_key`` is given).
        Leave both unset for endpoints that need no auth.
    default_model:
        Model id to use by default. If omitted and exactly one image model is
        found, it is selected automatically.
    dialect:
        ``auto`` (default), ``images`` (``/images/generations``) or ``chat``
        (``/chat/completions`` with image modalities). ``auto`` uses the Images
        API and transparently falls back to chat-completions on 404/405.
    make_default:
        Also set this provider's model as the global default model.
    discover:
        Set ``false`` to skip the ``/models`` probe (required for endpoints
        that do not implement it).

    Returns the discovered models plus any warnings; inspect ``models`` and, if
    there are several, call ``set_default_model`` to choose one.
    """
    config = _get_config()
    transport = _get_transport()

    provider_id = (id or "").strip().lower()
    if not _PROVIDER_ID_RE.match(provider_id):
        raise ImageMCPError(
            "Provider id must be lowercase letters, digits, '-' or '_' (max 64 chars).",
            provider=provider_id,
        )

    url = (base_url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ImageMCPError("base_url must start with http:// or https://", provider=provider_id)

    chosen = (dialect or "auto").strip().lower()
    if chosen not in ("auto", "images", "chat"):
        raise ImageMCPError("dialect must be 'auto', 'images' or 'chat'", provider=provider_id)
    resolved_dialect = "images" if chosen == "auto" else chosen

    mode = aspect_ratio_mode or ("prompt" if resolved_dialect == "chat" else "size")

    provider = ProviderConfig(
        id=provider_id,
        base_url=url,
        dialect=resolved_dialect,
        default_model=(default_model or None),
        label=provider_id,
        api_key=(api_key or None),
        api_key_env=(None if api_key else (api_key_env or None)),
        aspect_ratio_mode=mode,
        size_style=(size_style or "string"),
        include_models=list(include_models or []),
        exclude_models=list(exclude_models or []),
        extra_headers=dict(extra_headers or {}),
    )

    models: list[dict[str, Any]] = []
    warnings: list[str] = []

    if discover:
        try:
            found = discover_models(transport, provider)
            for model in found:
                capabilities = model.capabilities or {}
                models.append(
                    {
                        "id": model.id,
                        "name": model.name,
                        "source": model.source,
                        "accepts_image_input": "image" in (capabilities.get("input_modalities") or []),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            warnings.append(
                f"Model discovery failed ({exc}). The provider was still saved; "
                f"pass default_model explicitly or try discover=false."
            )

    if provider.default_model is None:
        if len(models) == 1:
            provider.default_model = models[0]["id"]
        elif len(models) > 1:
            warnings.append(
                "Several image models were found - pick one with set_default_model "
                "(a 'provider:model' spec), or pass default_model next time."
            )

    if discover and not models:
        warnings.append(
            "No image models were detected. The endpoint may not implement /models, "
            "or uses unusual model names - pass default_model explicitly."
        )

    target = writable_config_path(config.settings.config_path)
    upsert_provider(target, provider_id, provider_config_dict(provider))
    config.providers[provider_id] = provider

    applied_default: str | None = None
    if make_default and provider.default_model:
        applied_default = f"{provider_id}:{provider.default_model}"
        set_default_model_in_file(target, applied_default)
        config.default_model = applied_default

    result: dict[str, Any] = {
        "id": provider_id,
        "base_url": provider.base_url,
        "dialect": provider.dialect,
        "default_model": provider.default_model,
        "configured": provider.is_configured(),
        "key_source": provider.key_source(),
        "saved_to": str(target),
        "models": models,
        "warnings": warnings,
    }
    if applied_default:
        result["default_model_spec"] = applied_default
        result["hint"] = f"Ready. Call generate_image without a model to use {applied_default}."
    elif provider.default_model:
        result["hint"] = f"Ready. Use '{provider_id}:{provider.default_model}', or set_default_model to make it the default."
    else:
        result["hint"] = f"Call set_default_model with '{provider_id}:<model-id>' to finish setup."
    return result


@mcp.tool(
    title="Remove a provider",
    annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
)
def remove_provider(id: str) -> dict[str, Any]:
    """Remove a provider (including built-in presets) from the config file."""
    config = _get_config()
    provider_id = (id or "").strip().lower()
    target = writable_config_path(config.settings.config_path)

    existed = provider_id in config.providers
    remove_provider_from_file(target, provider_id)
    config.providers.pop(provider_id, None)

    cleared_default = False
    if config.default_model and config.default_model.split(":", 1)[0] == provider_id:
        config.default_model = None
        set_default_model_in_file(target, None)
        cleared_default = True

    return {
        "removed": provider_id,
        "existed": existed,
        "default_model_cleared": cleared_default,
        "saved_to": str(target),
    }


@mcp.tool(
    title="Set the default model",
    annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
)
def set_default_model(model: str) -> dict[str, Any]:
    """Persist the model used when ``generate_image`` is called without one.

    ``model`` is a ``provider:model`` spec or an alias, e.g.
    ``example:flux-schnell``. The choice is saved to the config file so it
    applies to future runs.
    """
    config = _get_config()
    spec = (model or "").strip()
    if not spec:
        raise ImageMCPError("model must be a non-empty 'provider:model' spec or alias")

    alias_of = config.aliases.get(spec)
    if ":" in spec:
        provider_id = spec.split(":", 1)[0]
        if provider_id not in config.providers:
            raise ImageMCPError(
                f"Unknown provider '{provider_id}'. Add it with add_provider first, "
                f"or use list_providers to see what exists.",
                provider=provider_id,
            )

    target = writable_config_path(config.settings.config_path)
    set_default_model_in_file(target, spec)
    config.default_model = spec

    return {
        "default_model": spec,
        "alias_of": alias_of,
        "saved_to": str(target),
        "hint": "generate_image calls without a model will now use this.",
    }


@mcp.tool(
    title="Generate or edit an image",
    annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True, idempotent_hint=False),
)
def generate_image(
    prompt: str,
    model: str | None = None,
    aspect_ratio: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    n: int = 1,
    output_path: str | None = None,
    filename_prefix: str | None = None,
    input_image_path: str | None = None,
    input_image_paths: list[str] | None = None,
    output_format: str | None = None,
    background: str | None = None,
    seed: int | None = None,
    return_image_content: bool = False,
    timeout_s: float | None = None,
    extra_body: dict[str, Any] | None = None,
) -> CallToolResult:
    """Generate an image, or edit existing images, via an OpenAI-compatible provider.

    Model selection
    ---------------
    ``model`` accepts three forms:

    * ``provider:model`` — explicit, e.g. ``openrouter:google/gemini-3.1-flash-image-preview``,
      ``openai:gpt-image-1``, ``xai:grok-imagine-image-2.0``. Fastest and unambiguous.
    * a bare model id — searched across every configured provider's catalogue
      (run ``list_models`` first). Ambiguous ids raise an error listing candidates.
    * an alias — defined in ``providers.json`` (e.g. ``nb2``).

    If omitted, the configured default model is used.

    Editing
    -------
    Pass ``input_image_path`` (and optionally ``input_image_paths``) and the
    ``prompt`` becomes an edit instruction. Requirements vary by provider.

    Output
    ------
    Images are always written to disk and their paths returned. Set
    ``return_image_content=true`` to additionally embed the images in the tool
    result so the model can look at them.
    """
    config = _get_config()
    transport = _get_transport()

    spec = model or default_target(config)
    if not spec:
        raise MissingAPIKeyError(
            "No model was specified and no provider is configured. Add your own "
            "endpoint with add_provider (e.g. base_url 'https://api.example.com/v1'), "
            "or set a provider API key env var, then retry."
        )

    provider, model_id = resolve_model(spec, config, transport)

    reference_paths = [path for path in ([input_image_path] + list(input_image_paths or [])) if path]
    input_images = [load_image_file(path) for path in reference_paths]
    mode = "edit" if input_images else "generate"

    request = GenerationRequest(
        provider=provider,
        model=model_id,
        prompt=prompt,
        mode=mode,
        n=max(1, int(n or 1)),
        aspect_ratio=aspect_ratio,
        size=size,
        quality=quality,
        output_format=output_format,
        background=background,
        seed=seed,
        extra_body=dict(extra_body or {}),
        input_images=input_images,
    )

    dialect = _dialect_for(request)
    path = request_path(request)
    body = build_request_body(request)

    started = time.monotonic()
    response = transport.post_json(provider, path, body, timeout_s=timeout_s)

    # Some providers 404/405 the Images API but speak chat-completions fine.
    if (
        response.status_code in (404, 405, 415, 501)
        and provider.allow_chat_fallback
        and dialect == "images"
        and provider.dialect == "images"
    ):
        logger.info("%s: falling back to chat-completions dialect", provider.id)
        chat_provider = replace(provider, dialect="chat")
        chat_request = replace(request, provider=chat_provider)
        fallback = transport.post_json(
            chat_provider, chat_provider.chat_path, build_request_body(chat_request), timeout_s=timeout_s
        )
        if fallback.status_code < 400:
            response = fallback
            provider = chat_provider
            dialect = "chat"
            path = chat_provider.chat_path

    if response.status_code >= 400:
        raise friendly_http_error(
            provider.id,
            model_id,
            response.status_code,
            response.text,
            url=transport.provider_url(provider, path),
            key_env=provider.key_env_names[0] if provider.key_env_names else None,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ImageMCPError(
            f"{provider.display_name} returned a non-JSON response (HTTP {response.status_code}).",
            provider=provider.id,
            model=model_id,
        ) from exc

    result = parse_response(payload, dialect)

    if not result.blobs and provider.poll is not None:
        logger.info("%s: no image yet, polling job", provider.id)
        payload = transport.poll_job(provider, payload, timeout_s=timeout_s)
        result = parse_response(payload, dialect)

    if not result.blobs:
        spoken = extract_text(payload) or "(the model returned no text)"
        raise NoImagesError(
            f"{provider.display_name} / {model_id} returned no image data. "
            f"Model said: {spoken[:400]}",
            provider=provider.id,
            model=model_id,
        )

    for blob in result.blobs:
        if blob.data is None and blob.url:
            data, content_type = transport.download(blob.url, provider)
            blob.data = data
            blob.mime = resolve_mime(data, blob.mime or content_type)

    try:
        file_paths = save_blobs(
            result.blobs,
            prompt=prompt,
            default_dir=config.settings.output_dir,
            output_path=output_path,
            filename_prefix=filename_prefix,
        )
    except ValueError as exc:
        raise NoImagesError(str(exc), provider=provider.id, model=model_id) from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    output: dict[str, Any] = {
        "file_paths": [str(file_path) for file_path in file_paths],
        "provider": provider.id,
        "model_id": model_id,
        "dialect": dialect,
        "mode": mode,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "image_count": len(file_paths),
        "elapsed_ms": elapsed_ms,
        "usage": result.usage,
    }
    if result.cost is not None:
        output["cost_usd"] = result.cost
    if result.text:
        output["model_text"] = result.text[:1000]

    content: list[Any] = [TextContent(type="text", text=json.dumps(output, indent=2, default=str))]
    if return_image_content:
        content.extend(_image_content(file_path) for file_path in file_paths)

    return CallToolResult(content=content, structured_content=output)


def main() -> None:
    """Entry point for the console script and ``python -m opencode_image_mcp``."""
    config = _get_config()
    _configure_logging(config.settings.log_level)
    logger.info(
        "opencode-image-mcp starting with %d configured provider(s)",
        len(config.configured_providers()),
    )
    mcp.run()


if __name__ == "__main__":
    main()
