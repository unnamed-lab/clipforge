"""Render spec: compression is separate from resolution, and the EDL is honest.

The EDL is the single description used by both preview and export, so the target
dimensions it reports must be the dimensions the encoder will actually produce —
after the compression width cap, not the requested frame size.
"""

from __future__ import annotations

from pathlib import Path

from clipforge.media import fingerprint
from clipforge.pipeline import analysis
from clipforge.rendering.encode import resolve_encode
from clipforge.rendering.spec import build_spec


def _segment(start: int = 1_000, end: int = 7_000) -> dict:
    return {
        "start_ms": start,
        "end_ms": end,
        "role": "main",
        "source_start_ms": start,
        "source_end_ms": end,
    }


def test_requested_resolution_is_kept_separate_from_encoded_width():
    # given
    spec = build_spec(source_id="source-1", segments=[_segment()], aspect="16:9", resolution=1080)

    # then
    assert spec.video.dimensions == (1920, 1080)
    assert spec.output_dimensions == (1920, 1080)


def test_compression_caps_the_output_without_rewriting_the_request():
    # given
    spec = build_spec(
        source_id="source-1",
        segments=[_segment()],
        aspect="16:9",
        resolution=1080,
        compression=resolve_encode("smallest"),
    )

    # then
    assert spec.video.dimensions == (1920, 1080)
    assert spec.output_dimensions == (1280, 720)
    assert spec.encode.preset == "smallest"


def test_edl_reports_the_dimensions_the_encoder_will_produce():
    # given
    spec = build_spec(
        source_id="source-1",
        segments=[_segment()],
        aspect="9:16",
        resolution=1080,
        compression=resolve_encode("smallest"),
    )

    # when
    edl = spec.to_edl()

    # then
    assert edl["target"]["resolution"] == 1080
    assert (edl["target"]["width"], edl["target"]["height"]) == spec.output_dimensions
    assert edl["target"]["compression"] == "smallest"
    # 1080x1920 is capped to 720 wide; the aspect ratio is preserved.
    assert (edl["target"]["width"], edl["target"]["height"]) == (720, 1280)


def test_edl_separates_source_and_output_coordinates():
    # given: two segments taken from different parts of the source
    spec = build_spec(
        source_id="source-1",
        segments=[_segment(1_000, 7_000), _segment(30_000, 34_000)],
    )

    # when
    edl = spec.to_edl()

    # then: the output timeline is contiguous and independent of source time
    assert edl["segments"][0]["source_start_ms"] == 1_000
    assert edl["segments"][0]["output_start_ms"] == 0
    assert edl["segments"][0]["output_end_ms"] == 6_000
    assert edl["segments"][1]["source_start_ms"] == 30_000
    assert edl["segments"][1]["output_start_ms"] == 6_000
    assert edl["total_output_ms"] == 10_000
    assert spec.total_duration_ms == 10_000


def test_summary_marks_the_size_as_an_estimate_and_names_the_compression():
    # given
    spec = build_spec(
        source_id="source-1",
        segments=[_segment()],
        compression=resolve_encode("higher"),
    )

    # when
    summary = spec.summary()

    # then
    assert summary["estimated"] is True
    assert summary["estimated_size_bytes"] > 0
    assert summary["compression"]["preset"] == "higher"
    assert summary["dimensions"] == list(spec.output_dimensions)


def test_the_default_compression_matches_the_balanced_preset():
    spec = build_spec(source_id="source-1", segments=[_segment()])

    assert spec.encode.preset == "balanced"


# ---------------------------------------------------------------------------
# Source identity for transcript caching
# ---------------------------------------------------------------------------


def test_fingerprint_reuses_a_stored_hash_instead_of_rehashing(monkeypatch, repos, project):
    # given
    monkeypatch.setattr(analysis, "sha256_file", lambda path: (_ for _ in ()).throw(AssertionError()))
    source = {"id": "source-1", "project_id": project["id"], "content_hash": "stored-hash"}

    # when
    digest = analysis._source_hash(repos, source, Path("ignored.mp4"))

    # then
    assert digest == "stored-hash"


def test_missing_hash_is_computed_once_and_persisted(monkeypatch, repos, project):
    # given
    calls = {"count": 0}

    def fake_hash(path):
        calls["count"] += 1
        return "computed-hash"

    monkeypatch.setattr(analysis, "sha256_file", fake_hash)
    repos.sources.upsert(
        project_id=project["id"],
        kind="upload",
        original_name="Source",
        storage_path="",
        relative_path="source/a.mp4",
        size_bytes=10,
        duration_ms=1_000,
        width=1920,
        height=1080,
        fps=30.0,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
        container="mp4",
        source_url=None,
        probe={},
    )
    source = repos.sources.find_optional(project["id"])

    # when
    first = analysis._source_hash(repos, source, Path("a.mp4"))
    second = analysis._source_hash(repos, repos.sources.find_optional(project["id"]), Path("a.mp4"))

    # then
    assert first == second == "computed-hash"
    assert calls["count"] == 1


def test_fingerprint_shape_is_unchanged_for_existing_caches():
    # given: the string that was hashed before this task
    from clipforge.media.policy import DEFAULT_QUALITY  # noqa: F401 - import check

    options = analysis.TranscriptionOptions(
        model="small", compute_type="int8", language=None, vad=True
    )

    # when
    fingerprint_value = analysis.analysis_fingerprint(None, options, content_hash="abc")

    # then
    assert fingerprint_value == analysis.build_fingerprint("abc", "small", "int8", None, True)
    assert len(fingerprint_value) == 64


def test_file_fingerprint_utility_matches_its_own_repeat(monkeypatch, tmp_path):
    # given
    path = tmp_path / "sample.bin"
    path.write_bytes(b"clipforge" * 1024)

    # when
    first = fingerprint.sha256_file(path)
    second = fingerprint.sha256_file(path)

    # then
    assert first == second
    assert len(first) == 64
