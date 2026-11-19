"""Asking for a second look at one scene.

The pipeline samples cheap frames and records that evidence level. This
endpoint is the escape hatch for a moment where text matters, so its job is to
use the best resolution the *source* can supply, never more, and to say plainly
when the source cannot support the claim.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from clipforge.api import routes_analysis
from clipforge.api.app import create_app
from clipforge.config import Settings
from clipforge.media.visual import SceneAnalysis
from clipforge.storage.local import LocalStorage


@pytest.fixture()
def storage(settings: Settings) -> LocalStorage:
    return LocalStorage(settings.data_dir)


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"x-clipforge-token": settings.token})
        yield test_client


def _source(repos, storage, project, *, height: int | None = 1080, has_video: bool = True) -> dict:
    key = f"projects/{project['id']}/source/{uuid.uuid4().hex}.mp4"
    path = storage.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 2048)
    return repos.sources.upsert(
        project_id=project["id"],
        kind="url",
        original_name="Source",
        storage_path=str(path),
        relative_path=key,
        size_bytes=2048,
        duration_ms=60_000,
        width=1920 if has_video else None,
        height=height,
        fps=30.0 if has_video else None,
        has_audio=True,
        video_codec="h264" if has_video else None,
        audio_codec="aac",
        container="mp4" if has_video else "m4a",
        source_url="https://example.com/watch?v=abc",
        probe={},
        has_video=has_video,
    )


@pytest.fixture()
def fake_analysis(monkeypatch):
    """Record the call and answer as the real analyser would at that height."""
    calls: list[dict] = []

    def fake(path, start_ms, end_ms, **kwargs):
        height = kwargs["analysis_height"]
        calls.append({"path": path, "start_ms": start_ms, "end_ms": end_ms, **kwargs})
        return SceneAnalysis(
            scenes=[],
            samples=[],
            cuts=[],
            warnings=[],
            analysis={
                "sampled_width": None,
                "sampled_height": height,
                "scene_fps": 1.0,
                "source_height": kwargs.get("source_height"),
                "evidence": "detail" if height >= 720 else "low_resolution",
                "text_verified": height >= 720,
                "text_heavy": False,
            },
        )

    monkeypatch.setattr(routes_analysis, "analyse_source_window", fake)
    return calls


def test_a_project_without_a_source_is_told_so(client, project):
    # when
    response = client.post(
        f"/api/projects/{project['id']}/scene-inspect", json={"start_ms": 0, "end_ms": 2000}
    )

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "source_missing"


def test_an_audio_only_source_has_no_frames_to_inspect(client, project, repos, storage):
    # given
    _source(repos, storage, project, height=None, has_video=False)

    # when
    response = client.post(
        f"/api/projects/{project['id']}/scene-inspect", json={"start_ms": 0, "end_ms": 2000}
    )

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_video_stream"


def test_a_frame_is_sampled_at_the_readable_height(client, project, repos, storage, fake_analysis):
    # given: a 1080p source
    _source(repos, storage, project, height=1080)

    # when
    response = client.post(
        f"/api/projects/{project['id']}/scene-inspect", json={"start_ms": 1000, "end_ms": 4000}
    )

    # then
    assert response.status_code == 200
    body = response.json()
    assert body["text_verified"] is True
    assert body["analysis"]["sampled_height"] == 720
    assert "readable at this resolution" in body["note"]
    assert fake_analysis[0]["analysis_height"] == 720
    assert fake_analysis[0]["scene_fps"] == 1.0


def test_the_source_is_never_stretched_to_meet_the_request(client, project, repos, storage, fake_analysis):
    # given: a 480p source, which is the best evidence that exists
    _source(repos, storage, project, height=480)

    # when
    response = client.post(
        f"/api/projects/{project['id']}/scene-inspect",
        json={"start_ms": 0, "end_ms": 3000, "height": 1080},
    )

    # then: the request is capped at the source, and the answer is honest
    assert response.status_code == 200
    body = response.json()
    assert fake_analysis[0]["analysis_height"] == 480
    assert body["analysis"]["sampled_height"] == 480
    assert body["text_verified"] is False
    assert "cannot supply" in body["note"]


def test_a_window_is_clamped_to_the_source_duration(client, project, repos, storage, fake_analysis):
    # given: a 60 second source
    _source(repos, storage, project, height=1080)

    # when
    response = client.post(
        f"/api/projects/{project['id']}/scene-inspect", json={"start_ms": 58_000, "end_ms": 90_000}
    )

    # then
    assert response.status_code == 200
    assert fake_analysis[0]["start_ms"] == 58_000
    assert fake_analysis[0]["end_ms"] == 60_000


def test_a_window_shorter_than_half_a_second_is_rejected(client, project, repos, storage, fake_analysis):
    # given
    _source(repos, storage, project, height=1080)

    # when
    response = client.post(
        f"/api/projects/{project['id']}/scene-inspect", json={"start_ms": 5000, "end_ms": 5100}
    )

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_span"
    assert fake_analysis == []
