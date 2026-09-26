"""Configuration: built-in provider presets, environment variables and JSON.

A provider is *configured* as soon as an API key is available for it. The
server ships with presets for the common OpenAI-compatible vendors and can be
extended or overridden with a ``providers.json`` file.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path.home() / "opencode-images"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "opencode-image-mcp"

PROVIDER_KEYS = {
    "id",
    "base_url",
    "dialect",
    "default_model",
    "label",
    "api_key",
    "api_key_env",
    "alt_api_key_envs",
    "auth_header",
    "auth_scheme",
    "models_path",
    "image_models_path",
    "generation_path",
    "edit_path",
    "chat_path",
    "edit_style",
    "aspect_ratio_mode",
    "size_style",
    "size_param",
    "send_response_format",
    "chat_modalities",
    "include_models",
    "exclude_models",
    "extra_headers",
    "extra_query",
    "allow_chat_fallback",
    "poll",
}

# Providers that ship with the server. Anything else was added at runtime.
BUILTIN_PROVIDER_IDS = frozenset(
    {"openai", "openrouter", "gemini", "xai", "together", "deepinfra", "siliconflow", "fireworks"}
)


@dataclass
class PollConfig:
    """How to wait for providers that answer with a job id instead of an image."""

    url_template: str
    status_field: str = "status"
    id_field: str | None = None
    done_values: tuple[str, ...] = (
        "completed",
        "complete",
        "succeeded",
        "success",
        "done",
        "ready",
        "finished",
    )
    failed_values: tuple[str, ...] = (
        "failed",
        "failure",
        "error",
        "cancelled",
        "canceled",
        "expired",
    )
    result_url_field: str = "result_url"
    interval_s: float = 3.0
    max_interval_s: float = 15.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PollConfig":
        if not isinstance(data, dict) or not data.get("url_template"):
            raise ValueError("poll requires a 'url_template'")
        kwargs: dict[str, Any] = {}
        for key in (
            "url_template",
            "status_field",
            "id_field",
            "result_url_field",
        ):
            if key in data:
                kwargs[key] = data[key]
        for key in ("done_values", "failed_values"):
            if key in data:
                kwargs[key] = tuple(data[key])
        for key in ("interval_s", "max_interval_s"):
            if key in data:
                kwargs[key] = float(data[key])
        return cls(**kwargs)


@dataclass
class ProviderConfig:
    id: str
    base_url: str
    dialect: str = "images"  # "images" | "chat"
    default_model: str | None = None
    label: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    alt_api_key_envs: list[str] = field(default_factory=list)

    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"

    # Discovery
    models_path: str = "/models"
    image_models_path: str | None = None

    # Endpoints
    generation_path: str = "/images/generations"
    edit_path: str | None = "/images/edits"
    chat_path: str = "/chat/completions"
    edit_style: str = "openai_json"  # openai_json | xai | chat
    chat_modalities: bool = True

    # Parameter shaping
    aspect_ratio_mode: str = "size"  # size | field | prompt
    size_style: str = "string"  # string | width_height | image_size | none
    size_param: str = "size"
    send_response_format: bool = False

    # Filtering
    include_models: list[str] = field(default_factory=list)
    exclude_models: list[str] = field(default_factory=list)

    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_query: dict[str, str] = field(default_factory=dict)

    allow_chat_fallback: bool = True
    poll: PollConfig | None = None

    @property
    def display_name(self) -> str:
        return self.label or self.id

    @property
    def key_env_names(self) -> list[str]:
        names: list[str] = []
        if self.api_key_env:
            names.append(self.api_key_env)
        names.extend(self.alt_api_key_envs)
        return names

    def api_key_value(self) -> str | None:
        if self.api_key:
            return self.api_key
        for name in self.key_env_names:
            value = os.environ.get(name)
            if value:
                return value
        return None

    def is_configured(self) -> bool:
        """Providers with no declared key env are treated as anonymous/open."""
        if not self.key_env_names:
            return True
        return bool(self.api_key_value())

    def key_source(self) -> str:
        """Describe where the key comes from, without ever exposing the key."""
        if self.api_key:
            return "config-file"
        for name in self.key_env_names:
            if os.environ.get(name):
                return f"env:{name}"
        if self.key_env_names:
            return "missing"
        return "anonymous"


@dataclass
class Settings:
    output_dir: Path = DEFAULT_OUTPUT_DIR
    cache_dir: Path = DEFAULT_CACHE_DIR
    timeout_s: float = 300.0
    connect_timeout_s: float = 20.0
    retries: int = 3
    backoff_base_s: float = 1.5
    backoff_max_s: float = 20.0
    model_ttl_s: int = 86400
    log_level: str = "INFO"
    config_path: Path | None = None


@dataclass
class Config:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    default_model: str | None = None
    settings: Settings = field(default_factory=Settings)

    @property
    def provider_order(self) -> list[str]:
        return list(self.providers)

    def configured_providers(self) -> list[ProviderConfig]:
        return [p for p in self.providers.values() if p.is_configured()]


def _presets() -> dict[str, ProviderConfig]:
    """Fresh copies of the built-in provider presets."""
    return {
        "openai": ProviderConfig(
            id="openai",
            base_url="https://api.openai.com/v1",
            dialect="images",
            default_model="gpt-image-1",
            label="OpenAI",
            api_key_env="OPENAI_API_KEY",
            aspect_ratio_mode="size",
            size_style="string",
            edit_style="openai_json",
        ),
        "openrouter": ProviderConfig(
            id="openrouter",
            base_url="https://openrouter.ai/api/v1",
            dialect="images",
            default_model="google/gemini-3.1-flash-image-preview",
            label="OpenRouter",
            api_key_env="OPENROUTER_API_KEY",
            generation_path="/images",
            models_path="/models",
            image_models_path="/images/models",
            edit_path=None,
            edit_style="chat",
            aspect_ratio_mode="field",
            size_style="string",
            extra_headers={"HTTP-Referer": "https://github.com/", "X-Title": "opencode-image-mcp"},
        ),
        "gemini": ProviderConfig(
            id="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            dialect="images",
            default_model="gemini-2.5-flash-image",
            label="Google Gemini",
            api_key_env="GEMINI_API_KEY",
            alt_api_key_envs=["GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"],
            aspect_ratio_mode="field",
            size_style="string",
            send_response_format=True,
            edit_style="openai_json",
        ),
        "xai": ProviderConfig(
            id="xai",
            base_url="https://api.x.ai/v1",
            dialect="images",
            default_model="grok-imagine-image-2.0",
            label="xAI",
            api_key_env="XAI_API_KEY",
            aspect_ratio_mode="field",
            size_style="none",
            edit_style="xai",
        ),
        "together": ProviderConfig(
            id="together",
            base_url="https://api.together.xyz/v1",
            dialect="images",
            default_model="black-forest-labs/FLUX.1-schnell",
            label="Together AI",
            api_key_env="TOGETHER_API_KEY",
            size_style="width_height",
        ),
        "deepinfra": ProviderConfig(
            id="deepinfra",
            base_url="https://api.deepinfra.com/v1/openai",
            dialect="images",
            default_model="black-forest-labs/FLUX-1-schnell",
            label="DeepInfra",
            api_key_env="DEEPINFRA_API_KEY",
            size_style="width_height",
        ),
        "siliconflow": ProviderConfig(
            id="siliconflow",
            base_url="https://api.siliconflow.cn/v1",
            dialect="images",
            default_model="Qwen/Qwen-Image",
            label="SiliconFlow",
            api_key_env="SILICONFLOW_API_KEY",
            size_style="image_size",
        ),
        "fireworks": ProviderConfig(
            id="fireworks",
            base_url="https://api.fireworks.ai/inference/v1",
            dialect="images",
            default_model="accounts/fireworks/models/flux-1-schnell",
            label="Fireworks AI",
            api_key_env="FIREWORKS_API_KEY",
            size_style="width_height",
        ),
        "custom": ProviderConfig(
            id="custom",
            base_url=os.environ.get("IMAGE_MCP_BASE_URL", ""),
            dialect="images",
            default_model=os.environ.get("IMAGE_MCP_MODEL"),
            label="Custom endpoint",
            api_key_env="IMAGE_MCP_API_KEY",
            alt_api_key_envs=["OPENAI_API_KEY"],
            aspect_ratio_mode="size",
            size_style="string",
        ),
    }


def _provider_from_dict(provider_id: str, data: dict[str, Any], base: ProviderConfig | None) -> ProviderConfig:
    if not isinstance(data, dict):
        raise ValueError(f"provider '{provider_id}' must be an object")

    unknown = set(data) - PROVIDER_KEYS
    if unknown:
        raise ValueError(f"provider '{provider_id}' has unknown keys: {sorted(unknown)}")

    template = base or ProviderConfig(id=provider_id, base_url="")
    merged = copy.deepcopy(template)
    merged.id = provider_id

    for key, value in data.items():
        if key == "poll":
            merged.poll = PollConfig.from_dict(value) if value else None
        elif key in ("include_models", "exclude_models", "alt_api_key_envs"):
            setattr(merged, key, list(value or []))
        elif key in ("extra_headers", "extra_query"):
            setattr(merged, key, dict(value or {}))
        else:
            setattr(merged, key, value)

    if not merged.base_url:
        raise ValueError(f"provider '{provider_id}' needs a base_url")
    return merged


def user_config_path() -> Path:
    """The stable, writable location for runtime-added providers."""
    env = os.environ.get("IMAGE_MCP_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "opencode-image-mcp" / "providers.json"


def writable_config_path(config_path: Path | None) -> Path:
    """Where providers added at runtime get saved."""
    if config_path is not None:
        return Path(config_path).expanduser()
    return user_config_path()


def _find_config_path(explicit: Path | None) -> Path | None:
    if explicit:
        return explicit
    env = os.environ.get("IMAGE_MCP_CONFIG")
    if env:
        return Path(env).expanduser()
    user = Path.home() / ".config" / "opencode-image-mcp" / "providers.json"
    if user.is_file():
        return user
    cwd = Path.cwd() / "providers.json"
    if cwd.is_file():
        return cwd
    return None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _settings_from_env() -> Settings:
    settings = Settings()
    output = os.environ.get("IMAGE_MCP_OUTPUT_DIR") or os.environ.get("OUTPUT_DIR")
    if output:
        settings.output_dir = Path(output).expanduser()
    cache = os.environ.get("IMAGE_MCP_CACHE_DIR")
    if cache:
        settings.cache_dir = Path(cache).expanduser()
    settings.timeout_s = _env_float("IMAGE_MCP_TIMEOUT", settings.timeout_s)
    settings.retries = _env_int("IMAGE_MCP_RETRIES", settings.retries)
    settings.model_ttl_s = _env_int("IMAGE_MCP_MODEL_TTL", settings.model_ttl_s)
    settings.log_level = os.environ.get("IMAGE_MCP_LOG_LEVEL") or os.environ.get("LOG_LEVEL") or settings.log_level
    return settings


def _apply_settings_dict(settings: Settings, data: dict[str, Any]) -> Settings:
    if not isinstance(data, dict):
        return settings
    if "output_dir" in data and data["output_dir"]:
        settings.output_dir = Path(str(data["output_dir"])).expanduser()
    if "cache_dir" in data and data["cache_dir"]:
        settings.cache_dir = Path(str(data["cache_dir"])).expanduser()
    if "timeout_s" in data and data["timeout_s"]:
        settings.timeout_s = float(data["timeout_s"])
    if "retries" in data and data["retries"] is not None:
        settings.retries = int(data["retries"])
    if "model_ttl_s" in data and data["model_ttl_s"] is not None:
        settings.model_ttl_s = int(data["model_ttl_s"])
    if "log_level" in data and data["log_level"]:
        settings.log_level = str(data["log_level"])
    return settings


def load_config(path: str | Path | None = None) -> Config:
    """Build the effective configuration from presets, JSON and env vars."""
    providers = _presets()
    aliases: dict[str, str] = {}
    default_model: str | None = None
    settings = _settings_from_env()

    explicit = Path(path).expanduser() if path else None
    config_path = _find_config_path(explicit)
    if config_path is not None and config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not read config file {config_path}: {exc}") from exc
        settings = _apply_settings_dict(settings, raw.get("settings") or {})
        for provider_id, provider_data in (raw.get("providers") or {}).items():
            if provider_data is None:  # {"together": null} disables a preset
                providers.pop(provider_id, None)
                continue
            providers[provider_id] = _provider_from_dict(provider_id, provider_data, providers.get(provider_id))
        aliases.update({str(k): str(v) for k, v in (raw.get("aliases") or {}).items()})
        default_model = raw.get("default_model") or default_model
    settings.config_path = config_path

    # Environment overrides for the well-known presets.
    if "openai" in providers and os.environ.get("OPENAI_BASE_URL"):
        providers["openai"].base_url = os.environ["OPENAI_BASE_URL"].rstrip("/")

    custom = providers.get("custom")
    if custom is not None:
        env_base = os.environ.get("IMAGE_MCP_CUSTOM_BASE_URL") or os.environ.get("IMAGE_MCP_BASE_URL")
        if env_base:
            custom.base_url = env_base.rstrip("/")
        if not custom.base_url:
            providers.pop("custom", None)
        elif not custom.api_key_value():
            # Local/self-hosted endpoints often need no key: treat as anonymous
            # rather than silently ignoring the provider.
            custom.api_key_env = None
            custom.alt_api_key_envs = []

    return Config(
        providers=providers,
        aliases=aliases,
        default_model=default_model,
        settings=settings,
    )


def default_target(config: Config) -> str | None:
    """Pick the model spec to use when the caller does not name one."""
    if config.default_model:
        return config.default_model
    for provider in config.configured_providers():
        if provider.default_model:
            return f"{provider.id}:{provider.default_model}"
    return None


# ---------------------------------------------------------------------------
# Runtime persistence: read/modify/write providers.json
# ---------------------------------------------------------------------------


def read_config_file(path: Path) -> dict[str, Any]:
    """Read a providers.json, returning ``{}`` when it does not exist yet."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a JSON object")
    return data


def write_config_file(path: Path, data: dict[str, Any]) -> None:
    """Write atomically so a crash can't leave a half-written config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def upsert_provider(path: Path, provider_id: str, provider_data: dict[str, Any]) -> dict[str, Any]:
    """Add or replace a provider in the config file, preserving everything else."""
    raw = read_config_file(path)
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers[provider_id] = provider_data
    raw["providers"] = providers
    write_config_file(path, raw)
    return raw


def remove_provider_from_file(path: Path, provider_id: str) -> dict[str, Any]:
    """Disable a provider by writing ``null`` (also disables built-in presets)."""
    raw = read_config_file(path)
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers[provider_id] = None
    raw["providers"] = providers
    write_config_file(path, raw)
    return raw


def set_default_model_in_file(path: Path, spec: str | None) -> dict[str, Any]:
    raw = read_config_file(path)
    raw["default_model"] = spec
    write_config_file(path, raw)
    return raw


def provider_config_dict(provider: ProviderConfig) -> dict[str, Any]:
    """Serialise a provider back to config-file shape (includes the inline key).

    Only non-default fields are written, so the file stays readable. The
    resulting file is gitignored and blocked by the pre-commit hook.
    """
    data: dict[str, Any] = {"base_url": provider.base_url}
    if provider.dialect != "images":
        data["dialect"] = provider.dialect
    if provider.default_model:
        data["default_model"] = provider.default_model
    if provider.label and provider.label != provider.id:
        data["label"] = provider.label
    if provider.api_key:
        data["api_key"] = provider.api_key
    if provider.api_key_env:
        data["api_key_env"] = provider.api_key_env
    if provider.alt_api_key_envs:
        data["alt_api_key_envs"] = provider.alt_api_key_envs
    if provider.auth_header != "Authorization":
        data["auth_header"] = provider.auth_header
    if provider.auth_scheme != "Bearer":
        data["auth_scheme"] = provider.auth_scheme
    if provider.models_path != "/models":
        data["models_path"] = provider.models_path
    if provider.image_models_path:
        data["image_models_path"] = provider.image_models_path
    if provider.generation_path != "/images/generations":
        data["generation_path"] = provider.generation_path
    if provider.edit_path != "/images/edits":
        data["edit_path"] = provider.edit_path
    if provider.chat_path != "/chat/completions":
        data["chat_path"] = provider.chat_path
    if provider.edit_style != "openai_json":
        data["edit_style"] = provider.edit_style
    if provider.aspect_ratio_mode != "size":
        data["aspect_ratio_mode"] = provider.aspect_ratio_mode
    if provider.size_style != "string":
        data["size_style"] = provider.size_style
    if provider.size_param != "size":
        data["size_param"] = provider.size_param
    if provider.send_response_format:
        data["send_response_format"] = True
    if not provider.chat_modalities:
        data["chat_modalities"] = False
    if provider.include_models:
        data["include_models"] = list(provider.include_models)
    if provider.exclude_models:
        data["exclude_models"] = list(provider.exclude_models)
    if provider.extra_headers:
        data["extra_headers"] = dict(provider.extra_headers)
    if provider.extra_query:
        data["extra_query"] = dict(provider.extra_query)
    if not provider.allow_chat_fallback:
        data["allow_chat_fallback"] = False
    if provider.poll:
        data["poll"] = asdict(provider.poll)
    return data
