"""Collection and removal of stored media for a project."""

from __future__ import annotations

from clipforge.db.repositories import Repositories
from clipforge.storage.base import StorageBackend, delete_keys


def project_media_keys(repos: Repositories, project_id: str) -> list[str]:
    keys: list[str | None] = []
    source = repos.sources.find_optional(project_id)
    if source:
        keys.append(source.get("relative_path"))
    for render in repos.renders.list_raw_for_project(project_id):
        keys.append(render.get("relative_path"))
        keys.append(render.get("subtitle_path"))
    for asset in repos.assets.list_for_project(project_id):
        keys.append(asset.get("relative_path"))
    return [key for key in keys if key]


def delete_project_media(storage: StorageBackend, repos: Repositories, project_id: str) -> int:
    keys = project_media_keys(repos, project_id)
    delete_keys(storage, keys)
    return len(keys)
