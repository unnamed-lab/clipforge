"""Layout settings and the composition-preview contract.

The preview must be rendered by the same composition plan the export uses, so
these tests assert that request values reach the canonical spec and that the
response exposes the tracked ranges and panel geometry the UI reviews.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipforge.api import routes_renders
from clipforge.api.app import create_app
from clipforge.config import Settings
from clipforge.rendering.composition import CompositionPlan
from clipforge.rendering.spec import (
    CompositionDecision,
    FaceCamRange,
    FaceCamTrack as SpecFaceCamTrack,
    Rect,
    SegmentSpan,
)
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


def _source(repos, storage, project, *, has_video: bool = True) -> dict:
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
        height=1080 if has_video else None,
        fps=30.0 if has_video else None,
        has_audio=True,
        video_codec="h264" if has_video else None,
        audio_codec="aac",
        container="mp4" if has_video else "m4a",
        source_url="https://example.com/watch?v=abc",
        probe={},
        has_video=has_video,
    )


def _plan(*, mode: str = "stack", fallback: bool = False) -> CompositionPlan:
    decision = CompositionDecision(
        mode=mode,
        background="solid",
        background_color="#0B0D11",
        facecam=Rect(x_pct=0.70, y_pct=0.65, width_pct=0.25, height_pct=0.30),
        facecam_state="visible",
        face_cam_position="top",
        stack_split_pct=55,
        gameplay_rect=Rect(x_pct=0.10, y_pct=0.0, width_pct=0.60, height_pct=1.0),
        facecam_panel_rect=Rect(x_pct=0.66, y_pct=0.60, width_pct=0.30, height_pct=0.40),
        caption_band={"top_pct": 0.34, "bottom_pct": 0.51},
    )
    if fallback:
        decision.mode = "crop"
        decision.facecam = None
        decision.gameplay_rect = None
        decision.facecam_panel_rect = None
        decision.warnings = ["No face-cam was detected for this range."]
    track = SpecFaceCamTrack(
        ranges=[
            FaceCamRange(
                start_ms=0,
                end_ms=10_000,
                state="visible" if not fallback else "missing",
                rect=Rect(x_pct=0.70, y_pct=0.65, width_pct=0.25, height_pct=0.30)
                if not fallback
                else None,
                confidence=0.9 if not fallback else 0.0,
                reason="tracked",
            )
        ],
        sample_fps=1.0,
        warnings=[],
    )
    return CompositionPlan(
        segments=[
            SegmentSpan(
                start_ms=0,
                end_ms=10_000,
                role="main",
                composition=decision,
            )
        ],
        warnings=[],
        facecam_track=track,
    )


def _patch_preview(monkeypatch, plan: CompositionPlan, captured: list[dict]):
    def fake_expand(spec, *args, **kwargs):
        captured.append(spec)
        return plan

    def fake_preview(spec, *, segment, source, cwd, at_ms=None, **kwargs):
        target = Path(cwd) / "preview.jpg"
        target.write_bytes(b"jpeg" * 512)
        return SimpleNamespace(output_name="preview.jpg", cwd=Path(cwd))

    monkeypatch.setattr(routes_renders, "expand_composition", fake_expand)
    monkeypatch.setattr(routes_renders, "build_preview_command", fake_preview)
    monkeypatch.setattr(routes_renders, "execute", lambda *args, **kwargs: None)


def test_layout_settings_round_trip_through_project_overrides(client, project):
    # given a saved stack with a top face-cam, a split and a time-ranged correction
    body = {
        "overrides": {
            "export": {
                "composition": "stack",
                "face_cam_position": "top",
                "stack_split_pct": 55,
                "adaptive_stack": True,
                "face_cam_overrides": [
                    {
                        "start_ms": 1_000,
                        "end_ms": 4_000,
                        "rect": {"x_pct": 0.1, "y_pct": 0.1, "width_pct": 0.2, "height_pct": 0.2},
                        "reason": "manual fix",
                    }
                ],
            }
        }
    }

    # when
    response = client.patch(f"/api/projects/{project['id']}/settings", json=body)
    loaded = client.get(f"/api/projects/{project['id']}/settings")

    # then
    assert response.status_code == 200
    export = loaded.json()["resolved"]["export"]
    assert export["composition"] == "stack"
    assert export["face_cam_position"] == "top"
    assert export["stack_split_pct"] == 55
    assert export["adaptive_stack"] is True
    assert export["face_cam_overrides"][0]["reason"] == "manual fix"


def test_preview_uses_the_requested_layout_and_reports_tracked_geometry(
    client, project, repos, storage, monkeypatch
):
    # given a source and a request that differs from the saved defaults
    _source(repos, storage, project)
    captured: list[dict] = []
    _patch_preview(monkeypatch, _plan(), captured)

    # when
    response = client.post(
        f"/api/projects/{project['id']}/renders/composition-preview",
        json={
            "segments": [{"start_ms": 0, "end_ms": 10_000, "role": "main"}],
            "aspect": "9:16",
            "resolution": 1080,
            "composition": "stack",
            "face_cam_position": "top",
            "stack_split_pct": 55,
            "adaptive_stack": True,
            "face_cam_overrides": [
                {
                    "start_ms": 1_000,
                    "end_ms": 4_000,
                    "rect": {"x_pct": 0.1, "y_pct": 0.1, "width_pct": 0.2, "height_pct": 0.2},
                }
            ],
        },
    )

    # then
    assert response.status_code == 200
    body = response.json()
    assert captured[0].video.face_cam_position == "top"
    assert captured[0].video.stack_split_pct == 55
    assert captured[0].video.adaptive_stack is True
    assert captured[0].video.face_cam_overrides[0].reason == "Manual face-cam region"
    assert body["face_cam_position"] == "top"
    assert body["stack_split_pct"] == 55
    assert body["facecam"]["detected"] is True
    assert body["facecam"]["state"] == "visible"
    assert body["facecam"]["ranges"][0]["state"] == "visible"
    assert body["gameplay_rect"]["x_pct"] == 0.10
    assert body["facecam_panel_rect"]["x_pct"] == 0.66
    assert body["fallback"] is False


def test_preview_reports_a_fallback_when_the_face_cam_is_missing(
    client, project, repos, storage, monkeypatch
):
    # given a stack request that the plan could not draw
    _source(repos, storage, project)
    _patch_preview(monkeypatch, _plan(mode="crop", fallback=True), [])

    # when
    response = client.post(
        f"/api/projects/{project['id']}/renders/composition-preview",
        json={
            "segments": [{"start_ms": 0, "end_ms": 10_000, "role": "main"}],
            "aspect": "9:16",
            "resolution": 1080,
            "composition": "stack",
            "face_cam_position": "top",
        },
    )

    # then
    assert response.status_code == 200
    body = response.json()
    assert body["fallback"] is True
    assert body["decision"]["mode"] == "crop"
    assert body["facecam"]["detected"] is False
    assert any("No face-cam" in warning for warning in body["warnings"])


def test_preview_rejects_an_audio_only_source(client, project, repos, storage):
    # given a source with no video stream
    _source(repos, storage, project, has_video=False)

    # when
    response = client.post(
        f"/api/projects/{project['id']}/renders/composition-preview",
        json={"segments": [{"start_ms": 0, "end_ms": 10_000, "role": "main"}], "aspect": "9:16"},
    )

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_video_stream"


def test_saved_manual_override_is_used_when_the_request_omits_it(
    client, project, repos, storage, monkeypatch
):
    # given a saved correction on the project
    client.patch(
        f"/api/projects/{project['id']}/settings",
        json={
            "overrides": {
                "export": {
                    "composition": "stack",
                    "face_cam_overrides": [
                        {
                            "start_ms": 2_000,
                            "end_ms": 5_000,
                            "rect": {"x_pct": 0.05, "y_pct": 0.05, "width_pct": 0.2, "height_pct": 0.2},
                        }
                    ],
                }
            }
        },
    )
    _source(repos, storage, project)
    captured: list[dict] = []
    _patch_preview(monkeypatch, _plan(), captured)

    # when
    response = client.post(
        f"/api/projects/{project['id']}/renders/composition-preview",
        json={
            "segments": [{"start_ms": 0, "end_ms": 10_000, "role": "main"}],
            "aspect": "9:16",
            "composition": "stack",
        },
    )

    # then
    assert response.status_code == 200
    assert captured[0].video.face_cam_overrides[0].start_ms == 2_000
    assert captured[0].video.face_cam_overrides[0].rect.x_pct == 0.05
