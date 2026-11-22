"""Rendered-output review findings and bounded, deterministic repairs."""

from __future__ import annotations

from clipforge.quality.review import (
    apply_repair,
    build_review_report,
    choose_repair,
)
from clipforge.rendering.spec import build_spec


def _gate(**overrides) -> dict:
    checks = [
        {"id": "streams", "label": "Streams", "status": "pass", "detail": "ok"},
        {"id": "dimensions", "label": "Dimensions", "status": "pass", "detail": "ok"},
        {"id": "duration", "label": "Duration", "status": "pass", "detail": "ok"},
    ]
    checks.extend(overrides.pop("checks", []))
    return {
        "version": 1,
        "status": overrides.pop("status", "pass"),
        "blocking": overrides.pop("blocking", False),
        "summary": "Export gate passed.",
        "checks": checks,
        "output": {"width": 720, "height": 1280, "duration_ms": 4_000},
        **overrides,
    }


def _spec(**overrides):
    payload = {
        "source_id": "src",
        "segments": [
            {"start_ms": 0, "end_ms": 2_000, "role": "main"},
            {"start_ms": 4_000, "end_ms": 6_000, "role": "payoff"},
        ],
        "aspect": "9:16",
        "resolution": 720,
        "captions_enabled": True,
        "captions_burn_in": True,
    }
    payload.update(overrides)
    return build_spec(**payload)


def _source() -> dict:
    return {"id": "src", "duration_ms": 10_000, "has_audio": True, "width": 1920, "height": 1080}


def _render(spec) -> dict:
    return {"id": "render-1", "edl_version": 1, "spec": spec.model_dump()}


def _transcript() -> list[dict]:
    return [
        {"start_ms": 0, "end_ms": 2_000, "text": " The first complete thought.", "words": []},
        {"start_ms": 2_200, "end_ms": 4_000, "text": " A second sentence.", "words": []},
        {"start_ms": 4_000, "end_ms": 6_000, "text": " A third sentence ends here.", "words": []},
    ]


def test_caption_overflow_produces_a_stable_repairable_finding():
    # given
    report = build_review_report(
        render=_render(_spec()),
        spec=_spec(),
        source=_source(),
        gate=_gate(
            status="fail",
            blocking=True,
            checks=[{
                "id": "captions",
                "label": "Caption bounds",
                "status": "fail",
                "detail": "Caption line is 900px wide in a 720px frame.",
                "width_px": 900,
                "limit_px": 640,
            }],
        ),
        transcript_segments=_transcript(),
    )

    # then
    caption = next(item for item in report["findings"] if item["id"] == "gate_captions")
    assert caption["severity"] == "blocking"
    assert caption["auto_repairable"] is True
    assert caption["proposed_repair"]["kind"] == "captions_compact"
    assert report["status"] == "fail"


def test_caption_repair_shrinks_the_line_and_revalidates():
    # given
    spec = _spec()
    original = spec.captions.font_size
    report = build_review_report(
        render=_render(spec),
        spec=spec,
        source=_source(),
        gate=_gate(checks=[{
            "id": "captions", "label": "Captions", "status": "warn",
            "detail": "over", "width_px": 700, "limit_px": 640,
        }]),
        transcript_segments=_transcript(),
    )
    repair = choose_repair(report, applied=set(), attempts_left=2)

    # when
    updated, change = apply_repair(spec, repair, source_duration_ms=10_000)

    # then
    assert change["applied"] is True
    assert updated.captions.font_size < original
    assert updated.captions.max_line_width_pct <= 0.86
    assert updated.segments[0].start_ms == spec.segments[0].start_ms


def test_a_repair_kind_is_attempted_once_and_the_loop_terminates():
    # given a report whose only supported repair is already applied
    report = build_review_report(
        render=_render(_spec()),
        spec=_spec(),
        source=_source(),
        gate=_gate(checks=[{
            "id": "captions", "label": "Captions", "status": "warn",
            "detail": "over", "width_px": 700, "limit_px": 640,
        }]),
        transcript_segments=_transcript(),
    )

    # then
    assert choose_repair(report, applied={"captions_compact"}, attempts_left=2) is None
    assert choose_repair(report, applied=set(), attempts_left=0) is None


def test_duplicate_source_segments_are_detected_and_removable():
    # given
    spec = _spec(
        segments=[
            {"start_ms": 0, "end_ms": 2_000, "role": "main"},
            {"start_ms": 0, "end_ms": 2_000, "role": "main"},
        ]
    )
    report = build_review_report(
        render=_render(spec), spec=spec, source=_source(), gate=_gate(),
        transcript_segments=_transcript(),
    )
    finding = next(item for item in report["findings"] if item["id"] == "edl_duplicate_segment")
    assert finding["proposed_repair"]["kind"] == "dedupe_segments"

    # when
    updated, change = apply_repair(spec, finding["proposed_repair"], source_duration_ms=10_000)

    # then
    assert change["applied"] is True
    assert len(updated.segments) == 1


def test_a_suspicious_ending_can_snap_to_a_sentence_boundary():
    # given a last segment that ends inside a spoken sentence
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 3_200, "role": "main"}])
    report = build_review_report(
        render=_render(spec),
        spec=spec,
        source=_source(),
        gate=_gate(checks=[{
            "id": "ending",
            "label": "Ending review",
            "status": "warn",
            "detail": "Speech continues after the end.",
            "suspicious": True,
            "reviewed": "pending",
            "alternatives": [{"end_ms": 4_000, "source": "sentence", "confidence": 0.8}],
        }]),
        transcript_segments=_transcript(),
    )
    finding = next(item for item in report["findings"] if item["id"] == "gate_ending")
    assert finding["auto_repairable"] is True

    # when
    updated, change = apply_repair(spec, finding["proposed_repair"], source_duration_ms=10_000)

    # then
    assert change["applied"] is True
    assert updated.segments[-1].end_ms == 4_000
    assert updated.total_duration_ms == 4_000


def test_a_repair_that_would_make_the_clip_too_short_is_refused():
    # given
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 2_100, "role": "main"}])
    repair = {"kind": "snap_end_to_sentence", "params": {"end_ms": 500}}

    # when
    updated, change = apply_repair(spec, repair, source_duration_ms=10_000)

    # then
    assert change["applied"] is False
    assert updated.segments[0].end_ms == 2_100


def test_a_clean_render_has_no_findings_and_needs_no_repair():
    # given
    spec = _spec()
    report = build_review_report(
        render=_render(spec),
        spec=spec,
        source={**_source(), "has_audio": False},
        gate=_gate(),
        transcript_segments=_transcript(),
        audio={"measured": True, "mean_db": -18.0, "max_db": -1.0},
    )

    # then
    assert report["status"] in {"pass", "warn"}
    assert choose_repair(report, applied=set(), attempts_left=2) is None


def test_audio_clipping_is_reported_when_measured():
    spec = _spec()
    report = build_review_report(
        render=_render(spec),
        spec=spec,
        source=_source(),
        gate=_gate(),
        transcript_segments=_transcript(),
        audio={"measured": True, "mean_db": -8.0, "max_db": -0.05},
    )

    clipping = next(item for item in report["findings"] if item["id"] == "audio_clipping")
    assert clipping["severity"] == "warning"
    assert clipping["auto_repairable"] is False
