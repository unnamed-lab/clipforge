"""Project workspace layout and disk budgeting.

Layout (all generated identifiers, never user strings):

    data/projects/<project_id>/
        source/<file_id>.<container>
        audio/audio.wav
        analysis/transcript.json
        analysis/waveform.png
        renders/<render_id>.mp4
        tmp/                      (cleared before each render)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from clipforge.config import Settings
from clipforge.errors import CapacityExceeded, ValidationFailed
from clipforge.security.paths import assert_within, safe_join

PROJECT_SUBDIRS = ("source", "audio", "analysis", "renders", "tmp")


def project_root(settings: Settings, project_id: str) -> Path:
    if not project_id or len(project_id) != 32 or not all(c in "0123456789abcdef" for c in project_id):
        raise ValidationFailed("Invalid project identifier")
    root = safe_join(settings.projects_dir, project_id)
    return root


def ensure_layout(settings: Settings, project_id: str) -> Path:
    root = project_root(settings, project_id)
    for subdir in PROJECT_SUBDIRS:
        (root / subdir).mkdir(parents=True, exist_ok=True)
    return root


def free_bytes(path: Path) -> int:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return 0


def ensure_space(settings: Settings, path: Path, required_bytes: int = 0) -> None:
    free = free_bytes(path)
    needed = max(required_bytes, settings.min_free_bytes)
    if free < needed:
        raise CapacityExceeded(
            f"Not enough free disk space. This needs about {needed // (1024 ** 3) + 1} GB free "
            f"and only {free // (1024 ** 2)} MB is available.",
            code="insufficient_disk",
        )


def project_relative(settings: Settings, path: Path) -> str:
    return assert_within(settings.data_dir, path).relative_to(settings.data_dir.resolve()).as_posix()


def absolute_from_relative(settings: Settings, relative: str) -> Path:
    if relative.startswith("/") or "\\" in relative or ".." in relative.split("/"):
        raise ValidationFailed("Invalid stored path")
    resolved = (settings.data_dir / relative).resolve()
    if settings.data_dir.resolve() not in resolved.parents:
        raise ValidationFailed("Invalid stored path")
    return resolved


def write_json_atomic(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def clear_tmp(settings: Settings, project_id: str) -> None:
    tmp = project_root(settings, project_id) / "tmp"
    if not tmp.exists():
        return
    for child in tmp.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            continue


def remove_project_workspace(settings: Settings, project_id: str) -> None:
    try:
        root = project_root(settings, project_id)
    except ValidationFailed:
        return
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
