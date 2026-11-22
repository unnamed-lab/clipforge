"""Copy approved deliverables into the user's export folder.

The render row remains the asset identity. A save row records that a copy exists
in a directory outside the app's data root, so the UI and manifest can show it
without exposing private storage keys.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.errors import Conflict, ValidationFailed
from clipforge.media.fingerprint import sha256_file
from clipforge.pipeline.exports import export_filename, slugify
from clipforge.storage.base import StorageBackend, StorageObjectMissing, ensure_local

SYSTEM_ROOTS = [
    Path("/etc"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/System"),
    Path("/Library"),
]
WINDOWS_ROOTS = ["Windows", "Program Files", "Program Files (x86)", "ProgramData"]


def display_path(path: Path) -> str:
    try:
        home = Path.home().resolve()
        resolved = path.resolve()
        if resolved == home:
            return "~"
        return str(Path("~") / resolved.relative_to(home))
    except (OSError, ValueError):
        return str(path)


def _is_unacceptable(path: Path) -> str | None:
    resolved = path.resolve()
    if resolved.parent == resolved:
        return "A whole drive or filesystem root cannot be the export folder."
    if resolved == Path.home().resolve():
        return "Choose a folder inside your home directory, not the home directory itself."
    lowered = str(resolved).lower()
    for name in WINDOWS_ROOTS:
        candidate = (Path(os.environ.get("SystemDrive", "C:") + "\\") / name).resolve()
        if candidate == resolved or candidate in resolved.parents:
            return "System application folders cannot be used as an export folder."
    for root in SYSTEM_ROOTS:
        try:
            if root == resolved or root in resolved.parents:
                return "System folders cannot be used as an export folder."
        except OSError:
            continue
    if not lowered:
        return "The export folder is empty."
    return None


def validate_export_dir(raw: str | None) -> Path | None:
    if raw is None or not raw.strip():
        return None
    candidate = Path(os.path.expandvars(raw.strip())).expanduser()
    if not candidate.is_absolute():
        raise ValidationFailed(
            "The export folder must be an absolute path on the worker machine.",
            code="invalid_export_dir",
        )
    problem = _is_unacceptable(candidate)
    if problem:
        raise ValidationFailed(problem, code="invalid_export_dir")
    return candidate.resolve()


def base_export_directory(settings: Settings, project: dict) -> Path:
    configured = (project.get("settings") or {}).get("export_dir")
    if configured:
        resolved = validate_export_dir(str(configured))
        if resolved is not None:
            return resolved
    return settings.effective_export_dir.resolve()


def project_export_directory(settings: Settings, project: dict) -> Path:
    return base_export_directory(settings, project) / slugify(project.get("name") or "project")


def export_settings_document(settings: Settings, project: dict) -> dict:
    custom = (project.get("settings") or {}).get("export_dir")
    default_dir = settings.effective_export_dir.resolve()
    effective = project_export_directory(settings, project)
    return {
        "custom_dir": display_path(Path(custom)) if custom else None,
        "default_dir": display_path(default_dir),
        "effective_dir": display_path(effective),
        "project_folder": slugify(project.get("name") or "project"),
    }


def _filename_for(project: dict, render: dict) -> str:
    if render.get("display_name"):
        return str(render["display_name"])
    spec = render.get("spec") or {}
    return export_filename(
        project_name=project.get("name") or "project",
        strategy=(spec.get("editorial") or {}).get("strategy"),
        aspect=(spec.get("video") or {}).get("aspect") or "9:16",
        version=int(render.get("version") or 1),
    )


def _available_target(directory: Path, filename: str, checksum: str | None) -> tuple[Path, bool]:
    """Return a non-conflicting path. Never overwrite a different file."""
    target = directory / filename
    if not target.exists():
        return target, False
    existing = sha256_file(target)
    if checksum and existing == checksum:
        return target, True
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 100):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate, False
    raise Conflict("Could not find a free filename for the exported clip.")


def save_render(
    repos: Repositories,
    settings: Settings,
    storage: StorageBackend,
    render: dict,
    *,
    directory: Path | None = None,
) -> dict:
    """Copy one completed render into the project's export folder."""
    if render.get("status") != "completed":
        raise Conflict("Only a completed render can be saved.")
    raw = repos.renders.get_raw(render["id"])
    if not raw.get("relative_path"):
        raise Conflict("Only a completed render can be saved.")
    project = repos.projects.get(render["project_id"])
    target_dir = directory or project_export_directory(settings, project)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = ensure_local(
            storage,
            raw["relative_path"],
            missing_message="The rendered file is missing from storage.",
        )
    except StorageObjectMissing as exc:
        raise Conflict(exc.message) from exc
    checksum = render.get("checksum") or raw.get("checksum") or sha256_file(source)
    filename = _filename_for(project, render)
    target, already_there = _available_target(target_dir, filename, checksum)
    if not already_there:
        partial = target.with_suffix(target.suffix + ".partial")
        shutil.copyfile(source, partial)
        os.replace(partial, target)
    size_bytes = target.stat().st_size
    saved = repos.export_saves.record(
        render_id=render["id"],
        project_id=project["id"],
        directory=str(target_dir.resolve()),
        filename=target.name,
        checksum=checksum,
        size_bytes=size_bytes,
    )
    return {
        "filename": target.name,
        "directory": display_path(target_dir),
        "checksum": checksum,
        "size_bytes": size_bytes,
        "already_present": already_there,
        "saved_at": saved.get("updated_at") or saved.get("created_at"),
    }


def saved_documents(repos: Repositories, render_id: str) -> list[dict]:
    rows = repos.export_saves.list_for_render(render_id)
    return [
        {
            "filename": row["filename"],
            "directory": display_path(Path(row["directory"])),
            "checksum": row.get("checksum"),
            "size_bytes": row.get("size_bytes"),
            "saved_at": row.get("updated_at") or row.get("created_at"),
        }
        for row in rows
    ]


def delete_render_assets(
    repos: Repositories,
    storage: StorageBackend,
    render: dict,
) -> dict:
    """Delete an export from app storage at the user's explicit request.

    Copies already written to the user's export folder are deliberately left
    alone: the app must not reach outside its data root during a delete.
    """
    keys: list[str] = []
    try:
        raw = repos.renders.get_raw(render["id"])
    except Exception:
        raw = {}
    if raw.get("relative_path"):
        keys.append(str(raw["relative_path"]))
    review = render.get("review") or {}
    if review.get("draft_relative_path"):
        keys.append(str(review["draft_relative_path"]))
    if raw.get("subtitle_path"):
        keys.append(str(raw["subtitle_path"]))
    for key in dict.fromkeys(keys):
        try:
            storage.delete(key)
        except Exception:
            pass
    repos.export_saves.delete_for_render(render["id"])
    repos.renders.delete(render["id"])
    return {"deleted": True, "render_id": render["id"], "files": len(keys)}
