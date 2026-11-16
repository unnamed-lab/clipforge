"""Validation for uploaded brand assets.

Nothing uploaded here is trusted: images are checked by content, not extension, and
SVG is parsed with a strict allow-list. SVG is accepted for storage and preview but
not for rendering, because FFmpeg cannot rasterise it — the UI says so instead of
silently dropping the logo.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ElementTree

from clipforge.errors import ValidationFailed

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SVG_BYTES = 1024 * 1024
MAX_DIMENSION = 4096
MAX_AUDIO_BYTES = 40 * 1024 * 1024

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
AUDIO_SUFFIXES = {"mp3", "wav", "m4a", "aac", "ogg", "flac", "opus"}

SVG_ALLOWED_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "defs", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "title", "desc",
}
SVG_ALLOWED_ATTRS = {
    "xmlns", "viewBox", "width", "height", "fill", "fill-rule", "fill-opacity", "stroke",
    "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-opacity", "opacity",
    "d", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "points",
    "transform", "offset", "stop-color", "stop-opacity", "id", "clip-path", "mask",
    "gradientUnits", "preserveAspectRatio", "version", "role", "aria-label",
}
UNSAFE_SVG_PATTERNS = (
    re.compile(r"<\s*script", re.IGNORECASE),
    re.compile(r"<\s*foreignObject", re.IGNORECASE),
    re.compile(r"<!ENTITY", re.IGNORECASE),
    re.compile(r"<\s*!DOCTYPE", re.IGNORECASE),
    re.compile(r"\son[a-z]+\s*=", re.IGNORECASE),
    re.compile(r"javascript:", re.IGNORECASE),
    re.compile(r"data:text/html", re.IGNORECASE),
    re.compile(r"https?://", re.IGNORECASE),
)


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValidationFailed("That file is not a valid PNG image", code="asset_invalid")
    width, height = struct.unpack(">II", data[16:24])
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise ValidationFailed(
            f"Logo dimensions must be between 1 and {MAX_DIMENSION} pixels", code="asset_invalid"
        )
    return int(width), int(height)


def _sanitise_svg(text: str) -> tuple[bytes, int, int]:
    for pattern in UNSAFE_SVG_PATTERNS:
        if pattern.search(text):
            raise ValidationFailed(
                "That SVG contains scripts, external references or unsupported markup",
                code="asset_unsafe_svg",
            )
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValidationFailed("That SVG could not be parsed", code="asset_invalid") from exc

    for element in root.iter():
        tag = element.tag.split("}")[-1]
        if tag not in SVG_ALLOWED_TAGS:
            raise ValidationFailed(f"SVG element <{tag}> is not allowed", code="asset_unsafe_svg")
        for attribute in list(element.attrib):
            name = attribute.split("}")[-1]
            if name.lower().startswith("on") or name not in SVG_ALLOWED_ATTRS:
                del element.attrib[attribute]

    width = int(float(root.get("width", "0") or 0)) if str(root.get("width", "")).replace(".", "").isdigit() else 0
    height = int(float(root.get("height", "0") or 0)) if str(root.get("height", "")).replace(".", "").isdigit() else 0
    if not (width and height):
        viewbox = root.get("viewBox", "")
        parts = viewbox.replace(",", " ").split()
        if len(parts) == 4:
            width, height = int(float(parts[2])), int(float(parts[3]))
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValidationFailed(f"Logo dimensions must not exceed {MAX_DIMENSION} pixels", code="asset_invalid")

    cleaned = ElementTree.tostring(root, encoding="utf-8")
    return cleaned, max(1, width), max(1, height)


def inspect_image(data: bytes, filename: str) -> dict:
    if not data:
        raise ValidationFailed("That file was empty", code="empty_file")
    if data.startswith(PNG_SIGNATURE):
        if len(data) > MAX_IMAGE_BYTES:
            raise ValidationFailed("Logo files must be 10 MB or smaller", code="asset_too_large")
        width, height = _png_size(data)
        return {"mime": "image/png", "suffix": "png", "width": width, "height": height, "renderable": True}

    lowered = (filename or "").lower()
    if lowered.endswith(".svg") or data.lstrip()[:4].lower() == b"<svg":
        if len(data) > MAX_SVG_BYTES:
            raise ValidationFailed("SVG logo files must be 1 MB or smaller", code="asset_too_large")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationFailed("That SVG is not valid UTF-8", code="asset_invalid") from exc
        cleaned, width, height = _sanitise_svg(text)
        return {
            "mime": "image/svg+xml",
            "suffix": "svg",
            "width": width,
            "height": height,
            "renderable": False,
            "data": cleaned,
        }

    raise ValidationFailed(
        "Logos must be a PNG or a safe SVG. Export other formats as PNG first.",
        code="asset_invalid",
    )


def audio_suffix(filename: str) -> str:
    suffix = (filename or "").rsplit(".", 1)[-1].lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValidationFailed(
            "Music must be MP3, WAV, M4A, AAC, OGG, Opus or FLAC.", code="asset_invalid"
        )
    return suffix
