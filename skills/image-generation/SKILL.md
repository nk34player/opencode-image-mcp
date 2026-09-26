---
name: image-generation
description: Use when the user wants to generate, create, draw, illustrate, render, or edit an image, or when they give you an OpenAI-compatible image endpoint (a base URL and API key) to set up and reuse. Covers registering custom endpoints, picking the right model, writing prompts for image models, choosing aspect ratios, and using the opencode-image MCP tools.
---

# Image generation

Use the `opencode-image` MCP server to generate and edit images through any
OpenAI-compatible provider — including endpoints the user supplies.

## The main flow: bring your own endpoint

When the user gives you a URL, **register it** — do not ask them to pick a
preset, and do not assume a specific vendor.

1. **Register** it:

   ```
   add_provider(id="example", base_url="https://api.example.com/v1", api_key="sk-...")
   ```

   - `id` is a short lowercase handle they'll use later (letters, digits, `-`, `_`).
   - Prefer `api_key_env="SOME_ENV_VAR"` over `api_key` if the user would rather
     not store the secret on disk. Use neither for local endpoints that need no auth.
   - `dialect="auto"` (default) is right almost always; it uses the Images API
     and falls back to chat-completions automatically.

2. **Read the result and pick the model**:
   - `models` has exactly one entry → it is already the default. Done.
   - `models` has several → choose the best image model and persist it:
     `set_default_model("example:<model-id>")`.
   - `models` is empty → the endpoint likely has no `/models` route. Call
     `add_provider` again with `default_model="<id>"` and `discover=false`, or
     just `set_default_model("example:<id>")` if you already know the id.

3. **Generate** — `generate_image(prompt="...")` with no model now uses it.

Example exchange:

> User: "use https://api.example.com/v1 with key sk-abc, that's my image server"

```
add_provider(id="example", base_url="https://api.example.com/v1", api_key="sk-abc")
→ models: ["flux-schnell", "flux-pro"], default_model: null
set_default_model("example:flux-pro")
generate_image(prompt="a corgi astronaut, editorial photo, 16:9", aspect_ratio="16:9")
```

The endpoint is saved to a gitignored config file, so **future sessions already
know it** — you only register once. If they later say "use my image server",
check `list_providers` before registering anything new.

## General workflow

1. **See what exists** — `list_providers`. If nothing is configured and the user
   has not given you a URL, ask for one (or for a key env var).
2. **Register a new endpoint** if the user named one (see above).
3. **Discover models** — `list_models(provider="example")` when you need to see
   options; `refresh=true` to bypass the cache.
4. **Pick a model** — prefer an explicit `example:model-id` spec.
5. **Generate** — call `generate_image` with a concrete prompt.
6. **Report back** — give the file path. Use `return_image_content=true` when
   the user wants to *see* the result in chat.

## Choosing a model

Match the model family to the job:

| Need | Prefer |
| --- | --- |
| Fast drafts, many iterations, cheap | `*-schnell` / `*-turbo` / `*-mini` / `flash` class |
| Everyday default | a mid-tier general image model |
| Text rendered *inside* the image | Gemini/Nano Banana family — many others distort small text |
| Photoreal hero asset, maximum fidelity | top-tier `gpt-image-*` or Pro image models |
| Multiple variations in one call | models that accept `n > 1` |
| Editing / conditioning on a photo | any model that accepts image input (`input_image_path`) |
| Vector/SVG output | Recraft-class vector models, with `output_format="svg"` |

- Use the exact `full_id` returned by `list_models` — bare ids can be ambiguous.
- `capabilities.supported_parameters` (when present) says which knobs a model
  accepts. Do not send parameters it does not list.
- `pricing` (when present) helps you pick the cheapest option for drafts.

## Writing prompts

- Be **concrete**: subject, style, lighting, composition, lens, mood. Vague
  prompts return generic stock imagery.
- Name the medium ("editorial photograph", "flat vector illustration",
  "watercolor") instead of hoping the model infers it.
- For text in images, quote the exact string and say it is the only text:
  `a neon sign reading exactly "OpenRouter"`.
- Avoid "no text" / "no watermark" — it can trigger watermark behaviour. Say
  "clean composition, no captions, no labels" instead.
- For consistent characters, pass the previous output as `input_image_path`
  with an explicit instruction ("same character, new pose: …").

## Aspect ratio and size

- `aspect_ratio` is portable; let the server map it to the provider's native
  parameter. Use `size` only when you need exact pixels.
- Common mappings: article hero `16:9`; social square `1:1`; story/mobile `9:16`;
  cinematic banner `21:9`; portrait print `2:3` or `4:5`.
- Higher resolution costs more and is slower. Start small, then re-render the
  final choice larger.

## Editing

Pass `input_image_path` (and `input_image_paths` for multiple references); the
`prompt` becomes an edit instruction:

- "remove the background, keep just the subject"
- "render this as a pencil sketch with detailed shading"
- "same character, now standing in the rain at night"

Check `list_providers` → `supports_edit`, or fall back to a provider that does.

## Common pitfalls

- **`returned no image data`** — the model replied with text (refusal or
  rambling). Rephrase more concretely or switch model.
- **`authentication failed`** — wrong/missing key for that endpoint. Fix the key
  via `api_key`/`api_key_env`; do not retry blindly.
- **`the provider rejected this endpoint`** — the model may use the other
  dialect. `auto` already falls back for 404/405; try another model otherwise.
- **`429`** — the server already retried with backoff. Wait, or switch provider.
- **`Unknown provider`** — you used a provider id that is not registered. Call
  `add_provider` first, or `list_providers` to see valid ids.
- **Never echo the API key** back to the user or into notes. It is stored only
  by `add_provider` in the gitignored config file.
- **Cost** — the result includes `usage`/`cost_usd` when reported. Mention large
  costs, and prefer cheap models for iterations.

## Request → call mapping

| User says | Do this |
| --- | --- |
| "use https://api.example.com/v1 with key X" | `add_provider(id="example", base_url="…", api_key="X")`, then `set_default_model` if several models |
| "use my image server" (already set up) | `list_providers`, then `generate_image` |
| "Make me a picture of X" | `generate_image(prompt="X")` |
| "…in 16:9" | add `aspect_ratio="16:9"` |
| "…with model Y" | `generate_image(model="example:Y")` |
| "…give me 4 options" | `n=4` if the model supports it |
| "edit this photo: …" | `input_image_path="…"`, instruction as `prompt` |
| "let me see it" | `return_image_content=true` |
| "just save it here" | `output_path="./out/hero.png"` |
| "forget that endpoint" | `remove_provider(id="example")` |
