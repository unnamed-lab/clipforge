"""Filesystem storage used for local development and single-disk deployments."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from clipforge.security.paths import safe_join
from clipforge.storage.base import StorageObjectMissing, validate_key


class LocalStorage:
    name = "local"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()

    def path_for(self, key: str) -> Path:
        return safe_join(self.data_dir, *validate_key(key).split("/"))

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        partial.write_bytes(data)
        os.replace(partial, destination)

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> None:
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == destination:
            return
        try:
            os.replace(source, destination)
        except OSError:
            partial = destination.with_name(destination.name + ".partial")
            shutil.copyfile(source, partial)
            os.replace(partial, destination)
            source.unlink(missing_ok=True)

    def download(self, key: str, destination: Path) -> None:
        source = self.path_for(key)
        if not source.is_file():
            raise StorageObjectMissing("The stored file is missing")
        if source.resolve() == destination.resolve():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def delete(self, key: str) -> None:
        try:
            self.path_for(key).unlink(missing_ok=True)
        except ValueError as exc:
            raise StorageObjectMissing("The stored file key is invalid") from exc

    def signed_url(self, key: str, *, expires_in: int) -> str | None:
        return None

    def describe(self) -> dict:
        return {"backend": self.name, "bucket": None, "remote": False}

    def close(self) -> None:
        return None
