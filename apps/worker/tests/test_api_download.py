"""The HTTP contract for download and compression choices.

These run the real app with a temporary data directory. They prove the payloads
the UI reads exist in the shape it expects, without touching the network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.config import Settings


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"x-clipforge-token": settings.token})
        yield test_client


def test_capabilities_describe_every_download_quality_and_mode(client):
    # when
    response = client.get("/api/capabilities")

    # then
    assert response.status_code == 200
    download = response.json()["download"]
    qualities = {item["id"] for item in download["qualities"]}
    assert {
        "lowest",
        "144p",
        "240p",
        "360p",
        "480p",
        "720p",
        "1080p",
        "1440p",
        "2160p",
        "best",
        "audio",
    } <= qualities
    modes = {item["id"] for item in download["processing"]}
    assert modes == {"fast", "balanced", "high", "custom"}
    proxy_range = download["proxy_height_range"]
    assert proxy_range["max"] >= proxy_range["min"] >= 144
    assert download["approximate_sizes"] is True


def test_capabilities_describe_compression_without_requiring_a_gpu(client):
    # when
    response = client.get("/api/capabilities")

    # then
    compression = response.json()["compression"]
    presets = {item["id"] for item in compression["presets"]}
    assert presets == {"smallest", "balanced", "higher"}
    assert compression["gpu_required"] is False
    assert compression["available_encoders"]
    assert all(item["resolution_cap"] for item in compression["presets"])


def test_render_estimate_is_labelled_approximate(client, project):
    # when
    response = client.post(
        f"/api/projects/{project['id']}/renders/estimate",
        json={
            "aspect": "16:9",
            "resolution": 1080,
            "fps": 30,
            "duration_ms": 30_000,
            "compression": {"preset": "smallest"},
        },
    )

    # then
    assert response.status_code == 200
    body = response.json()
    assert body["approximate"] is True
    assert body["estimated_bytes"] > 0
    assert (body["width"], body["height"]) == (1280, 720)
    assert "duration" in body["basis"]


def test_render_estimate_rejects_impossible_compression(client, project):
    # when
    response = client.post(
        f"/api/projects/{project['id']}/renders/estimate",
        json={
            "duration_ms": 30_000,
            "compression": {"preset": "custom", "crf": 99},
        },
    )

    # then
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_compression"
    assert "crf" in error["message"].lower()


def test_an_import_request_without_a_url_is_rejected_before_any_download(client, project):
    # when
    response = client.post(f"/api/projects/{project['id']}/sources/url", json={"quality": "720p"})

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_an_import_requires_rights_confirmation(client, project):
    # when
    response = client.post(
        f"/api/projects/{project['id']}/sources/url",
        json={"url": "https://example.com/watch?v=abc", "quality": "720p"},
    )

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "rights_not_confirmed"


def test_an_unsafe_url_is_rejected_without_contacting_the_site(client, project):
    # when
    response = client.post(
        f"/api/projects/{project['id']}/sources/url",
        json={
            "url": "http://127.0.0.1/admin",
            "rights_confirmed": True,
            "quality": "720p",
        },
    )

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "url_blocked"


def test_the_worker_still_requires_its_token(client):
    # given
    headers = {"x-clipforge-token": "wrong"}

    # when
    response = client.get("/api/capabilities", headers=headers)

    # then
    assert response.status_code == 401
