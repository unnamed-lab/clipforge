"""Storage key safety, local round-trips and the Supabase REST adapter."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from clipforge.storage import build_storage
from clipforge.storage.base import (
    StorageConfigurationError,
    StorageObjectMissing,
    StorageUnavailable,
    ensure_local,
    validate_key,
)
from clipforge.storage.local import LocalStorage
from clipforge.storage.supabase import SupabaseStorage

BUCKET = "clipforge"
KEY = "assets/one.png"


def make_supabase(settings, handler, *, base_url="https://project.supabase.co", prefix="/storage/v1") -> SupabaseStorage:
    return SupabaseStorage(
        base_url=base_url,
        service_key="service-role-key",
        bucket=BUCKET,
        cache_dir=settings.data_dir / "cache",
        storage_prefix=prefix,
        transport=httpx.MockTransport(handler),
    )


# --- keys and local storage ---------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["", "/absolute", "../escape", "assets/../../escape", "assets//one.png", "assets\\one.png", "a" * 513],
)
def test_storage_keys_reject_traversal_and_invalid_segments(key):
    with pytest.raises(ValueError):
        validate_key(key)


def test_local_storage_round_trip(settings):
    # given
    storage = LocalStorage(settings.data_dir)

    # when
    storage.put_bytes(KEY, b"png-bytes", content_type="image/png")

    # then
    assert storage.exists(KEY) is True
    assert storage.path_for(KEY).read_bytes() == b"png-bytes"
    copied = settings.tmp_dir / "copy.png"
    storage.download(KEY, copied)
    assert copied.read_bytes() == b"png-bytes"
    storage.delete(KEY)
    assert storage.exists(KEY) is False


def test_local_storage_moves_file_without_copy(settings):
    # given
    storage = LocalStorage(settings.data_dir)
    source = settings.tmp_dir / "upload.mp4"
    source.write_bytes(b"video")

    # when
    storage.put_file("projects/" + "a" * 32 + "/source/one.mp4", source)

    # then
    assert source.exists() is False
    assert storage.path_for("projects/" + "a" * 32 + "/source/one.mp4").read_bytes() == b"video"


def test_build_storage_defaults_to_local(settings):
    assert build_storage(settings).name == "local"


def test_build_storage_requires_supabase_credentials(settings):
    # given
    remote = replace(settings, storage_backend="supabase")

    # when / then
    with pytest.raises(StorageConfigurationError):
        build_storage(remote)


def test_build_storage_rejects_unknown_backend(settings):
    with pytest.raises(StorageConfigurationError):
        build_storage(replace(settings, storage_backend="s3"))


# --- Supabase adapter ---------------------------------------------------------


def test_supabase_upload_download_delete_and_signed_url(settings):
    # given
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path.endswith("/object/sign/clipforge/assets/one.png"):
            return httpx.Response(200, json={"signedURL": "/object/sign/clipforge/assets/one.png?token=abc"})
        if request.method == "POST":
            return httpx.Response(200, json={"Key": "clipforge/assets/one.png"})
        if request.method == "HEAD":
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(200, content=b"remote-bytes")
        if request.method == "DELETE":
            return httpx.Response(200)
        return httpx.Response(404)

    storage = make_supabase(settings, handler)

    # when
    storage.put_bytes(KEY, b"png-bytes", content_type="image/png")
    downloaded = settings.tmp_dir / "remote.png"
    storage.download(KEY, downloaded)

    # then
    assert downloaded.read_bytes() == b"remote-bytes"
    assert storage.path_for(KEY).read_bytes() == b"png-bytes"

    # when
    signed = storage.signed_url(KEY, expires_in=120)
    storage.delete(KEY)

    # then
    assert signed == "https://project.supabase.co/storage/v1/object/sign/clipforge/assets/one.png?token=abc"
    upload = next(request for request in seen if request.method == "POST" and "sign" not in request.url.path)
    assert upload.headers["apikey"] == "service-role-key"
    assert upload.headers["authorization"] == "Bearer service-role-key"
    assert upload.headers["x-upsert"] == "true"
    assert upload.headers["content-type"] == "image/png"
    assert seen[-1].method == "DELETE"
    assert storage.path_for(KEY).exists() is False
    storage.close()


def test_supabase_can_target_the_storage_api_image_without_a_gateway_prefix(settings):
    # given
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"Key": "clipforge/assets/one.png"})

    storage = make_supabase(
        settings,
        handler,
        base_url="http://storage-api:5000",
        prefix="",
    )

    # when
    storage.put_bytes(KEY, b"png-bytes", content_type="image/png")

    # then
    assert seen[0].url.path == "/object/clipforge/assets/one.png"
    assert storage.describe()["prefix"] == ""
    storage.close()


def test_supabase_existing_storage_prefix_is_not_duplicated(settings):
    # given
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    storage = make_supabase(
        settings,
        handler,
        base_url="http://localhost:8000/storage/v1",
        prefix="/storage/v1",
    )

    # when
    storage.put_bytes(KEY, b"png-bytes")

    # then
    assert seen[0].url.path == "/storage/v1/object/clipforge/assets/one.png"
    storage.close()


def test_supabase_exists_returns_false_for_missing_object(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    storage = make_supabase(settings, handler)
    assert storage.exists(KEY) is False
    storage.close()


def test_supabase_delete_missing_still_clears_local_cache(settings):
    # given
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    storage = make_supabase(settings, handler)
    cached = storage.path_for(KEY)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"stale")

    # when
    storage.delete(KEY)

    # then
    assert cached.exists() is False
    storage.close()


def test_supabase_missing_download_raises_domain_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    storage = make_supabase(settings, handler)
    with pytest.raises(StorageObjectMissing):
        storage.download(KEY, settings.tmp_dir / "missing.png")
    storage.close()


def test_supabase_server_error_is_unavailable(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    storage = make_supabase(settings, handler)
    with pytest.raises(StorageUnavailable):
        storage.put_bytes(KEY, b"bytes")
    storage.close()


def test_supabase_invalid_configuration_is_rejected(tmp_path):
    cache = tmp_path / "cache"
    with pytest.raises(StorageConfigurationError):
        SupabaseStorage(base_url="", service_key="key", bucket=BUCKET, cache_dir=cache)
    with pytest.raises(StorageConfigurationError):
        SupabaseStorage(base_url="https://project.supabase.co", service_key="", bucket=BUCKET, cache_dir=cache)
    with pytest.raises(StorageConfigurationError):
        SupabaseStorage(
            base_url="https://project.supabase.co",
            service_key="key",
            bucket="bad/bucket",
            cache_dir=cache,
        )


def test_ensure_local_downloads_cold_cache(settings):
    # given
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(200, content=b"remote")

    storage = make_supabase(settings, handler)
    destination = storage.path_for(KEY)
    assert destination.exists() is False

    # when
    resolved = ensure_local(storage, KEY, missing_message="missing")

    # then
    assert resolved == destination
    assert resolved.read_bytes() == b"remote"
    storage.close()


def test_ensure_local_reports_missing_remote_object(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    storage = make_supabase(settings, handler)
    with pytest.raises(StorageObjectMissing):
        ensure_local(storage, KEY, missing_message="missing")
    storage.close()
