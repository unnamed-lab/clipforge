"""Export identity, versioning, reuse and the project manifest."""

from __future__ import annotations

import json

from clipforge.pipeline.exports import (
    aspect_slug,
    build_manifest,
    export_filename,
    slugify,
    source_identity,
    spec_fingerprint,
    strategy_slug,
)
from clipforge.rendering.spec import build_spec


def _spec(**overrides):
    payload = {
        "source_id": "src",
        "segments": [{"start_ms": 0, "end_ms": 4_000, "role": "main"}],
        "aspect": "9:16",
        "resolution": 720,
        "captions_enabled": False,
    }
    payload.update(overrides)
    return build_spec(**payload)


def test_slug_and_export_name_are_readable_and_stable():
    assert slugify("  Customer Perspective / Q4  ") == "customer-perspective-q4"
    assert slugify("日本語", fallback="project") == "project"
    assert strategy_slug("cold_open") == "hook-first"
    assert strategy_slug("question_answer") == "question-answer"
    assert aspect_slug("9:16") == "portrait"
    assert aspect_slug("16:9") == "landscape"
    assert (
        export_filename(
            project_name="Customer Perspective",
            strategy="cold_open",
            aspect="9:16",
            version=1,
        )
        == "customer-perspective__hook-first__portrait__v001.mp4"
    )
    assert export_filename(
        project_name="A", strategy="result_first", aspect="1:1", version=27
    ).endswith("__v027.mp4")


def test_spec_fingerprint_changes_only_with_rendered_decisions():
    # given
    first = _spec()
    same = _spec(title="A different title")
    changed_edit = _spec(segments=[{"start_ms": 0, "end_ms": 5_000, "role": "main"}])

    # then
    assert spec_fingerprint(first) == spec_fingerprint(same)
    assert spec_fingerprint(first) != spec_fingerprint(changed_edit)


def test_source_identity_prefers_the_content_hash():
    assert source_identity({"id": "s", "content_hash": "abc"}) == "abc"
    assert source_identity({"id": "s", "size_bytes": 10, "duration_ms": 5}).startswith("s|10|5")


def test_render_versions_increment_and_identical_edits_are_reusable(settings, repos, project):
    # given
    spec = _spec().model_dump()
    first = repos.renders.create(
        project_id=project["id"],
        job_id=None,
        candidate_id=None,
        spec=spec,
        status="completed",
        spec_hash="hash",
        source_identity="source",
        display_name="project__hook-first__portrait__v001.mp4",
    )

    # when
    second = repos.renders.create(
        project_id=project["id"],
        job_id=None,
        candidate_id=None,
        spec=spec,
        status="completed",
        spec_hash="other",
        source_identity="source",
    )
    reusable = repos.renders.find_reusable(project["id"], spec_hash="hash", source_identity="source")

    # then
    assert (first["version"], second["version"]) == (1, 2)
    assert reusable is not None and reusable["id"] == first["id"]


def test_superseded_exports_are_retained_but_no_longer_current(settings, repos, project):
    # given two completed exports for the same candidate
    first = repos.renders.create(
        project_id=project["id"], job_id=None, candidate_id=None, spec=_spec().model_dump(),
        status="completed", spec_hash="one", source_identity="source",
    )
    second = repos.renders.create(
        project_id=project["id"], job_id=None, candidate_id=None, spec=_spec().model_dump(),
        status="completed", spec_hash="two", source_identity="source",
    )

    # when
    changed = repos.renders.mark_superseded(project["id"], None, second["id"])

    # then
    assert changed == 1
    assert repos.renders.get(first["id"])["superseded"] is True
    assert repos.renders.get(second["id"])["superseded"] is False
    # Both files remain addressable by their stable ids.
    assert repos.renders.get(first["id"])["download_url"]
    assert repos.renders.get(second["id"])["download_url"]


def test_manifest_lists_assets_without_private_storage_paths(settings, repos, project, sample_segments):
    # given a seeded project, transcript and one export
    source = repos.sources.upsert(
        project_id=project["id"], kind="upload", original_name="source.mp4",
        storage_path="C:/private/path/source.mp4", relative_path="projects/x/source.mp4",
        size_bytes=10, duration_ms=10_000, width=1920, height=1080, fps=30.0,
        has_audio=True, video_codec="h264", audio_codec="aac", container="mp4",
        source_url=None, probe={}, has_video=True, content_hash="source-hash",
    )
    repos.transcripts.save(
        project_id=project["id"], source_id=source["id"], fingerprint="fp", language="en",
        language_probability=0.9, model="tiny", duration_ms=10_000, segments=sample_segments,
    )
    run = repos.runs.create(project["id"], {})
    repos.runs.finish(run["id"], status="completed", provider="baseline", warnings=[])
    render = repos.renders.create(
        project_id=project["id"], job_id=None, candidate_id=None, spec=_spec().model_dump(),
        status="completed", spec_hash="hash", source_identity="source-hash",
        display_name="test-project__chronological__portrait__v001.mp4",
    )
    repos.renders.mark_completed(
        render["id"], relative_path="projects/x/renders/r.mp4", size_bytes=1000,
        duration_ms=4_000, width=720, height=1280, checksum="abc", review_status="pass",
    )

    # when
    manifest = build_manifest(repos, project["id"])

    # then
    assert manifest["project"]["slug"] == "test-project"
    assert manifest["source"]["asset_id"] == source["id"]
    assert manifest["analysis"]["candidate_count"] == 0
    assert len(manifest["exports"]) == 1
    exported = manifest["exports"][0]
    assert exported["asset_id"] == render["id"]
    assert exported["filename"].endswith(".mp4")
    assert exported["checksum"] == "abc"
    assert exported["current"] is True
    serialised = json.dumps(manifest)
    assert "C:/private" not in serialised
    assert "storage_path" not in serialised
    assert "relative_path" not in serialised
    assert manifest["provenance"]["source_identity"] == "source-hash"
