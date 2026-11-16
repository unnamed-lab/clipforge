from __future__ import annotations

from clipforge.config import Settings
from clipforge.storage.base import (
    StorageBackend,
    StorageConfigurationError,
    StorageObjectMissing,
    StorageUnavailable,
)
from clipforge.storage.local import LocalStorage
from clipforge.storage.supabase import SupabaseStorage


def build_storage(settings: Settings) -> StorageBackend:
    backend = (settings.storage_backend or "local").strip().lower()
    if backend == "local":
        return LocalStorage(settings.data_dir)
    if backend == "supabase":
        return SupabaseStorage(
            base_url=settings.supabase_url,
            service_key=settings.supabase_service_key,
            bucket=settings.supabase_bucket,
            cache_dir=settings.data_dir / "cache",
            storage_prefix=settings.supabase_storage_prefix,
            timeout_seconds=settings.supabase_timeout_seconds,
        )
    raise StorageConfigurationError(
        "CLIPFORGE_STORAGE_BACKEND must be 'local' or 'supabase'."
    )


__all__ = [
    "StorageBackend",
    "StorageConfigurationError",
    "StorageObjectMissing",
    "StorageUnavailable",
    "build_storage",
]
