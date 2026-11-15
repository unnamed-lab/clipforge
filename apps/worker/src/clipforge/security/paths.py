"""Filesystem safety helpers.

Every path handed to FFmpeg, yt-dlp or the HTTP layer is derived from a generated
identifier inside a project workspace and then re-validated against that workspace
root. User-supplied strings are only ever stored as display metadata.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from clipforge.errors import ValidationFailed

_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def display_name(raw: str | None, *, fallback: str = "Untitled") -> str:
    """Normalise a user-supplied filename for storage *as metadata only*."""
    if not raw:
        return fallback
    cleaned = unicodedata.normalize("NFKC", raw)
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    cleaned = _UNSAFE.sub("_", cleaned).strip(" .")
    if not cleaned:
        return fallback
    stem = Path(cleaned).stem
    if stem.upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:180]


def safe_join(root: Path, *parts: str) -> Path:
    """Join generated path segments and guarantee the result stays under *root*."""
    root_resolved = root.resolve()
    candidate = root_resolved
    for part in parts:
        if not part or part in {".", ".."}:
            raise ValidationFailed("Invalid path segment")
        if "/" in part or "\\" in part or "\x00" in part:
            raise ValidationFailed("Invalid path segment")
        candidate = candidate / part
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValidationFailed("Path escapes the project workspace")
    return resolved


def assert_within(root: Path, path: Path) -> Path:
    root_resolved = root.resolve()
    resolved = path.resolve()
    if root_resolved not in resolved.parents and resolved != root_resolved:
        raise ValidationFailed("Path escapes the project workspace")
    return resolved


def relative_display(path: Path, root: Path) -> str:
    """Human-readable, non-leaking path label (relative to the data root)."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
