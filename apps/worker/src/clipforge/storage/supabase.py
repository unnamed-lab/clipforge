"""Supabase Storage backend using the Storage REST API.

Only the worker holds the service-role key. Browser traffic never receives it:
downloads and uploads continue to pass through the authenticated Next.js proxy
and the worker, which validates and probes media before it is stored.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from clipforge.storage.base import (
    StorageConfigurationError,
    StorageObjectMissing,
    StorageUnavailable,
    validate_key,
)

logger = logging.getLogger(__name__)

_BUCKET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"


class SupabaseStorage:
    name = "supabase"

    def __init__(
        self,
        *,
        base_url: str,
        service_key: str,
        bucket: str,
        cache_dir: Path,
        storage_prefix: str = "/storage/v1",
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        storage_prefix = (storage_prefix or "").strip()
        if storage_prefix and not storage_prefix.startswith("/"):
            storage_prefix = "/" + storage_prefix
        storage_prefix = storage_prefix.rstrip("/")
        base_url = (base_url or "").strip().rstrip("/")
        if storage_prefix and base_url.endswith(storage_prefix):
            base_url = base_url[: -len(storage_prefix)]
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise StorageConfigurationError(
                "CLIPFORGE_SUPABASE_URL must be an absolute http(s) project URL, "
                "for example https://your-project.supabase.co"
            )
        if not (service_key or "").strip():
            raise StorageConfigurationError(
                "CLIPFORGE_SUPABASE_SERVICE_KEY is required when CLIPFORGE_STORAGE_BACKEND=supabase"
            )
        if not re.fullmatch(_BUCKET_PATTERN, bucket or ""):
            raise StorageConfigurationError(
                "CLIPFORGE_SUPABASE_BUCKET must be 1-100 letters, numbers, dots, hyphens or underscores"
            )
        self.base_url = base_url
        self.bucket = bucket
        self.storage_prefix = storage_prefix
        self.cache_dir = cache_dir.resolve()
        self._service_key = service_key.strip()
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(15.0, timeout_seconds)),
            transport=transport,
            follow_redirects=False,
        )

    # -- paths -------------------------------------------------------------

    def path_for(self, key: str) -> Path:
        key = validate_key(key)
        candidate = (self.cache_dir / key).resolve()
        if self.cache_dir not in candidate.parents:
            raise ValueError("Storage key escapes the cache directory")
        return candidate

    def _object_url(self, key: str) -> str:
        return (
            f"{self.base_url}{self.storage_prefix}/object/"
            f"{quote(self.bucket, safe='')}/{quote(validate_key(key), safe='/')}"
        )

    def _sign_url(self, key: str) -> str:
        return (
            f"{self.base_url}{self.storage_prefix}/object/sign/"
            f"{quote(self.bucket, safe='')}/{quote(validate_key(key), safe='/')}"
        )

    # -- contract ----------------------------------------------------------

    def exists(self, key: str) -> bool:
        response = self._request("HEAD", self._object_url(key), allow_missing=True)
        return response is not None

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        headers = {"x-upsert": "true"}
        if content_type:
            headers["content-type"] = content_type
        self._request("POST", self._object_url(key), content=data, headers=headers, retry=False)
        self._write_cache(key, data)

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> None:
        headers = {"x-upsert": "true", "content-length": str(source.stat().st_size)}
        if content_type:
            headers["content-type"] = content_type
        with source.open("rb") as handle:
            self._request("POST", self._object_url(key), content=handle, headers=headers, retry=False)
        self._cache_file(key, source)

    def download(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        try:
            with self._client.stream("GET", self._object_url(key), headers=self._auth_headers()) as response:
                if response.status_code == 404:
                    raise StorageObjectMissing("The stored file is missing from object storage")
                self._raise_for_status(response.status_code)
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
            os.replace(partial, destination)
        except (StorageObjectMissing, StorageUnavailable):
            partial.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise StorageUnavailable("Object storage could not be reached. Retry in a moment.") from exc

    def delete(self, key: str) -> None:
        self._request("DELETE", self._object_url(key), allow_missing=True, retry=True)
        self.path_for(key).unlink(missing_ok=True)

    def signed_url(self, key: str, *, expires_in: int) -> str | None:
        response = self._request(
            "POST",
            self._sign_url(key),
            json={"expiresIn": max(60, int(expires_in))},
            retry=True,
        )
        if response is None:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        signed = payload.get("signedURL") or payload.get("signedUrl")
        if not isinstance(signed, str) or not signed:
            return None
        if signed.startswith("http://") or signed.startswith("https://"):
            return signed
        if self.storage_prefix and signed.startswith(f"{self.storage_prefix}/"):
            return f"{self.base_url}{signed}"
        return f"{self.base_url}{self.storage_prefix}/{signed.lstrip('/')}"

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "bucket": self.bucket,
            "remote": True,
            "host": urlparse(self.base_url).netloc,
            "prefix": self.storage_prefix,
        }

    def close(self) -> None:
        self._client.close()

    # -- internals ---------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {
            "apikey": self._service_key,
            "Authorization": f"Bearer {self._service_key}",
            "x-client-info": "clipforge-worker",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | object | None = None,
        headers: dict[str, str] | None = None,
        json: dict | None = None,
        allow_missing: bool = False,
        retry: bool = True,
    ) -> httpx.Response | None:
        merged = {**self._auth_headers(), **(headers or {})}
        attempts = 2 if retry else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.request(method, url, content=content, headers=merged, json=json)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                raise StorageUnavailable("Object storage could not be reached. Retry in a moment.") from exc

            if response.status_code == 404 or (method == "HEAD" and response.status_code == 400):
                if allow_missing:
                    return None
                raise StorageObjectMissing("The stored file is missing from object storage")
            if response.status_code >= 500 and attempt + 1 < attempts:
                continue
            if response.status_code >= 500:
                logger.warning("Supabase Storage returned %s", response.status_code)
                raise StorageUnavailable("Object storage is temporarily unavailable. Retry in a moment.")
            self._raise_for_status(response.status_code)
            return response
        raise StorageUnavailable("Object storage could not be reached. Retry in a moment.") from last_error

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if 200 <= status_code < 300:
            return
        if status_code in {401, 403}:
            raise StorageUnavailable("Object storage rejected the configured credentials.")
        if status_code == 413:
            raise StorageUnavailable("Object storage rejected the file because it is too large for the bucket.")
        logger.warning("Supabase Storage returned %s", status_code)
        raise StorageUnavailable("Object storage rejected the request.")

    def _write_cache(self, key: str, data: bytes) -> None:
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        partial.write_bytes(data)
        os.replace(partial, destination)

    def _cache_file(self, key: str, source: Path) -> None:
        destination = self.path_for(key)
        if source.resolve() == destination:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, destination)
        except OSError:
            partial = destination.with_name(destination.name + ".partial")
            shutil.copyfile(source, partial)
            os.replace(partial, destination)
            source.unlink(missing_ok=True)
