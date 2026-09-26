"""Request builders and response parsers for the two OpenAI-compatible dialects.

* ``images`` — ``POST /images/generations`` (and ``/images/edits``), the classic
  OpenAI Images API, widely cloned by other vendors.
* ``chat`` — ``POST /chat/completions`` with ``modalities: ["image","text"]``,
  the shape OpenRouter and similar aggregators use for Nano Banana style models.

Responses are normalised into :class:`ImageBlob` objects regardless of how the
provider chose to encode them (raw base64, data URL, or hosted URL).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import ProviderConfig
from .images import ImageBlob, normalize_mime, split_data_url, to_data_url

ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "4:3": "1536x1024",
    "3:4": "1024x1536",
    "5:4": "1280x1024",
    "4:5": "1024x1280",
    "21:9": "1792x1024",
    "9:21": "1024x1792",
    "2:1": "1536x768",
    "1:2": "768x1536",
    "3:1": "1536x512",
    "1:3": "512x1536",
    "1:4": "1024x256",
    "4:1": "256x1024",
    "8:1": "128x1024",
    "1:8": "1024x128",
}


@dataclass
class GenerationRequest:
    provider: ProviderConfig
    model: str
    prompt: str
    mode: str = "generate"  # "generate" | "edit"
    n: int = 1
    aspect_ratio: str | None = None
    size: str | None = None
    quality: str | None = None
    output_format: str | None = None
    background: str | None = None
    seed: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    input_images: list[tuple[bytes, str]] = field(default_factory=list)


@dataclass
class ParseResult:
    blobs: list[ImageBlob] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    text: str | None = None
    raw: Any = None

    @property
    def cost(self) -> float | None:
        return extract_cost(self.usage)


def aspect_to_size(aspect_ratio: str | None) -> str | None:
    if not aspect_ratio:
        return None
    if aspect_ratio in ASPECT_TO_SIZE:
        return ASPECT_TO_SIZE[aspect_ratio]
    try:
        width_text, height_text = aspect_ratio.split(":", 1)
        width, height = float(width_text), float(height_text)
        if width <= 0 or height <= 0:
            return None
        if width >= height:
            return f"{int(round(1024 * width / height))}x1024"
        return f"1024x{int(round(1024 * height / width))}"
    except (ValueError, ZeroDivisionError):
        return None


def _apply_size(body: dict[str, Any], provider: ProviderConfig, size: str | None) -> None:
    if not size or provider.size_style == "none":
        return
    if provider.size_style == "width_height":
        try:
            width_text, height_text = size.lower().split("x", 1)
            body["width"] = int(width_text)
            body["height"] = int(height_text)
            return
        except ValueError:
            pass
    elif provider.size_style == "image_size":
        body["image_size"] = size
        return
    body[provider.size_param] = size


def _apply_common_options(body: dict[str, Any], request: GenerationRequest) -> None:
    if request.quality:
        body["quality"] = request.quality
    if request.output_format:
        body["output_format"] = request.output_format
    if request.background:
        body["background"] = request.background
    if request.seed is not None:
        body["seed"] = request.seed


def build_messages(request: GenerationRequest, *, aspect_in_prompt: bool) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    text = request.prompt
    if aspect_in_prompt and request.aspect_ratio:
        text = f"Aspect ratio: {request.aspect_ratio}. {text}"
    content.append({"type": "text", "text": text})
    for data, mime in request.input_images:
        content.append({"type": "image_url", "image_url": {"url": to_data_url(data, mime)}})
    return [{"role": "user", "content": content}]


def build_chat_body(request: GenerationRequest) -> dict[str, Any]:
    provider = request.provider
    body: dict[str, Any] = {
        "model": request.model,
        "messages": build_messages(request, aspect_in_prompt=provider.aspect_ratio_mode == "prompt"),
    }
    if provider.chat_modalities and request.mode == "generate":
        body["modalities"] = ["image", "text"]
    if request.n and request.n > 1:
        body["n"] = request.n
    if request.aspect_ratio and provider.aspect_ratio_mode == "field":
        body["aspect_ratio"] = request.aspect_ratio
    body.update(request.extra_body)
    return body


def build_images_body(request: GenerationRequest) -> dict[str, Any]:
    provider = request.provider
    body: dict[str, Any] = {"model": request.model, "prompt": request.prompt}

    if request.n and request.n > 1:
        body["n"] = request.n

    size = request.size
    if not size and request.aspect_ratio and provider.aspect_ratio_mode == "size":
        size = aspect_to_size(request.aspect_ratio)
    _apply_size(body, provider, size)

    if request.aspect_ratio and provider.aspect_ratio_mode == "field":
        body["aspect_ratio"] = request.aspect_ratio

    _apply_common_options(body, request)

    if provider.send_response_format or "dall-e" in request.model.lower():
        body["response_format"] = "b64_json"

    body.update(request.extra_body)
    return body


def build_edit_body(request: GenerationRequest) -> dict[str, Any]:
    provider = request.provider
    if provider.edit_style == "xai":
        body: dict[str, Any] = {"model": request.model, "prompt": request.prompt}
        if len(request.input_images) == 1:
            data, mime = request.input_images[0]
            body["image"] = {"url": to_data_url(data, mime), "type": "image_url"}
        else:
            body["images"] = [
                {"url": to_data_url(data, mime), "type": "image_url"} for data, mime in request.input_images
            ]
        if request.n and request.n > 1:
            body["n"] = request.n
        _apply_common_options(body, request)
        body.update(request.extra_body)
        return body

    # OpenAI-compatible JSON edit shape.
    body = {
        "model": request.model,
        "prompt": request.prompt,
        "images": [{"image_url": to_data_url(data, mime)} for data, mime in request.input_images],
    }
    if request.n and request.n > 1:
        body["n"] = request.n
    size = request.size
    if not size and request.aspect_ratio and provider.aspect_ratio_mode == "size":
        size = aspect_to_size(request.aspect_ratio)
    _apply_size(body, provider, size)
    _apply_common_options(body, request)
    body.update(request.extra_body)
    return body


def build_request_body(request: GenerationRequest) -> dict[str, Any]:
    """Pick the right body shape for the request's mode and provider dialect."""
    if request.mode == "edit":
        if request.provider.edit_style == "chat" or request.provider.edit_path is None:
            return build_chat_body(request)
        return build_edit_body(request)
    if request.provider.dialect == "chat":
        return build_chat_body(request)
    return build_images_body(request)


def request_path(request: GenerationRequest) -> str:
    """The provider-relative path to POST to."""
    provider = request.provider
    if request.mode == "edit":
        if provider.edit_style == "chat" or provider.edit_path is None:
            return provider.chat_path
        return provider.edit_path
    if provider.dialect == "chat":
        return provider.chat_path
    return provider.generation_path


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _string_to_blob(value: str) -> ImageBlob | None:
    if not value:
        return None
    if value.startswith("data:"):
        split = split_data_url(value)
        if split is None:
            return None
        mime, b64 = split
        return ImageBlob(b64=b64, mime=mime)
    if value.startswith(("http://", "https://")):
        return ImageBlob(url=value)
    return ImageBlob(b64=value)


def _entry_to_blob(entry: Any) -> ImageBlob | None:
    if entry is None:
        return None
    if isinstance(entry, str):
        return _string_to_blob(entry)
    if not isinstance(entry, dict):
        return None

    mime = normalize_mime(_first_str(entry, "media_type", "mime_type", "mime", "content_type", "output_format"))
    blob = ImageBlob(mime=mime, revised_prompt=_first_str(entry, "revised_prompt"))

    for key in ("b64_json", "b64", "base64", "image_base64", "image_b64", "image_data", "data"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            if value.startswith("data:"):
                split = split_data_url(value)
                if split:
                    blob.b64 = split[1]
                    blob.mime = split[0] or blob.mime
            elif value.startswith(("http://", "https://")):
                blob.url = value
            else:
                blob.b64 = value
            break

    if blob.b64 is None and blob.url is None:
        for key in ("url", "image_url", "image", "output"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                blob.url = value
                break
            if isinstance(value, dict):
                inner = value.get("url")
                if isinstance(inner, str) and inner:
                    blob.url = inner
                    break

    if blob.b64 is None and blob.url is None:
        return None
    return blob


def parse_images_response(payload: Any) -> ParseResult:
    blobs: list[ImageBlob] = []
    usage: dict[str, Any] | None = None
    text: str | None = None

    if isinstance(payload, dict):
        if isinstance(payload.get("usage"), dict):
            usage = payload["usage"]
        text = _first_str(payload, "revised_prompt")
        for key in ("data", "output", "images", "artifacts", "results"):
            sequence = payload.get(key)
            if isinstance(sequence, list):
                for entry in sequence:
                    blob = _entry_to_blob(entry)
                    if blob:
                        blobs.append(blob)
                if blobs:
                    break
        if not blobs:
            blob = _entry_to_blob(payload)
            if blob:
                blobs.append(blob)
    elif isinstance(payload, list):
        for entry in payload:
            blob = _entry_to_blob(entry)
            if blob:
                blobs.append(blob)

    return ParseResult(blobs=blobs, usage=usage, text=text, raw=payload)


def parse_chat_response(payload: Any) -> ParseResult:
    blobs: list[ImageBlob] = []
    usage: dict[str, Any] | None = None
    text: str | None = None

    if isinstance(payload, dict):
        if isinstance(payload.get("usage"), dict):
            usage = payload["usage"]
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else {}
            for image in message.get("images") or []:
                if not isinstance(image, dict):
                    continue
                url = image.get("image_url") or image.get("url")
                if isinstance(url, dict):
                    url = url.get("url")
                blob = _string_to_blob(url) if isinstance(url, str) else None
                if blob:
                    blobs.append(blob)

            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in ("image_url", "output_image", "image"):
                        url = part.get("image_url") or part.get("url") or part.get("image")
                        if isinstance(url, dict):
                            url = url.get("url")
                        blob = _string_to_blob(url) if isinstance(url, str) else None
                        if blob:
                            blobs.append(blob)
                    elif part.get("type") == "text" and isinstance(part.get("text"), str):
                        text = (text or "") + part["text"]

    if not blobs:
        fallback = parse_images_response(payload)
        blobs = fallback.blobs
        text = text or fallback.text
        usage = usage or fallback.usage

    return ParseResult(blobs=blobs, usage=usage, text=text, raw=payload)


def parse_response(payload: Any, dialect: str) -> ParseResult:
    if dialect == "chat":
        return parse_chat_response(payload)
    return parse_images_response(payload)


def extract_text(payload: Any) -> str | None:
    """Best-effort assistant text, used to explain 'no images' responses."""
    result = parse_chat_response(payload) if isinstance(payload, dict) and payload.get("choices") else None
    if result and result.text:
        return result.text
    if isinstance(payload, dict):
        for key in ("message", "detail", "error", "revised_prompt", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                inner = value.get("message")
                if isinstance(inner, str):
                    return inner
    return None


def extract_cost(usage: dict[str, Any] | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    for key in ("cost", "total_cost", "price", "cost_usd"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None
