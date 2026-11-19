"""Resolution-aware analysis and the source/proxy separation.

These tests pin the two promises the pipeline makes about pictures:

* analysis records the resolution its conclusions were drawn from, and says so
  when text-heavy scenes were sampled too small to read;
* a low-resolution proxy is a working copy for analysis only — the retained
  source stays the thing an export reads.
"""

from __future__ import annotations

import uuid

import pytest

from clipforge.db.repositories import serialise_source
from clipforge.media.assets import audio_input, proxy_input, visual_input
from clipforge.media.visual import (
    DEFAULT_ANALYSIS_HEIGHT,
    LOW_EVIDENCE_HEIGHT,
    MAX_SAMPLES,
    TEXT_READABLE_HEIGHT,
    FrameSample,
    SceneAnalysis,
    _aggregate,
    sampling_fps,
)
from clipforge.storage.local import LocalStorage
from clipforge.storage.workspace import ensure_layout


@pytest.fixture()
def storage(settings) -> LocalStorage:
    return LocalStorage(settings.data_dir)


def _file(storage: LocalStorage, key: str, size: int = 2048) -> str:
    path = storage.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return key


def _source(repos, settings, storage, project, *, height: int = 1080, width: int = 1920) -> dict:
    key = f"projects/{project['id']}/source/{uuid.uuid4().hex}.mp4"
    path = storage.path_for(_file(storage, key))
    return repos.sources.upsert(
        project_id=project["id"],
        kind="upload",
        original_name="Source",
        storage_path=str(path),
        relative_path=key,
        size_bytes=2_048,
        duration_ms=120_000,
        width=width,
        height=height,
        fps=30.0,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
        container="mp4",
        source_url=None,
        probe={},
        has_video=True,
        content_hash="content-hash",
    )


def _proxy(repos, project, storage, source, *, width: int = 480, height: int = 270) -> dict:
    key = f"projects/{project['id']}/proxy/{height}p.mp4"
    storage.path_for(_file(storage, key))
    return repos.media.upsert(
        project_id=project["id"],
        source_id=source["id"],
        role="proxy",
        storage_path=str(storage.path_for(key)),
        relative_path=key,
        size_bytes=2_048,
        duration_ms=120_000,
        width=width,
        height=height,
        fps=30.0,
        has_audio=False,
        video_codec="h264",
        audio_codec=None,
        container="mp4",
        settings={"limit": 480, "audio": "dropped"},
        source_identity="content-hash",
    )


# ---------------------------------------------------------------------------
# Sampling density
# ---------------------------------------------------------------------------


def test_sampling_rate_is_lowered_so_the_whole_window_stays_covered():
    # given: an hour at 1 fps would need more samples than the budget allows
    duration_ms = 60 * 60 * 1000

    # when
    fps = sampling_fps(duration_ms, 1.0)

    # then: the rate drops instead of the tail being dropped with -frames:v
    assert fps < 1.0
    assert duration_ms / 1000 * fps <= MAX_SAMPLES + 0.5


def test_very_long_windows_keep_a_workable_minimum_rate():
    # given: a six-hour source, where the budget alone would ask for an
    # unusably small frame rate
    duration_ms = 6 * 60 * 60 * 1000

    # when
    fps = sampling_fps(duration_ms, 1.0)

    # then: extraction still advances through the window
    assert fps == pytest.approx(0.1)


def test_short_windows_keep_the_requested_rate():
    assert sampling_fps(60_000, 0.5) == 0.5
    assert sampling_fps(60_000, 1.0) == 1.0


# ---------------------------------------------------------------------------
# Evidence level of a scene decision
# ---------------------------------------------------------------------------


def _sample(time_ms: int, content_type: str, *, text_at_edges: float = 0.0) -> FrameSample:
    return FrameSample(
        time_ms=time_ms,
        content_type=content_type,
        confidence=0.8,
        subject_x=0.5,
        features={
            "edge_density": 0.1,
            "border_edge_density": 0.1,
            "bright_fraction": 0.5,
            "dark_fraction": 0.1,
            "colorfulness": 0.1,
            "std_luma": 0.2,
            "letterboxed": 0.0,
            "text_at_edges": text_at_edges,
            "skin_fraction": 0.0,
            "content_x": 0.0,
            "content_y": 0.0,
            "content_width": 1.0,
            "content_height": 1.0,
        },
    )


def test_a_text_heavy_scene_sampled_small_is_flagged_as_unverified():
    # given
    samples = [_sample(1000 * index, "slide") for index in range(5)]

    # when
    scene = _aggregate(samples, 0, 5000, analysis_height=DEFAULT_ANALYSIS_HEIGHT)

    # then
    assert scene.content_type == "slide"
    assert any("sampled at 144p" in warning and "cannot be read" in warning for warning in scene.warnings)


def test_the_same_scene_sampled_at_a_readable_height_is_not_flagged():
    # given
    samples = [_sample(1000 * index, "slide") for index in range(5)]

    # when
    scene = _aggregate(samples, 0, 5000, analysis_height=TEXT_READABLE_HEIGHT)

    # then
    assert not any("cannot be read" in warning for warning in scene.warnings)


def test_edge_reaching_text_is_treated_as_text_heavy():
    # given: a talking head frame whose information reaches the edges
    samples = [_sample(1000 * index, "talking_head", text_at_edges=1.0) for index in range(4)]

    # when
    scene = _aggregate(samples, 0, 4000, analysis_height=DEFAULT_ANALYSIS_HEIGHT)

    # then
    assert any("sampled at 144p" in warning for warning in scene.warnings)


def test_analysis_evidence_survives_a_cache_round_trip():
    # given
    analysis = SceneAnalysis(
        scenes=[],
        samples=[],
        cuts=[],
        warnings=[],
        analysis={
            "sampled_width": 256,
            "sampled_height": 144,
            "scene_fps": 1.0,
            "source_height": 1080,
            "evidence": "low_resolution",
            "text_verified": False,
            "text_heavy": True,
        },
    )

    # when
    restored = SceneAnalysis.from_dict(analysis.to_dict())

    # then
    assert restored.analysis == analysis.analysis


# ---------------------------------------------------------------------------
# Which file each stage reads
# ---------------------------------------------------------------------------


def test_picture_analysis_prefers_the_proxy(repos, settings, project, storage):
    # given
    source = _source(repos, settings, storage, project)
    _proxy(repos, project, storage, source)

    # when
    path, height = visual_input(repos, storage, source, storage.path_for(source["relative_path"]))

    # then
    assert "proxy" in str(path)
    assert height == 270


def test_picture_analysis_falls_back_to_the_source_at_a_bounded_height(repos, settings, project, storage):
    # given
    source = _source(repos, settings, storage, project)

    # when
    path, height = visual_input(repos, storage, source, storage.path_for(source["relative_path"]))

    # then
    assert path == storage.path_for(source["relative_path"])
    # The fallback never claims to sample at the source's real height.
    assert height == LOW_EVIDENCE_HEIGHT


def test_a_missing_proxy_falls_back_instead_of_failing(repos, settings, project, storage):
    # given
    source = _source(repos, settings, storage, project)
    proxy = _proxy(repos, project, storage, source)
    storage.path_for(proxy["relative_path"]).unlink()

    # when
    path, height = visual_input(repos, storage, source, storage.path_for(source["relative_path"]))

    # then
    assert path == storage.path_for(source["relative_path"])
    assert height == LOW_EVIDENCE_HEIGHT
    assert proxy_input(repos, storage, source) is None


def test_audio_input_returns_the_extracted_track(repos, settings, project, storage):
    # given
    source = _source(repos, settings, storage, project)
    key = f"projects/{project['id']}/audio/audio.wav"
    storage.path_for(_file(storage, key))
    repos.media.upsert(
        project_id=project["id"],
        source_id=source["id"],
        role="audio",
        storage_path=str(storage.path_for(key)),
        relative_path=key,
        size_bytes=2_048,
        duration_ms=120_000,
        width=None,
        height=None,
        fps=None,
        has_audio=True,
        video_codec=None,
        audio_codec="pcm_s16le",
        container="wav",
        settings={"sample_rate": 16_000, "channels": 1},
        source_identity="content-hash",
    )

    # when / then
    assert audio_input(repos, storage, source) == storage.path_for(key)


def test_no_audio_asset_means_none_rather_than_the_video(repos, settings, project, storage):
    # given
    source = _source(repos, settings, storage, project)

    # when / then
    assert audio_input(repos, storage, source) is None


# ---------------------------------------------------------------------------
# The proxy can never be an export source
# ---------------------------------------------------------------------------


def test_the_proxy_has_its_own_row_and_leaves_the_source_untouched(repos, settings, project, storage):
    # given
    source = _source(repos, settings, storage, project, height=1080, width=1920)
    proxy = _proxy(repos, project, storage, source)

    # when
    retained = repos.sources.get(project["id"])

    # then: the row an export reads still describes the original
    assert retained["height"] == 1080
    assert retained["relative_path"] == source["relative_path"]
    assert retained["size_bytes"] == 2_048
    assert retained["id"] == source["id"]
    # ...and the proxy is a separate row with its own dimensions and no audio.
    assert proxy["height"] == 270
    assert proxy["has_audio"] == 0
    assert proxy["role"] == "proxy"
    assert proxy["relative_path"] != retained["relative_path"]


def test_serialised_source_hides_internal_fields_but_keeps_the_download_summary(repos, settings, project, storage):
    # given
    source = _source(repos, settings, storage, project)
    repos.sources.upsert(
        project_id=project["id"],
        kind="url",
        original_name="Source",
        storage_path=str(storage.path_for(source["relative_path"])),
        relative_path=source["relative_path"],
        size_bytes=2_048,
        duration_ms=120_000,
        width=1920,
        height=1080,
        fps=30.0,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
        container="mp4",
        source_url="https://example.com/watch?v=abc",
        probe={},
        has_video=True,
        content_hash="content-hash",
        download={"selector": "137+140", "height": 1080, "approximate": True},
    )

    # when
    serialised = serialise_source(repos.sources.get(project["id"]))

    # then
    assert "storage_path" not in serialised
    assert "content_hash" not in serialised
    assert serialised["has_video"] is True
    assert serialised["download"]["selector"] == "137+140"


def test_replacing_a_proxy_keeps_one_row_per_role(repos, settings, project, storage):
    # given
    source = _source(repos, settings, storage, project)
    first = _proxy(repos, project, storage, source, width=480, height=270)
    second_key = f"projects/{project['id']}/proxy/360p.mp4"
    storage.path_for(_file(storage, second_key))

    # when
    second = repos.media.upsert(
        project_id=project["id"],
        source_id=source["id"],
        role="proxy",
        storage_path=str(storage.path_for(second_key)),
        relative_path=second_key,
        size_bytes=900,
        duration_ms=120_000,
        width=640,
        height=360,
        fps=30.0,
        has_audio=False,
        video_codec="h264",
        audio_codec=None,
        container="mp4",
        settings={"limit": 360},
        source_identity="content-hash",
    )

    # then
    assert second["id"] == first["id"]
    assert repos.media.find(source["id"], "proxy")["relative_path"] == second_key
    assert repos.media.delete(source["id"], "proxy")["relative_path"] == second_key
    assert repos.media.find(source["id"], "proxy") is None


def test_layout_keeps_working_copies_out_of_the_source_directory(settings, project):
    # when
    paths = ensure_layout(settings, project["id"])

    # then
    assert paths.name == project["id"]
    assert (paths / "source").exists()
    assert (paths / "proxy").exists()
