"""Image decoding, MIME/extension detection and on-disk saving.

Providers return images in several shapes: raw base64, ``data:`` URLs,
remote ``http(s)`` URLs, or multibyte payloads with a separate ``media_type``.
Everything funnels through :class:`ImageBlob` and :func:`save_blobs`.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes

MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}

FORMAT_TO_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "svg+xml": "image/svg+xml",
    "avif": "image/avif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
}

DEFAULT_MIME = "image/png"


@dataclass
class ImageBlob:
    """A single image returned by a provider, before it hits disk."""

    data: bytes | None = None
    url: str | None = None
    mime: str | None = None
    b64: str | None = None
    revised_prompt: str | None = None

    def resolved_bytes(self) -> bytes | None:
        if self.data is not None:
            return self.data
        if self.b64:
            return decode_base64(self.b64)
        return None


def normalize_mime(value: str | None) -> str | None:
    """Turn ``png``, ``image/jpg`` or ``IMAGE/PNG`` into a canonical MIME type."""
    if not value:
        return None
    candidate = value.strip().lower().split(";", 1)[0]
    if candidate in FORMAT_TO_MIME:
        return FORMAT_TO_MIME[candidate]
    if "/" in candidate:
        return candidate
    return FORMAT_TO_MIME.get(candidate)


def sniff_mime(data: bytes) -> str | None:
    """Best-effort MIME detection from file magic bytes."""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    head = data[:512].lstrip()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return "image/svg+xml"
    return None


def resolve_mime(data: bytes | None, mime_hint: str | None = None) -> str:
    """Prefer a provider hint, but fall back to magic bytes, then PNG."""
    normalized = normalize_mime(mime_hint)
    sniffed = sniff_mime(data) if data else None
    # Magic bytes win when they disagree with a vague text/plain hint.
    if sniffed and (not normalized or normalized.startswith("text/") or normalized == "application/octet-stream"):
        return sniffed
    return normalized or sniffed or DEFAULT_MIME


def ext_for(data: bytes | None, mime_hint: str | None = None) -> str:
    return MIME_TO_EXT.get(resolve_mime(data, mime_hint), ".png")


def split_data_url(url: str) -> tuple[str | None, str] | None:
    """Split a ``data:`` URL into ``(mime, base64_payload)``."""
    if not url or not url.startswith("data:"):
        return None
    try:
        header, payload = url.split(",", 1)
    except ValueError:
        return None
    meta = header[5:]
    mime = meta.split(";", 1)[0] or None
    if ";base64" in meta.replace(" ", ""):
        return normalize_mime(mime), payload
    return normalize_mime(mime), base64.b64encode(unquote_to_bytes(payload)).decode("ascii")


def decode_base64(value: str | None) -> bytes | None:
    """Decode standard or data-URL base64, tolerating whitespace/padding."""
    if not value:
        return None
    text = value.strip()
    if text.startswith("data:"):
        split = split_data_url(text)
        if split is None:
            return None
        text = split[1]
    text = re.sub(r"\s+", "", text)
    padding = len(text) % 4
    if padding:
        text += "=" * (4 - padding)
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError):
        return None


def to_data_url(data: bytes, mime: str | None = None) -> str:
    resolved = resolve_mime(data, mime)
    return f"data:{resolved};base64,{base64.b64encode(data).decode('ascii')}"


def slugify(text: str, max_len: int = 40) -> str:
    """Turn a prompt into a filesystem-safe filename stem."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", (text or "").lower().strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:max_len] or "image"


def load_image_file(path: str | Path) -> tuple[bytes, str]:
    """Read a local image for use as an edit/reference input."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise FileNotFoundError(f"Input image not found: {target}")
    data = target.read_bytes()
    hinted = mimetypes.guess_type(target.name)[0]
    return data, resolve_mime(data, hinted)


def save_blobs(
    blobs: list[ImageBlob],
    *,
    prompt: str,
    default_dir: str | Path,
    output_path: str | None = None,
    filename_prefix: str | None = None,
) -> list[Path]:
    """Write decoded blobs to disk, honouring the reference's path semantics.

    * a full file path with an extension -> that exact file (multi-image gets
      ``_2``, ``_3`` suffixes)
    * a directory path -> auto-named files inside it
    * ``None`` -> ``default_dir``
    """
    prepared: list[tuple[bytes, str]] = []
    for blob in blobs:
        data = blob.resolved_bytes()
        if data is None:
            continue
        prepared.append((data, ext_for(data, blob.mime)))

    if not prepared:
        raise ValueError("No decodable image data was returned by the provider")

    if output_path:
        target = Path(output_path).expanduser()
        is_dir = (
            output_path.endswith(("/", "\\"))
            or (target.exists() and target.is_dir())
            or target.suffix == ""
        )
    else:
        target = Path(default_dir).expanduser()
        is_dir = True

    written: list[Path] = []

    if is_dir:
        target.mkdir(parents=True, exist_ok=True)
        stem = filename_prefix or slugify(prompt)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        for index, (data, ext) in enumerate(prepared):
            suffix = "" if index == 0 else f"_{index + 1}"
            written.append(_write_unique(target / f"{stem}_{stamp}{suffix}{ext}", data))
        return written

    target.parent.mkdir(parents=True, exist_ok=True)
    base_ext = target.suffix or prepared[0][1]
    for index, (data, ext) in enumerate(prepared):
        if index == 0:
            path = target if target.suffix else target.with_suffix(base_ext)
        else:
            path = target.with_name(f"{target.stem}_{index + 1}{target.suffix or ext}")
        written.append(_write_unique(path, data))
    return written


def _write_unique(path: Path, data: bytes) -> Path:
    """Never silently clobber an existing file."""
    if not path.exists():
        path.write_bytes(data)
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            candidate.write_bytes(data)
            return candidate
        counter += 1
