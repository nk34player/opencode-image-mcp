# opencode-image-mcp

A **multi-provider image generation MCP server**. Point it at any
OpenAI-compatible API, let it **auto-detect the image models** each provider
offers, **pick the model you ask for**, then **generate and wait** for the
result — saving the image to disk and returning its path (optionally inline).

It is a generalized, hardened take on the popular single-provider
`openrouter-image-mcp`: instead of one hardcoded vendor, it speaks both common
OpenAI-compatible dialects and works with OpenAI, OpenRouter, Google Gemini,
xAI, Together, DeepInfra, SiliconFlow, Fireworks, or any custom endpoint you
configure.

---

## Bring your own endpoint

**No preset required.** Hand the agent a base URL and it registers the
endpoint, discovers its models, and remembers it:

> Use `https://api.example.com/v1` with key `sk-abc123` and call it `example`.

The agent calls `add_provider`, which:

1. probes `https://api.example.com/v1/models`,
2. keeps only the models that can produce images,
3. saves the endpoint (and key) to the gitignored config file,
4. sets that model as the default.

From then on, in this session **and every future run**:

> Make me a corgi astronaut in 16:9.

...resolves straight to your endpoint. If the endpoint exposes several image
models, the agent inspects the list and calls `set_default_model` to pick one.
Endpoints without a `/models` route still work — pass `default_model` and
`discover=false`.

---

## Features

- **Any OpenAI-compatible provider** — built-in presets plus arbitrary custom
  base URLs.
- **Two API dialects, auto-selected** — the classic Images API
  (`/images/generations`, `/images/edits`) and the chat-completions
  `modalities: ["image","text"]` style used by aggregators. Includes automatic
  chat fallback when a provider rejects the Images endpoint.
- **Model auto-discovery** — queries each provider's model endpoint, filters to
  image-capable models, and caches the result on disk. OpenRouter's dedicated
  `/images/models` capability metadata is used when available.
- **Flexible model selection** — `provider:model`, bare model ids searched
  across providers, or user-defined aliases.
- **Waits for slow jobs** — long timeouts, retry with backoff on `429`/`5xx`
  (honouring `Retry-After`), and generic polling for providers that return a
  job id instead of an image.
- **Robust output handling** — base64, data URLs and hosted URLs; MIME/extension
  detection from magic bytes; multi-image `_2`/`_3` suffixes; never clobbers
  existing files.
- **Friendly errors** — 401/402/404/413/429/5xx and timeouts all become clear,
  actionable messages naming the provider and model.
- **Inline previews on demand** — return file paths by default, or embed the
  generated image in the tool result so the model can see it.
- **Small footprint** — Python + the MCP SDK + `httpx`. Nothing else.

---

## Supported providers (optional presets)

These presets are just convenient shortcuts — none of them are required, and
the custom-endpoint flow above works with any OpenAI-compatible API.

| Provider | Dialect | Model list endpoint | Notes |
| --- | --- | --- | --- |
| `openai` | images | `/models` | `gpt-image-*`, `dall-e-*` |
| `openrouter` | images + chat | `/images/models` | capability-aware; Gemini/GPT/Flux/Seedream via one key |
| `gemini` | images | `/models` | Google's OpenAI-compatible endpoint |
| `xai` | images | `/models` | Grok Imagine; returns hosted URLs |
| `together` | images | `/models` | FLUX and friends (`width`/`height`) |
| `deepinfra` | images | `/models` | FLUX (`width`/`height`) |
| `siliconflow` | images | `/models` | `image_size` param |
| `fireworks` | images | `/models` | `width`/`height` |
| `custom` | any | configurable | LM Studio, vLLM, ComfyUI gateway, proxies… |

A provider becomes active the moment its API key env var is set. Unconfigured
presets are listed as `configured: false` and skipped.

---

## Install

Requires Python 3.10+ and (recommended) [`uv`](https://astral.sh/uv).

### From a git URL

```bash
uv tool install --from git+https://github.com/you/opencode-image-mcp opencode-image-mcp
```

### From a local checkout

```bash
git clone https://github.com/you/opencode-image-mcp
cd opencode-image-mcp
uv tool install --reinstall --from . opencode-image-mcp
```

Two ways to launch it:

- the console script `opencode-image-mcp`
- the module entry point `python -m opencode_image_mcp` (more reliable on
  Windows, where the generated `.exe` launcher can miss the venv's
  site-packages)

Find the tool's venv Python with `uv tool dir`, then use it in your client
config as shown below.

### Run from source without installing

```bash
uv venv
uv pip install -e ".[dev]"
python -m opencode_image_mcp
```

---

## Configure

### 1. API keys (environment variables)

Set any subset; each one enables that provider:

```bash
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-v1-...
GEMINI_API_KEY=...            # GOOGLE_API_KEY also accepted
XAI_API_KEY=xai-...
TOGETHER_API_KEY=...
DEEPINFRA_API_KEY=...
SILICONFLOW_API_KEY=...
FIREWORKS_API_KEY=...
```

### 2. Server settings (optional)

| Variable | Default | Purpose |
| --- | --- | --- |
| `OUTPUT_DIR` (or `IMAGE_MCP_OUTPUT_DIR`) | `~/opencode-images` | Default save directory |
| `IMAGE_MCP_TIMEOUT` | `300` | Per-request timeout in seconds |
| `IMAGE_MCP_RETRIES` | `3` | Retry attempts for `429`/`5xx`/network errors |
| `IMAGE_MCP_MODEL_TTL` | `86400` | Model-list cache TTL in seconds |
| `IMAGE_MCP_CACHE_DIR` | `~/.cache/opencode-image-mcp` | Where the model cache lives |
| `IMAGE_MCP_CONFIG` | `./providers.json` or `~/.config/opencode-image-mcp/providers.json` | Extra config file |
| `IMAGE_MCP_BASE_URL` / `IMAGE_MCP_API_KEY` / `IMAGE_MCP_MODEL` | — | Configure the `custom` provider |
| `OPENAI_BASE_URL` | — | Point the `openai` preset at a compatible proxy |
| `LOG_LEVEL` (or `IMAGE_MCP_LOG_LEVEL`) | `INFO` | Logging level (logs go to stderr) |

### 3. Custom providers, aliases and defaults (`providers.json`)

Copy `providers.example.json` and edit. Only the keys you set are changed.

```json
{
  "default_model": "openrouter:google/gemini-3.1-flash-image-preview",
  "aliases": {
    "nb2": "openrouter:google/gemini-3.1-flash-image-preview",
    "fast": "gemini:gemini-2.5-flash-image"
  },
  "settings": { "output_dir": "./generated", "timeout_s": 300 },
  "providers": {
    "local": {
      "base_url": "http://localhost:8000/v1",
      "dialect": "images",
      "api_key_env": null,
      "default_model": "flux-schnell",
      "include_models": ["flux", "sdxl"],
      "aspect_ratio_mode": "size"
    },
    "comfy": {
      "base_url": "http://localhost:8188/openai/v1",
      "dialect": "chat",
      "default_model": "comfy-image",
      "poll": {
        "url_template": "{base_url}/jobs/{id}",
        "status_field": "status",
        "done_values": ["completed", "done", "succeeded"],
        "failed_values": ["failed", "error", "cancelled"],
        "result_url_field": "result_url",
        "interval_s": 2,
        "max_interval_s": 10
      }
    },
    "together": null
  }
}
```

Set a provider to `null` to disable a preset. Setting `api_key_env` to `null`
marks a provider as anonymous (no key required) — handy for local servers.

**Where runtime additions are stored:** `add_provider` writes to
`IMAGE_MCP_CONFIG` when set, otherwise `~/.config/opencode-image-mcp/providers.json`.
A key passed to `add_provider` is stored inline in that file. It is gitignored
and blocked by the pre-commit hook, so it never reaches the cloud — but keep the
file out of shared/backup folders if you would rather the key not sit on disk.

**Provider keys:** `base_url`, `dialect`, `default_model`, `label`, `api_key`,
`api_key_env`, `alt_api_key_envs`, `auth_header`, `auth_scheme`, `models_path`,
`image_models_path`, `generation_path`, `edit_path`, `chat_path`, `edit_style`,
`aspect_ratio_mode`, `size_style`, `size_param`, `send_response_format`,
`chat_modalities`, `include_models`, `exclude_models`, `extra_headers`,
`extra_query`, `allow_chat_fallback`, `poll`. Unknown keys are rejected so typos
surface immediately.

| Field | Meaning |
| --- | --- |
| `dialect` | `images` (default) or `chat` |
| `edit_style` | `openai_json`, `xai`, or `chat` |
| `aspect_ratio_mode` | `size` (derive WxH), `field` (send `aspect_ratio`), `prompt` (prefix the prompt) |
| `size_style` | `string` (`size`), `width_height`, `image_size`, `none` |
| `include_models` / `exclude_models` | regex allow/deny applied to model ids |
| `auth_header` / `auth_scheme` | e.g. Azure-style `api-key` with an empty scheme |
| `extra_query` | e.g. `{"api-version": "2024-02-01"}` |
| `poll` | enables waiting for async job APIs |

---

## Wire it into your MCP client

Replace the path with the tool venv's Python (`uv tool dir` → `.../Scripts/python.exe`
on Windows, `.../bin/python` elsewhere).

### opencode / Claude Code (`~/.claude.json`)

```json
{
  "mcpServers": {
    "opencode-image": {
      "command": "C:/Users/YOU/AppData/Roaming/uv/tools/opencode-image-mcp/Scripts/python.exe",
      "args": ["-m", "opencode_image_mcp"],
      "env": {
        "OPENROUTER_API_KEY": "sk-or-v1-...",
        "OUTPUT_DIR": "C:/Users/YOU/Pictures/generated"
      }
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "opencode-image": {
      "command": "/Users/YOU/.local/share/uv/tools/opencode-image-mcp/bin/python",
      "args": ["-m", "opencode_image_mcp"],
      "env": { "OPENAI_API_KEY": "sk-..." }
    }
  }
}
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "opencode-image": {
      "command": "/path/to/uv/tools/opencode-image-mcp/bin/python",
      "args": ["-m", "opencode_image_mcp"],
      "env": { "GEMINI_API_KEY": "..." }
    }
  }
}
```

### Generic stdio client

Set the env vars in the parent shell and run `python -m opencode_image_mcp`.

---

## Tools

### `list_providers`

No arguments. Returns every known provider with `configured`, `key_env`,
`dialect`, `default_model`, `supports_edit` and `polls_async_jobs`, plus the
effective default model and aliases.

### `list_models(provider=None, refresh=False)`

Discovers image models from configured providers, cached on disk. Each entry
has `provider`, `id`, `full_id`, `name`, `source` and, when the provider exposes
them, `capabilities` and `pricing`. Errors per provider are returned under
`errors`. Use `refresh=true` to bypass the cache.

### `add_provider(...)`

Register **any** OpenAI-compatible image endpoint at runtime. It probes
`{base_url}/models`, filters to image models, persists the provider to the
config file, and makes it usable immediately and in future sessions.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `id` | string | *required* | Short handle for `provider:model` specs, e.g. `example` |
| `base_url` | string | *required* | API root, e.g. `https://api.example.com/v1` |
| `api_key` | string | `null` | Bearer token, stored in the gitignored config file |
| `api_key_env` | string | `null` | Read the key from an env var instead (ignored if `api_key` is set) |
| `default_model` | string | `null` | Model to use by default; auto-picked if exactly one image model is found |
| `dialect` | string | `auto` | `auto`, `images` (`/images/generations`) or `chat` (`/chat/completions` + image modalities) |
| `make_default` | bool | `false` | Also set this as the global default model |
| `extra_headers` | object | `null` | Extra request headers |
| `include_models` / `exclude_models` | string[] | `null` | Regex allow/deny applied to discovered model ids |
| `aspect_ratio_mode` | string | `size` / `prompt` | `size`, `field` or `prompt` |
| `size_style` | string | `string` | `string`, `width_height`, `image_size` or `none` |
| `discover` | bool | `true` | Set `false` for endpoints without a `/models` route |

Returns the discovered `models`, the chosen `default_model`, `key_source`,
`warnings` and the `saved_to` path. The key is never echoed back.

### `set_default_model(model)`

Persist the model used when `generate_image` is called without one, e.g.
`example:flux-schnell`. Saved to the config file, so it applies to future runs.

### `remove_provider(id)`

Remove a provider (including a built-in preset) from the config file. Clears the
default model if it pointed at that provider.

### `generate_image(...)`

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `prompt` | string | *required* | Image description, or an edit instruction with input images |
| `model` | string | config default | `provider:model`, bare id, or alias |
| `aspect_ratio` | string | `null` | e.g. `1:1`, `16:9`, `9:16`, `21:9` — mapped per provider |
| `size` | string | `null` | Explicit size, e.g. `1536x1024` |
| `quality` | string | `null` | `low`/`medium`/`high`/`auto` (where supported) |
| `n` | int | `1` | Number of images |
| `output_path` | string | `null` | Exact file, a directory, or omitted for `OUTPUT_DIR` |
| `filename_prefix` | string | `null` | Stem for auto-generated names |
| `input_image_path` | string | `null` | One reference image → edit mode |
| `input_image_paths` | string[] | `null` | Multiple references (where supported) |
| `output_format` | string | `null` | `png`/`jpeg`/`webp` (where supported) |
| `background` | string | `null` | `transparent`/`opaque`/`auto` (where supported) |
| `seed` | int | `null` | Deterministic generation (where supported) |
| `return_image_content` | bool | `false` | Also embed the image in the result |
| `timeout_s` | float | `null` | Override the configured timeout |
| `extra_body` | object | `null` | Escape hatch: merged into the request body |

**Return shape** (also available as `structured_content`):

```json
{
  "file_paths": ["C:/.../corgi_20260926_120000.png"],
  "provider": "openrouter",
  "model_id": "google/gemini-3.1-flash-image-preview",
  "dialect": "images",
  "mode": "generate",
  "prompt": "a corgi astronaut",
  "aspect_ratio": "16:9",
  "image_count": 1,
  "elapsed_ms": 8421,
  "usage": { "prompt_tokens": 22, "completion_tokens": 1500, "cost": 0.06834 },
  "cost_usd": 0.06834
}
```

**Example prompts**

> Generate a cinematic corgi astronaut in 16:9 using `openrouter:google/gemini-3.1-flash-image-preview`.

> List the image models my providers offer, then use the cheapest one.

> Take `./photo.jpg` and remove the background, save to `./cutout.png`.

---

## How model selection works

1. `provider:model` — explicit and unambiguous. No discovery needed.
2. **Bare model id** — the server searches every configured provider's cached
   catalogue (exact, then case-insensitive, then suffix match). If several
   providers match, it lists the candidates and asks you to pick one.
3. **Alias** — any key from `aliases` in `providers.json`.
4. **Omitted** — `default_model` from config, else the first configured
   provider's default.

Discovery uses OpenRouter's authoritative `/images/models` capability metadata
when available; otherwise it filters model ids by image-family heuristics
(`flux`, `dall-e`, `gpt-image`, `seedream`, `imagen`, `grok-imagine`,
`qwen-image`, …) refined by your `include_models`/`exclude_models` regexes.

---

## Waiting for the response

- Requests use a generous timeout (default 300s) — image generation is slow.
- `429`/`5xx` and network errors are retried with exponential backoff and
  jitter, honouring `Retry-After`.
- If a provider answers with a **job id instead of an image**, configure `poll`
  and the server will poll until the job completes, then fetch the result
  (inline or via `result_url`).

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `configured_count: 0` | No API keys visible. Set them in the client's `env` block, not just your shell. |
| `authentication failed (401)` | Wrong/expired key for that provider; check the named env var. |
| `request rejected (HTTP 400)` | Unsupported parameter for that model — drop `quality`/`size`/`output_format`, or use `extra_body`. |
| `the provider rejected this endpoint` | Model uses the other dialect; try another model/provider (chat fallback is automatic where possible). |
| `returned no image data` | The model answered with text (refusal or chatter). Rephrase or switch model. |
| Wrong extension | Never happens by design — extensions come from magic bytes, not the URL. |
| `ModuleNotFoundError` on the Windows `.exe` launcher | Use `python -m opencode_image_mcp` with the venv Python, as in the configs above. |

---

## Development

The test suite is **local only** — `tests/` is gitignored and never committed.

```bash
uv venv
uv pip install -e ".[dev]"
python -m pytest
```

Layout:

```
src/opencode_image_mcp/
  config.py     # presets, providers.json read/write, env, Settings
  registry.py   # model discovery, cache, resolution
  dialects.py   # images/chat request builders + response parsers
  transport.py  # httpx client, retries/backoff, downloads, polling
  images.py     # MIME sniffing, decode, save
  errors.py     # friendly error mapping
  server.py     # MCP tools
```

The tests mock HTTP with `httpx.MockTransport`, so they run offline and cost
nothing.

---

## Repository safety (keeping keys out of the cloud)

`.env` and `providers.json` are gitignored, so real keys and custom endpoints
never get committed. That is backed by a pre-commit hook in `.githooks/` which
also catches `git add -f` and any staged diff that looks like a live API key.

The `tests/` directory is gitignored too: it stays on your machine for local
runs and is never committed.

The hooks path is local git config, so enable it once per clone:

```bash
git config core.hooksPath .githooks
```

Then verify:

```bash
git check-ignore -v .env providers.json   # both should be listed
```

Bypass only if you are certain: `git commit --no-verify`.

---

## Companion skill

`skills/image-generation/SKILL.md` is an optional agent skill with a model
selection decision tree, prompt-engineering tips, aspect-ratio guidance and
common pitfalls. Install it by copying the folder into your client's skills
directory.

---

## License

MIT — see [LICENSE](LICENSE).
