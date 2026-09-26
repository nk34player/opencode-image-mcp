---
name: image-generation
description: Use when the user wants to generate, create, draw, illustrate, render, or edit an image. Covers picking the right model/provider, writing good prompts for image models, choosing aspect ratios and sizes, and using the opencode-image MCP tools.
---

# Image generation

Use the `opencode-image` MCP server to generate and edit images through any
configured OpenAI-compatible provider.

## Workflow

1. **Check what is available** — call `list_providers`, then `list_models`
   (only if you need to discover a model). Skip this if the user named a model.
2. **Pick a model** — prefer an explicit `provider:model` spec.
3. **Generate** — call `generate_image` with a concrete prompt.
4. **Report back** — tell the user where the file was saved. Set
   `return_image_content=true` when the user wants to *see* the result in chat.

## Choosing a model

There is no universal "best". Match the model family to the job:

| Need | Prefer |
| --- | --- |
| Fast drafts, many iterations, cheap | `flash`-class / `*-schnell` / `gpt-mini`-class models |
| Everyday default, balanced | a mid-tier general image model (e.g. Gemini Flash Image) |
| Text rendered *inside* the image | Gemini/Nano Banana family — OpenAI models often distort small text |
| Photoreal hero asset, maximum fidelity | top-tier OpenAI `gpt-image-*` or Gemini Pro image |
| Multiple variations in one call | models that accept `n > 1` |
| Editing / conditioning on a photo | any model that accepts image input (pass `input_image_path`) |
| Vector/SVG output | Recraft-class vector models, with `output_format="svg"` |

Practical guidance:

- Prefer the exact `full_id` returned by `list_models` — bare ids may be
  ambiguous across providers.
- `capabilities.supported_parameters` (when present) tells you whether a model
  accepts `resolution`, `aspect_ratio`, `n`, `seed`, etc. Respect it; do not
  send parameters a model does not list.
- `pricing` (when present) lets you pick the cheapest option for a draft.

## Writing prompts

- Be **concrete**: subject, style, lighting, composition, camera/lens, mood.
  Vague prompts return generic stock imagery.
- Name the medium ("editorial photograph", "flat vector illustration",
  "watercolor") rather than hoping the model infers it.
- For text in images, put the exact string in quotes and say it is the only
  text: `a neon sign reading exactly "OpenRouter"`.
- Avoid "no text" / "no watermark" — it sometimes triggers watermark behaviour.
  Say "clean composition, no captions, no labels" instead.
- For consistent characters across calls, pass the previous output as
  `input_image_path` with an explicit instruction ("same character, new pose").

## Aspect ratio and size

- `aspect_ratio` is portable; let the server map it to the provider's native
  parameter. Use `size` only when you need exact pixels.
- Common mappings: article hero `16:9`; social square `1:1`; mobile/story `9:16`;
  cinematic banner `21:9`; portrait print `2:3` or `4:5`.
- Higher resolution costs more and is slower. Start small, then re-render the
  final choice larger.

## Editing

Pass `input_image_path` (and `input_image_paths` for multiple references). The
`prompt` becomes an edit instruction:

- "remove the background, keep just the subject"
- "render this as a pencil sketch with detailed shading"
- "same character, now standing in the rain at night"

Not every provider supports editing; check `list_providers`
(`supports_edit`) or fall back to a provider that does.

## Common pitfalls

- **`returned no image data`** — the model replied with text instead of an
  image (refusal or rambling). Rephrase more concretely, or switch model.
- **`authentication failed`** — the provider's key env var is missing or
  invalid. Tell the user which env var; do not retry blindly.
- **`429`** — the server already retried with backoff. If it still fails, wait
  or switch provider.
- **Wrong-looking results** — you probably asked a text-centric model to draw.
  Switch to a dedicated image model.
- **Cost** — the result includes `usage`/`cost_usd` when the provider reports
  it. Mention large costs, and prefer cheaper models for iterations.

## Request → call mapping

| User says | Do this |
| --- | --- |
| "Make me a picture of X" | `generate_image(prompt="X")` with the default model |
| "…in 16:9" | add `aspect_ratio="16:9"` |
| "…using gpt-image" | `model="openai:gpt-image-1"` (or the exact id from `list_models`) |
| "…give me 4 options" | `n=4` if the model supports it |
| "edit this photo: …" | `input_image_path="…"` with the instruction as `prompt` |
| "let me see it" | `return_image_content=true` |
| "just save it here" | `output_path="./out/hero.png"` |
