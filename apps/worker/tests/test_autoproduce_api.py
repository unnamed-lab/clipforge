"""End-to-end automatic production: real video in, reviewed export out."""

from __future__ import annotations

import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.media.process import require
from clipforge.media.probe import probe
from clipforge.pipeline.orchestrator import create_run
from clipforge.storage.local import LocalStorage

HEADERS = {"x-clipforge-token": "test-token"}


def _make_clip(path) -> None:
    ffmpeg = require("ffmpeg")
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/jobs/{job_id}", headers=HEADERS).json()["job"]
        if payload["state"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.3)
    raise AssertionError(f"Job {job_id} did not finish within {timeout} seconds")


@pytest.fixture()
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        test_client.headers.update(HEADERS)
        yield test_client


@pytest.fixture()
def seeded(repos, settings, project):
    storage = LocalStorage(settings.data_dir)
    key = f"projects/{project['id']}/source/{uuid.uuid4().hex}.mp4"
    path = storage.path_for(key)
    _make_clip(path)
    info = probe(path)
    source = repos.sources.upsert(
        project_id=project["id"],
        kind="upload",
        original_name="two-second-source.mp4",
        storage_path=str(path),
        relative_path=key,
        size_bytes=path.stat().st_size,
        duration_ms=info.duration_ms,
        width=info.width,
        height=info.height,
        fps=info.fps,
        has_audio=info.has_audio,
        video_codec=info.video_codec,
        audio_codec=info.audio_codec,
        container="mp4",
        source_url=None,
        probe=info.raw,
        has_video=True,
    )
    segments = [
        {
            "id": 0,
            "start_ms": 0,
            "end_ms": 2000,
            "text": " This is a short test clip for the project pipeline.",
            "words": [
                {
                    "word": f" {token}",
                    "start_ms": index * 180,
                    "end_ms": index * 180 + 160,
                    "probability": 0.9,
                }
                for index, token in enumerate(
                    "This is a short test clip for the project pipeline.".split()
                )
            ],
        }
    ]
    repos.transcripts.save(
        project_id=project["id"],
        source_id=source["id"],
        fingerprint="pipeline-fixture",
        language="en",
        language_probability=0.99,
        model="tiny",
        duration_ms=info.duration_ms,
        segments=segments,
    )
    run = repos.runs.create(project["id"], {"goal": "best_moments"})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [
            {
                "id": "candidate-pipeline",
                "start_ms": 0,
                "end_ms": 2000,
                "title": "A short test clip",
                "excerpt": "This is a short test clip for the project pipeline.",
                "method": "baseline",
                "method_version": "1",
                "overall": 75.0,
                "hook_detail": {
                    "text": "This is a short test clip",
                    "start_ms": 0,
                    "end_ms": 900,
                    "source": "candidate",
                    "placement": "none",
                    "direction": "opening",
                    "approved": False,
                },
            }
        ],
    )
    repos.runs.finish(run["id"], status="completed", provider="baseline", warnings=[])
    return project


def _start(client: TestClient, project_id: str) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/autoproduce",
        json={
            "clip_count": 1,
            "allow_extra_discovery": False,
            "use_model_review": False,
            "aspect": "16:9",
            "resolution": 720,
            "resolution_mode": "custom",
            "custom_width": 320,
            "custom_height": 180,
            "captions_enabled": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait_for_run(client: TestClient, project_id: str, job: dict) -> dict:
    finished = _wait_for_job(client, job["id"])
    assert finished["state"] == "completed", finished
    return client.get(f"/api/projects/{project_id}/pipeline").json()


@pytest.mark.integration
def test_autoproduce_renders_reviews_and_versions_a_real_clip(client, seeded, settings):
    started = _start(client, seeded["id"])

    state = _wait_for_run(client, seeded["id"], started["job"])

    assert state["run"]["status"] in {"completed", "partially_completed"}
    assert [stage["status"] for stage in state["stages"] if stage["name"] == "FINAL_QA"] == ["completed"]
    assert state["clips"], state
    clip = state["clips"][0]
    assert clip["status"] in {"passed", "passed_with_warnings"}
    assert clip["render_id"]
    assert clip["review"]["findings"] is not None

    render = client.get(f"/api/renders/{clip['render_id']}", headers=HEADERS).json()["render"]
    assert render["status"] == "completed"
    assert render["review"]["checks"], "the export gate evidence must be persisted"
    assert render["review"]["status"] in {"pass", "warn"}
    assert render["display_name"].count("__") == 3
    assert re.search(r"__v\d{3}\.mp4$", render["display_name"])
    assert "__landscape__" in render["display_name"]
    assert render["checksum"]

    exports = client.get(f"/api/projects/{seeded['id']}/exports", headers=HEADERS).json()
    assert len(exports["current"]) == 1
    assert exports["current"][0]["asset_id"] == render["id"]
    assert exports["current"][0]["review"]["status"] in {"pass", "warn"}
    assert exports["current"][0]["saved"], "an approved deliverable is copied to the export folder"
    saved_copy = Path(exports["current"][0]["saved"][0]["filename"])
    saved_root = settings.effective_export_dir / "test-project"
    assert (saved_root / saved_copy.name).exists()
    # A repair produces a new version; the superseded predecessor is retained.
    for superseded in exports["superseded"]:
        assert superseded["asset_id"] != render["id"]

    manifest = client.get(f"/api/projects/{seeded['id']}/manifest", headers=HEADERS).json()
    assert manifest["source"]["asset_id"]
    assert len(manifest["exports"]) >= 1
    serialised = str(manifest)
    assert "storage_path" not in serialised
    assert "relative_path" not in serialised
    assert manifest["provenance"]["source_identity"]
    assert len(exports["current"]) + len(exports["superseded"]) == len(manifest["exports"])

    # A user can record an explicit approval without hiding the findings.
    approval = client.post(
        f"/api/pipeline/{state['run']['id']}/clips/{clip['id']}/approve",
        json={"approved": True, "note": "Reviewed the measured findings."},
    )
    assert approval.status_code == 200, approval.text
    approved = approval.json()["clip"]
    assert approved["review"]["approved_by_user"] is True
    assert approved["review"]["approval_note"] == "Reviewed the measured findings."
    assert approved["status"] in {"passed_with_warnings", "needs_review"}

    # Removing the clip clears it from the production list but keeps the export.
    removal = client.delete(
        f"/api/pipeline/{state['run']['id']}/clips/{clip['id']}", headers=HEADERS
    )
    assert removal.status_code == 200, removal.text
    after_removal = client.get(f"/api/projects/{seeded['id']}/pipeline", headers=HEADERS).json()
    assert after_removal["clips"] == []
    assert client.get(f"/api/projects/{seeded['id']}/exports", headers=HEADERS).json()["current"]

    # Deleting the export is explicit and removes the app-stored file.
    deleted = client.delete(f"/api/exports/{render['id']}", headers=HEADERS)
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/projects/{seeded['id']}/exports", headers=HEADERS).json()["current"] == []
    stored = settings.data_dir / "projects" / seeded["id"] / "renders" / f"{render['id']}.mp4"
    assert not stored.exists()
    # A copy already delivered to the export folder is not touched by app deletion.
    assert (saved_root / saved_copy.name).exists()


@pytest.mark.integration
def test_repeating_the_same_production_reuses_the_export(client, seeded):
    first = _start(client, seeded["id"])
    first_state = _wait_for_run(client, seeded["id"], first["job"])
    first_render = first_state["clips"][0]["render_id"]

    second = _start(client, seeded["id"])
    second_state = _wait_for_run(client, seeded["id"], second["job"])

    assert second["run"]["id"] != first["run"]["id"]
    assert second_state["clips"][0]["render_id"] == first_render
    renders = client.get(f"/api/projects/{seeded['id']}/renders", headers=HEADERS).json()["renders"]
    completed = [item for item in renders if item["status"] == "completed"]
    current = [item for item in completed if not item["superseded"]]
    assert len(current) == 1, "identical edits must not create a second current export"
    assert current[0]["id"] == first_render
    assert current[0]["version"] >= 1


@pytest.mark.integration
def test_a_second_start_while_running_returns_the_same_run(client, seeded):
    first = _start(client, seeded["id"])
    second = _start(client, seeded["id"])
    assert second["reused"] is True
    assert second["run"]["id"] == first["run"]["id"]
    _wait_for_run(client, seeded["id"], first["job"])


@pytest.mark.integration
def test_export_folder_can_be_changed_per_project(client, seeded, settings, tmp_path):
    # given the default settings value
    current = client.get(
        f"/api/projects/{seeded['id']}/export-settings", headers=HEADERS
    ).json()
    assert current["export_settings"]["effective_dir"]

    # when a project overrides the folder
    target = tmp_path / "chosen-exports"
    response = client.put(
        f"/api/projects/{seeded['id']}/export-settings",
        headers=HEADERS,
        json={"export_dir": str(target)},
    )
    assert response.status_code == 200, response.text
    document = response.json()["export_settings"]
    assert document["custom_dir"]
    assert document["effective_dir"].endswith("test-project")

    # then resetting returns to the default
    reset = client.put(
        f"/api/projects/{seeded['id']}/export-settings",
        headers=HEADERS,
        json={"export_dir": None},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["export_settings"]["custom_dir"] is None


def test_a_relative_export_folder_is_rejected(client, seeded):
    response = client.put(
        f"/api/projects/{seeded['id']}/export-settings",
        headers=HEADERS,
        json={"export_dir": "relative/clips"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_export_dir"


@pytest.mark.integration
def test_retry_resumes_after_the_last_completed_stage(client, seeded, repos, settings):
    # given a run that failed during analysis after source validation completed
    run = create_run(
        repos,
        settings,
        project_id=seeded["id"],
        request={
            "clip_count": 1,
            "allow_extra_discovery": False,
            "use_model_review": False,
            "aspect": "16:9",
            "resolution": 720,
            "resolution_mode": "custom",
            "custom_width": 320,
            "custom_height": 180,
            "captions_enabled": False,
        },
        provider={"provider": "none"},
    )
    repos.project_stages.start(run["id"], "SOURCE_VALIDATION")
    repos.project_stages.complete(run["id"], "SOURCE_VALIDATION", {"source_id": "known"})
    repos.project_stages.start(run["id"], "SOURCE_ANALYSIS")
    repos.project_stages.fail(run["id"], "SOURCE_ANALYSIS", code="boom", message="simulated")
    repos.project_runs.set_status(run["id"], "failed", message="simulated failure")

    # when
    response = client.post(f"/api/pipeline/{run['id']}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    _wait_for_job(client, job["id"])

    # then the completed stage was not executed again
    state = client.get(f"/api/projects/{seeded['id']}/pipeline", headers=HEADERS).json()
    stages = {stage["name"]: stage for stage in state["stages"]}
    assert stages["SOURCE_VALIDATION"]["status"] == "completed"
    assert stages["SOURCE_VALIDATION"]["attempt"] == 1
    assert stages["SOURCE_ANALYSIS"]["status"] == "completed"
    assert stages["SOURCE_ANALYSIS"]["attempt"] >= 2
    assert state["run"]["status"] in {"completed", "partially_completed"}
