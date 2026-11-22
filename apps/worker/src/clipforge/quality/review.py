"""Rendered-output review and the bounded, deterministic repair plan.

This module reviews the file that was actually encoded. Deterministic checks
read the gate's measurements, the canonical EDL, the transcript and the
face-cam track. A model review, when configured, receives that measured
evidence and must label its own basis; it never pretends to have watched frames.

Repairs are limited to mechanical corrections that deterministic code can apply
and re-validate. Narrative guesses are reported for a human, not applied.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from clipforge.media.process import ProcessFailed, require, run
from clipforge.quality.boundaries import review_ending
from clipforge.ranking.sentences import build_transcript_model
from clipforge.rendering.spec import RenderSpec

SEVERITY_ORDER = {"blocking": 0, "warning": 1, "info": 2}
MAX_REPAIR_KINDS = {"captions_compact", "snap_end_to_sentence", "dedupe_segments", "contain_edges"}


def _finding(
    identifier: str,
    *,
    severity: str,
    category: str,
    title: str,
    detail: str,
    output_range: dict | None = None,
    source_range: dict | None = None,
    evidence: list[str] | None = None,
    confidence: float | None = None,
    auto_repairable: bool = False,
    proposed_repair: dict | None = None,
    validation: str = "",
) -> dict:
    return {
        "id": identifier,
        "severity": severity,
        "category": category,
        "title": title,
        "detail": detail,
        "output_range": output_range,
        "source_range": source_range,
        "evidence": evidence or [],
        "confidence": confidence,
        "auto_repairable": bool(auto_repairable and proposed_repair),
        "proposed_repair": proposed_repair,
        "validation": validation or (
            "Re-render and repeat this check." if proposed_repair else "Human review."
        ),
    }


def _gate_findings(gate: dict) -> list[dict]:
    findings: list[dict] = []
    for check in gate.get("checks") or []:
        status = check.get("status")
        if status == "pass":
            continue
        identifier = f"gate_{check.get('id', 'check')}"
        if check.get("id") == "captions":
            width = float(check.get("width_px") or 0)
            limit = float(check.get("limit_px") or 0)
            over_limit = bool(limit and width > limit + 1) or width > 0 and not limit
            repair = {"kind": "captions_compact", "params": {}} if over_limit else None
            findings.append(
                _finding(
                    identifier,
                    severity="blocking" if status == "fail" else "warning",
                    category="visual",
                    title="Caption bounds",
                    detail=str(check.get("detail") or ""),
                    output_range=None,
                    evidence=[f"caption_width_px={width:.0f}", f"limit_px={limit:.0f}"],
                    auto_repairable=bool(repair),
                    proposed_repair=repair,
                )
            )
            continue
        if check.get("id") == "ending":
            suspicious = bool(check.get("suspicious"))
            reviewed = check.get("reviewed") or "pending"
            alternatives = check.get("alternatives") or []
            sentence_alternative = next(
                (
                    item
                    for item in alternatives
                    if item.get("source") == "sentence"
                    and float(item.get("confidence") or 0) >= 0.6
                ),
                None,
            )
            repair = None
            if suspicious and sentence_alternative:
                repair = {
                    "kind": "snap_end_to_sentence",
                    "params": {"end_ms": int(sentence_alternative["end_ms"])},
                }
            findings.append(
                _finding(
                    "gate_ending",
                    severity="warning" if status == "warn" else "info",
                    category="editorial",
                    title="Ending review",
                    detail=str(check.get("detail") or ""),
                    evidence=list(check.get("evidence") or [])[:6],
                    auto_repairable=bool(repair and reviewed == "pending"),
                    proposed_repair=repair if reviewed == "pending" else None,
                )
            )
            continue
        findings.append(
            _finding(
                identifier,
                severity="blocking" if status == "fail" else "warning",
                category=_category_for(str(check.get("id") or "")),
                title=str(check.get("label") or check.get("id") or "Check"),
                detail=str(check.get("detail") or ""),
                evidence=[f"{key}={value}" for key, value in check.items()
                          if key not in {"id", "label", "status", "detail"}][:6],
                auto_repairable=False,
            )
        )
    return findings


def _category_for(check_id: str) -> str:
    if check_id in {"audio_stream", "av_sync", "loudness", "clipping"}:
        return "audio"
    if check_id in {"captions", "composition", "black_frames", "freeze_frames", "overlays"}:
        return "visual"
    if check_id in {"duration_target", "ending"}:
        return "editorial"
    return "technical"


def _edl_findings(spec: RenderSpec, source_duration_ms: int) -> list[dict]:
    findings: list[dict] = []
    seen: dict[tuple[int, int], int] = {}
    duplicate_indexes: list[int] = []
    for index, segment in enumerate(spec.segments):
        if segment.start_ms < 0 or segment.end_ms > source_duration_ms:
            findings.append(
                _finding(
                    "edl_source_out_of_range",
                    severity="blocking",
                    category="editorial",
                    title="Edit points outside the source",
                    detail=f"Segment {index + 1} spans {segment.start_ms}-{segment.end_ms} ms "
                           f"but the source ends at {source_duration_ms} ms.",
                    source_range={"start_ms": segment.start_ms, "end_ms": segment.end_ms},
                    auto_repairable=False,
                )
            )
        key = (segment.start_ms, segment.end_ms)
        if key in seen:
            duplicate_indexes.append(index)
        else:
            seen[key] = index
    if duplicate_indexes:
        findings.append(
            _finding(
                "edl_duplicate_segment",
                severity="warning",
                category="editorial",
                title="Repeated source segment",
                detail="The edit repeats an identical source span.",
                source_range=None,
                evidence=[f"segment_index={index}" for index in duplicate_indexes],
                auto_repairable=True,
                proposed_repair={"kind": "dedupe_segments", "params": {}},
            )
        )
    return findings


def _boundary_findings(
    spec: RenderSpec,
    transcript_segments: list[dict],
    source_duration_ms: int,
) -> list[dict]:
    if not transcript_segments or not spec.segments:
        return []
    model = build_transcript_model(transcript_segments, source_duration_ms)
    if not model.sentences:
        return []
    last = spec.segments[-1]
    if last.end_ms >= source_duration_ms - 250:
        return []
    nearest = min(model.sentences, key=lambda sentence: abs(sentence.end_ms - last.end_ms))
    delta = nearest.end_ms - last.end_ms
    if abs(delta) <= 350:
        return []
    snap_target = None
    if 350 < delta <= 1_200 and last.end_ms + delta - last.start_ms >= 2_000:
        snap_target = nearest.end_ms
    elif -1_200 <= delta < -350 and nearest.end_ms - last.start_ms >= 2_000:
        snap_target = nearest.end_ms
    repair = (
        {"kind": "snap_end_to_sentence", "params": {"end_ms": int(snap_target)}}
        if snap_target is not None
        else None
    )
    return [
        _finding(
            "ends_mid_sentence",
            severity="warning",
            category="editorial",
            title="Clip ends away from a sentence boundary",
            detail=(
                f"The last segment ends at {last.end_ms / 1000:.2f}s; the nearest spoken "
                f"sentence ends at {nearest.end_ms / 1000:.2f}s "
                f"({abs(delta) / 1000:.2f}s away)."
            ),
            output_range={"start_ms": spec.total_duration_ms - last.duration_ms,
                          "end_ms": spec.total_duration_ms},
            source_range={"start_ms": last.start_ms, "end_ms": last.end_ms},
            evidence=[f"nearest_sentence='{nearest.text[:160]}'"],
            auto_repairable=repair is not None,
            proposed_repair=repair,
        )
    ]


def _composition_findings(spec: RenderSpec) -> list[dict]:
    findings: list[dict] = []
    cursor = 0
    for index, segment in enumerate(spec.segments):
        composition = segment.composition
        duration = segment.duration_ms
        if composition is not None:
            for warning in composition.warnings:
                lowered = warning.lower()
                output_range = {"start_ms": cursor, "end_ms": cursor + duration}
                if "left/right edges" in lowered and composition.mode == "crop":
                    findings.append(
                        _finding(
                            "crop_cuts_evidence",
                            severity="warning",
                            category="visual",
                            title="Crop may cut on-screen content",
                            detail=warning,
                            output_range=output_range,
                            source_range={"start_ms": segment.start_ms, "end_ms": segment.end_ms},
                            evidence=[f"composition={composition.mode}", f"content={composition.content_type}"],
                            auto_repairable=True,
                            proposed_repair={"kind": "contain_edges", "params": {"segment_index": index}},
                        )
                    )
                elif "check that its text is" in lowered or "verify the subject has room" in lowered:
                    findings.append(
                        _finding(
                            "slide_may_be_unreadable",
                            severity="warning",
                            category="visual",
                            title="On-screen content may be too small",
                            detail=warning,
                            output_range=output_range,
                            source_range={"start_ms": segment.start_ms, "end_ms": segment.end_ms},
                            evidence=[f"composition={composition.mode}", f"content={composition.content_type}"],
                            auto_repairable=False,
                        )
                    )
                elif "face-cam detection is uncertain" in lowered:
                    findings.append(
                        _finding(
                            "facecam_uncertain",
                            severity="warning",
                            category="visual",
                            title="Webcam region is uncertain",
                            detail=warning,
                            output_range=output_range,
                            source_range={"start_ms": segment.start_ms, "end_ms": segment.end_ms},
                            evidence=[f"facecam_state={composition.facecam_state}"],
                            auto_repairable=False,
                        )
                    )
        cursor += duration
    return findings


def _facecam_jump_findings(spec: RenderSpec) -> list[dict]:
    track = spec.facecam_track
    if track is None:
        return []
    ranges = [item for item in track.ranges if item.rect is not None]
    findings: list[dict] = []
    for previous, current in zip(ranges, ranges[1:]):
        assert previous.rect is not None and current.rect is not None
        jump = max(
            abs(previous.rect.x_pct - current.rect.x_pct),
            abs(previous.rect.y_pct - current.rect.y_pct),
        )
        if jump > 0.10:
            findings.append(
                _finding(
                    "webcam_crop_jump",
                    severity="warning",
                    category="visual",
                    title="Webcam panel moves sharply",
                    detail=(
                        f"The detected webcam region moved by {jump:.0%} of the source frame at "
                        f"{current.start_ms / 1000:.2f}s. Check the panel for a jump."
                    ),
                    source_range={"start_ms": current.start_ms, "end_ms": current.end_ms},
                    evidence=[f"previous_state={previous.state}", f"current_state={current.state}"],
                    auto_repairable=False,
                )
            )
    for item in track.ranges:
        if item.state in {"uncertain", "missing"}:
            findings.append(
                _finding(
                    "facecam_missing_or_uncertain",
                    severity="warning",
                    category="visual",
                    title="Webcam state changed",
                    detail=item.reason or f"The webcam region is {item.state} for this range.",
                    source_range={"start_ms": item.start_ms, "end_ms": item.end_ms},
                    evidence=[f"state={item.state}", f"confidence={item.confidence}"],
                    auto_repairable=False,
                )
            )
            break
    return findings


_MEAN_RE = re.compile(r"mean_volume:\s*(-?[0-9.]+) dB")
_MAX_RE = re.compile(r"max_volume:\s*(-?[0-9.]+) dB")


def measure_audio(path: Path) -> dict:
    """Measure volume levels. Returns measured values, never a guess."""
    try:
        ffmpeg = require("ffmpeg")
        result = run(
            [
                ffmpeg, "-nostdin", "-hide_banner", "-i", str(path),
                "-af", "volumedetect", "-f", "null", "-",
            ],
            timeout_seconds=120,
        )
    except (ProcessFailed, OSError) as exc:
        return {"measured": False, "detail": str(exc)[:300]}
    mean = _MEAN_RE.search(result.stderr)
    peak = _MAX_RE.search(result.stderr)
    if not mean and not peak:
        return {"measured": False, "detail": "volumedetect produced no numeric levels"}
    return {
        "measured": True,
        "mean_db": float(mean.group(1)) if mean else None,
        "max_db": float(peak.group(1)) if peak else None,
    }


def _audio_findings(spec: RenderSpec, source: dict, audio: dict | None) -> list[dict]:
    if not source.get("has_audio") or not audio or not audio.get("measured"):
        return []
    findings: list[dict] = []
    mean_db = audio.get("mean_db")
    max_db = audio.get("max_db")
    if max_db is not None and max_db >= -0.1:
        findings.append(
            _finding(
                "audio_clipping",
                severity="warning",
                category="audio",
                title="Audio may clip",
                detail=f"Measured peak volume is {max_db:.1f} dB; the signal is at or above full scale.",
                evidence=[f"max_db={max_db}"],
                auto_repairable=False,
            )
        )
    if mean_db is not None and mean_db <= -55:
        findings.append(
            _finding(
                "audio_very_quiet",
                severity="warning",
                category="audio",
                title="Audio is unexpectedly quiet",
                detail=f"Measured mean volume is {mean_db:.1f} dB.",
                evidence=[f"mean_db={mean_db}"],
                auto_repairable=False,
            )
        )
    return findings


def build_review_report(
    *,
    render: dict,
    spec: RenderSpec,
    source: dict,
    gate: dict,
    transcript_segments: list[dict] | None = None,
    audio: dict | None = None,
    strategy: str | None = None,
    model_review: dict | None = None,
) -> dict:
    findings = _gate_findings(gate)
    findings.extend(_edl_findings(spec, int(source.get("duration_ms") or 0)))
    findings.extend(_boundary_findings(spec, transcript_segments or [], int(source.get("duration_ms") or 0)))
    findings.extend(_composition_findings(spec))
    findings.extend(_facecam_jump_findings(spec))
    findings.extend(_audio_findings(spec, source, audio))
    findings.sort(key=lambda item: SEVERITY_ORDER.get(item["severity"], 3))

    model_block = model_review or {
        "status": "not_run",
        "reason": "No model review was requested; deterministic checks ran on the rendered file.",
        "findings": [],
        "basis": "deterministic media, EDL and transcript evidence",
    }
    findings.extend(model_block.get("findings") or [])

    blocking = [item for item in findings if item["severity"] == "blocking"]
    repairable = [item for item in findings if item.get("auto_repairable")]
    if blocking:
        status = "fail"
        summary = f"{len(blocking)} blocking finding(s); " + "; ".join(
            item["title"] for item in blocking[:3]
        )
    elif repairable:
        status = "needs_review"
        summary = f"{len(repairable)} finding(s) can be corrected automatically."
    elif findings:
        status = "warn"
        summary = f"{len(findings)} warning(s) need a human look."
    else:
        status = "pass"
        summary = "Rendered output passed the deterministic checks."

    return {
        "version": 1,
        "kind": "rendered_output",
        "render_id": render.get("id"),
        "asset_id": render.get("id"),
        "edl_version": int(render.get("edl_version") or 1),
        "strategy": strategy,
        "gate_status": gate.get("status"),
        "gate_summary": gate.get("summary"),
        "status": status,
        "summary": summary,
        "findings": findings,
        "checks": gate.get("checks") or [],
        "edl": gate.get("edl") or spec.to_edl(),
        "output": gate.get("output") or {},
        "model_review": model_block,
        "deterministic_only": True,
        "remaining_warnings": [
            item["title"] for item in findings if item["severity"] in {"warning", "blocking"}
        ][:12],
    }


def choose_repair(report: dict, *, applied: set[str], attempts_left: int) -> dict | None:
    """Pick one supported repair, once per clip, ordered by severity."""
    if attempts_left <= 0:
        return None
    for finding in report.get("findings") or []:
        repair = finding.get("proposed_repair")
        if not finding.get("auto_repairable") or not repair:
            continue
        kind = repair.get("kind")
        if kind not in MAX_REPAIR_KINDS or kind in applied:
            continue
        return {**repair, "finding_id": finding["id"], "severity": finding["severity"]}
    return None


def apply_repair(spec: RenderSpec, repair: dict, *, source_duration_ms: int) -> tuple[RenderSpec, dict]:
    """Apply one mechanical correction; return a new spec and what changed."""
    updated = spec.model_copy(deep=True)
    kind = repair.get("kind")
    params = repair.get("params") or {}
    changed: dict[str, Any] = {"kind": kind, "finding_id": repair.get("finding_id"), "changes": []}

    if kind == "captions_compact":
        captions = updated.captions
        old_size = captions.font_size
        captions.font_size = max(30, int(round(captions.font_size * 0.88)))
        captions.max_words_per_line = max(2, captions.max_words_per_line - 1)
        captions.max_line_width_pct = min(captions.max_line_width_pct, 0.86)
        if captions.position in {"bottom", "custom"}:
            captions.position_pct = min(captions.position_pct, 78.0)
            captions.margin_v = min(captions.margin_v, 120)
        changed["changes"].append(f"font_size {old_size}->{captions.font_size}")

    elif kind == "snap_end_to_sentence":
        end_ms = int(params.get("end_ms") or 0)
        last = updated.segments[-1]
        if end_ms <= last.start_ms or end_ms > source_duration_ms:
            return spec, {**changed, "applied": False, "reason": "the proposed end is invalid"}
        if end_ms - last.start_ms < 2_000:
            return spec, {**changed, "applied": False, "reason": "the proposed end would make the clip too short"}
        if sum(segment.duration_ms for segment in updated.segments[:-1]) + (end_ms - last.start_ms) < 2_000:
            return spec, {**changed, "applied": False, "reason": "the clip would be shorter than 2 seconds"}
        changed["changes"].append(f"last segment end {last.end_ms}->{end_ms}")
        last.end_ms = end_ms

    elif kind == "dedupe_segments":
        seen: set[tuple[int, int]] = set()
        kept = []
        for segment in updated.segments:
            key = (segment.start_ms, segment.end_ms)
            if key in seen:
                changed["changes"].append(f"removed duplicate segment {segment.start_ms}-{segment.end_ms}")
                continue
            seen.add(key)
            kept.append(segment)
        if len(kept) < len(updated.segments) and sum(seg.duration_ms for seg in kept) >= 2_000:
            updated.segments = kept
        else:
            return spec, {**changed, "applied": False, "reason": "dedupe would remove the whole clip"}

    elif kind == "contain_edges":
        index = int(params.get("segment_index") or 0)
        if index < 0 or index >= len(updated.segments):
            return spec, {**changed, "applied": False, "reason": "segment index is invalid"}
        segment = updated.segments[index]
        composition = segment.composition
        if composition is None:
            return spec, {**changed, "applied": False, "reason": "the segment has no composition decision"}
        composition.mode = "fit"
        composition.background = "blur"
        composition.auto = False
        composition.warnings = [
            warning for warning in composition.warnings if "left/right edges" not in warning.lower()
        ]
        composition.reason = "Contained after review found that cropping could cut on-screen content."
        changed["changes"].append(f"segment {index + 1} composition crop->fit")

    else:
        return spec, {**changed, "applied": False, "reason": "unsupported repair kind"}

    updated.validate_against(source_duration_ms)
    changed["applied"] = True
    changed["edl_version"] = int(updated.version)
    return updated, changed


def repair_endings(
    *,
    spec: RenderSpec,
    transcript_segments: list[dict],
    source_duration_ms: int,
) -> dict | None:
    """Reuse the existing ending reviewer for a deterministic boundary repair."""
    if not transcript_segments:
        return None
    model = build_transcript_model(transcript_segments, source_duration_ms)
    last = spec.segments[-1]
    review = review_ending(
        candidate_start_ms=last.start_ms,
        candidate_end_ms=last.end_ms,
        sentences=model.sentences,
        source_duration_ms=source_duration_ms,
    )
    if not review.get("suspicious"):
        return None
    for alternative in review.get("alternatives") or []:
        if alternative.get("source") != "sentence":
            continue
        if float(alternative.get("confidence") or 0) < 0.6:
            continue
        end_ms = int(alternative.get("end_ms") or 0)
        if 2_000 <= end_ms - last.start_ms < last.duration_ms:
            return {"kind": "snap_end_to_sentence", "params": {"end_ms": end_ms}}
    return None
