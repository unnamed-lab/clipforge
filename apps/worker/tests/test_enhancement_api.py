"""End-to-end API checks for the enhancement settings and real render output."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.enhancement.engines import reset_engines
from clipforge.media.process import require
from clipforge.media.probe import probe

HEADERS = {"x-clipforge-token": "test-token"}


def _make_clip(path: Path) -> None:
    ffmpeg = require("ffmpeg")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(path),
        ],
        check=True,
        timeout=120,
    )


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/jobs/{job_id}", headers=HEADERS).json()["job"]
        if payload["state"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.3)
    raise AssertionError(f"Job {job_id} did not finish within {timeout} seconds")


@pytest.fixture()
def client(settings):
    reset_engines()
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.mark.integration
def test_api_renders_ai_enhanced_footage_and_reports_what_ran(client, settings, tmp_path):
    # given an uploaded 160x90 source
    project = client.post("/api/projects", json={"name": "Enhancement API"}, headers=HEADERS).json()["project"]
    source_file = tmp_path / "source.mp4"
    _make_clip(source_file)
    upload = client.post(
        f"/api/projects/{project['id']}/sources/upload",
        headers=HEADERS,
        files={"file": ("source.mp4", source_file.read_bytes(), "video/mp4")},
    )
    assert upload.status_code == 200, upload.text

    # when an AI-enhanced 320x180 export is queued
    response = client.post(
        f"/api/projects/{project['id']}/renders",
        headers=HEADERS,
        json={
            "segments": [{"start_ms": 0, "end_ms": 2000}],
            "aspect": "16:9",
            "resolution": 720,
            "resolution_mode": "custom",
            "custom_width": 320,
            "custom_height": 180,
            "enhancement": {
                "profile": "ai",
                "model_id": "espcn-x2",
                "denoise": 0,
                "restoration": 1.0,
            },
            "captions_enabled": False,
        },
    )
    assert response.status_code == 200, response.text
    job = _wait_for_job(client, response.json()["job"]["id"])

    # then the job completed and the output really is 320x180 with audio
    assert job["state"] == "completed", job
    render = client.get(f"/api/projects/{project['id']}/renders", headers=HEADERS).json()["renders"][0]
    assert render["status"] == "completed"
    assert (render["width"], render["height"]) == (320, 180)
    assert 1800 <= render["duration_ms"] <= 2300
    assert render["review"]["checks"]
    enhancement = next(check for check in render["review"]["checks"] if check["id"] == "enhancement")
    assert enhancement["model"] == "espcn-x2"
    assert enhancement["status"] == "warn"

    source_row = client.get(f"/api/projects/{project['id']}/media", headers=HEADERS).json()["source"]
    source_path = settings.projects_dir.parent / source_row["relative_path"]
    assert source_path.exists()
    assert probe(source_path).has_audio


@pytest.mark.integration
def test_api_quality_preview_uses_the_same_model_and_reports_fallback(client, tmp_path):
    # given
    project = client.post("/api/projects", json={"name": "Compare"}, headers=HEADERS).json()["project"]
    source_file = tmp_path / "source.mp4"
    _make_clip(source_file)
    client.post(
        f"/api/projects/{project['id']}/sources/upload",
        headers=HEADERS,
        files={"file": ("source.mp4", source_file.read_bytes(), "video/mp4")},
    )

    # when
    response = client.post(
        f"/api/projects/{project['id']}/renders/quality-preview",
        headers=HEADERS,
        json={
            "segments": [{"start_ms": 0, "end_ms": 2000}],
            "aspect": "16:9",
            "resolution": 720,
            "resolution_mode": "custom",
            "custom_width": 320,
            "custom_height": 180,
            "enhancement": {"profile": "ai", "model_id": "espcn-x2", "denoise": 0},
            "captions_enabled": False,
        },
    )

    # then
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["applied_model"] == "espcn-x2"
    assert payload["original"].startswith("data:image/jpeg;base64,")
    assert payload["enhanced"].startswith("data:image/jpeg;base64,")
    assert payload["source"] == {"width": 160, "height": 90}
    assert payload["timestamp_ms"] == 1000

    # the same endpoint and framing chain also run the non-AI Clean profile
    clean = client.post(
        f"/api/projects/{project['id']}/renders/quality-preview",
        headers=HEADERS,
        json={
            "segments": [{"start_ms": 0, "end_ms": 2000}],
            "aspect": "16:9",
            "resolution": 720,
            "resolution_mode": "custom",
            "custom_width": 320,
            "custom_height": 180,
            "enhancement": {"profile": "clean", "denoise": 30},
            "captions_enabled": False,
        },
    )
    assert clean.status_code == 200, clean.text
    assert clean.json()["applied_model"] is None
    assert clean.json()["profile"] == "clean"


@pytest.mark.integration
def test_api_rejects_a_custom_size_that_would_stretch_the_frame(client, tmp_path):
    # given
    project = client.post("/api/projects", json={"name": "Bad size"}, headers=HEADERS).json()["project"]
    source_file = tmp_path / "source.mp4"
    _make_clip(source_file)
    client.post(
        f"/api/projects/{project['id']}/sources/upload",
        headers=HEADERS,
        files={"file": ("source.mp4", source_file.read_bytes(), "video/mp4")},
    )

    # when
    response = client.post(
        f"/api/projects/{project['id']}/renders",
        headers=HEADERS,
        json={
            "segments": [{"start_ms": 0, "end_ms": 2000}],
            "aspect": "9:16",
            "resolution": 1080,
            "resolution_mode": "custom",
            "custom_width": 1920,
            "custom_height": 1080,
            "captions_enabled": False,
        },
    )

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_resolution"


def test_capabilities_report_the_bundled_models_and_cpu_fallback(client):
    # when
    payload = client.get("/api/capabilities", headers=HEADERS).json()

    # then
    enhancement = payload["enhancement"]
    assert enhancement["gpu_required"] is False
    assert enhancement["frame_interpolation"] is False
    assert enhancement["devices"]["cpu_fallback"] is True
    assert {model["id"] for model in enhancement["models"]} >= {"espcn-x2", "espcn-x4", "realesrgan-x2"}
    assert all(model["licence"] for model in enhancement["models"])
    assert enhancement["profiles"][0]["id"] == "standard"
    assert "ai_fast" in {profile["id"] for profile in enhancement["profiles"]}
