"""Ending review: evidence, not automatic trimming.

A clip boundary can be sentence-aligned and still be visually wrong. This module
looks at the transcript around the candidate end and at scene cuts in a small
window, scores suspicion, and offers alternative source-time boundaries. It
never changes the candidate.
"""

from __future__ import annotations

import logging
from pathlib import Path

from clipforge.media.visual import SceneAnalysis, analyse_source_window
from clipforge.ranking.sentences import Sentence

logger = logging.getLogger(__name__)
WINDOW_BEFORE_MS = 7_000
WINDOW_AFTER_MS = 5_000
NEAR_CUT_MS = 3_000


def _sentence_at(sentences: list[Sentence], end_ms: int) -> tuple[Sentence | None, Sentence | None, Sentence | None]:
    index = -1
    for position, sentence in enumerate(sentences):
        if sentence.start_ms < end_ms:
            index = position
    if index < 0:
        return None, None, sentences[0] if sentences else None
    current = sentences[index]
    previous = sentences[index - 1] if index > 0 else None
    following = sentences[index + 1] if index + 1 < len(sentences) else None
    return previous, current, following


def _nearest(cuts: list[int], target: int, *, before: bool) -> int | None:
    candidates = [cut for cut in cuts if (cut <= target if before else cut >= target)]
    if not candidates:
        return None
    return min(candidates, key=lambda value: abs(value - target))


def _alternative(end_ms: int, label: str, reason: str, confidence: float, source: str) -> dict:
    return {
        "end_ms": int(end_ms),
        "label": label,
        "reason": reason,
        "confidence": round(max(0.0, min(0.95, confidence)), 2),
        "source": source,
    }


def review_ending(
    *,
    candidate_start_ms: int,
    candidate_end_ms: int,
    sentences: list[Sentence],
    source_path: Path | None = None,
    source_duration_ms: int | None = None,
    cache_dir: Path | None = None,
    scene_fps: float | None = None,
    analysis_height: int = 144,
    source_height: int | None = None,
) -> dict:
    previous, current, following = _sentence_at(sentences, candidate_end_ms)
    evidence: list[str] = []
    suspicion = 0.0
    scene_analysis: SceneAnalysis | None = None

    if source_path is not None:
        try:
            scene_analysis = analyse_source_window(
                source_path,
                max(0, candidate_end_ms - WINDOW_BEFORE_MS),
                min(source_duration_ms or 10**12, candidate_end_ms + WINDOW_AFTER_MS),
                cache_dir=cache_dir,
                source_duration_ms=source_duration_ms,
                scene_fps=scene_fps,
                analysis_height=analysis_height,
                source_height=source_height,
            )
        except Exception as exc:  # evidence is best-effort; it must not block analysis
            logger.debug("Ending scene analysis failed: %s", exc)

    cuts = scene_analysis.cuts if scene_analysis else []
    cut_before = _nearest(cuts, candidate_end_ms, before=True)
    cut_after = _nearest(cuts, candidate_end_ms, before=False)
    if cut_before is not None and 0 <= candidate_end_ms - cut_before <= NEAR_CUT_MS:
        suspicion += 0.35
        evidence.append(
            f"A scene cut at {cut_before / 1000:.2f}s is {(candidate_end_ms - cut_before) / 1000:.2f}s "
            "before the chosen end."
        )
    if cut_after is not None and 0 <= cut_after - candidate_end_ms <= NEAR_CUT_MS:
        suspicion += 0.15
        evidence.append(
            f"A scene cut at {cut_after / 1000:.2f}s is {(cut_after - candidate_end_ms) / 1000:.2f}s "
            "after the chosen end."
        )

    before_scene = None
    after_scene = None
    if scene_analysis:
        for scene in scene_analysis.scenes:
            if scene.start_ms <= candidate_end_ms <= scene.end_ms:
                before_scene = scene
            if scene.start_ms >= candidate_end_ms and after_scene is None:
                after_scene = scene
        if before_scene and after_scene and before_scene.content_type != after_scene.content_type:
            suspicion += 0.20
            evidence.append(
                f"The picture changes from {before_scene.content_type.replace('_', ' ')} to "
                f"{after_scene.content_type.replace('_', ' ')} at {after_scene.start_ms / 1000:.2f}s."
            )
            if after_scene.content_type in {"slide", "screen_recording"} and before_scene.content_type == "talking_head":
                suspicion += 0.10
                evidence.append("The camera cuts to on-screen content immediately after the chosen end.")

    if current is None and previous is not None:
        evidence.append("No spoken sentence spans the chosen end; the last sentence finished earlier.")
    if current is not None and not current.ends_with_terminator:
        suspicion += 0.12
        evidence.append(f"The last sentence does not finish: '{current.text[-80:]}'.")
    if following is not None and following.start_ms - candidate_end_ms <= 2_500:
        suspicion += 0.15
        evidence.append(
            f"Speech continues {(following.start_ms - candidate_end_ms) / 1000:.2f}s after the end: "
            f"'{following.text[:80]}'."
        )

    if source_duration_ms and candidate_end_ms >= source_duration_ms - 1_000:
        suspicion = 0.0
        evidence.append("The candidate reaches the end of the source; there is no trailing source material.")

    alternatives: list[dict] = [
        _alternative(
            candidate_end_ms,
            "Keep the current ending",
            "No automatic change has been made.",
            1.0 - min(0.9, suspicion),
            "current",
        )
    ]
    # A cut a few frames before the out point means the last visible frames are
    # already the next shot. That is worth offering separately from a normal
    # scene trim, which is only a reasonable choice when the cut is older.
    cut_at_end = (
        cut_before is not None
        and 0 <= candidate_end_ms - cut_before <= 500
        and cut_before - candidate_start_ms >= 3_000
    )
    if cut_at_end:
        alternatives.append(
            _alternative(
                cut_before,
                "Trim the final cut",
                "Removes the last few frames, which belong to the next shot.",
                0.82,
                "scene",
            )
        )
    elif cut_before is not None and candidate_end_ms - cut_before >= 1_000 and cut_before - candidate_start_ms >= 3_000:
        alternatives.append(
            _alternative(
                cut_before,
                "End on the cut",
                "Ends at the last visual cut before the current end.",
                0.72,
                "scene",
            )
        )
    if previous is not None and previous.end_ms - candidate_start_ms >= 3_000 and previous.end_ms < candidate_end_ms - 300:
        previous_confidence = 0.62 if current and not current.ends_with_terminator else 0.5
        if cut_at_end:
            previous_confidence = 0.76
        alternatives.append(
            _alternative(
                previous.end_ms,
                "End on the previous sentence",
                "Trims the unfinished or trailing sentence.",
                previous_confidence,
                "sentence",
            )
        )
    if following is not None and following.end_ms <= (source_duration_ms or following.end_ms) and following.end_ms <= candidate_end_ms + 6_000:
        finish_confidence = 0.55
        if cut_at_end:
            finish_confidence = 0.32
        alternatives.append(
            _alternative(
                following.end_ms,
                "Finish the next sentence",
                "Includes the sentence that continues after the current end.",
                finish_confidence,
                "sentence",
            )
        )

    seen: set[int] = set()
    unique: list[dict] = []
    for alternative in sorted(alternatives, key=lambda item: (-item["confidence"], item["end_ms"])):
        if alternative["end_ms"] in seen:
            continue
        seen.add(alternative["end_ms"])
        unique.append(alternative)

    suspicious = suspicion >= 0.45
    if not suspicious:
        evidence.append("No strong evidence of an unrelated trailing shot.")
    if source_path is None:
        evidence.append("Visual ending review is unavailable because this project has no video.")
    return {
        "version": 2,
        "reviewed": "pending",
        "suspicious": suspicious,
        "suspicion": round(min(1.0, suspicion), 2),
        "start_ms": candidate_start_ms,
        "end_ms": candidate_end_ms,
        "current_sentence": current.text if current else None,
        "next_sentence": following.text if following else None,
        "last_sentence_ends_with_terminator": bool(current and current.ends_with_terminator),
        "nearest_cut_before_ms": cut_before,
        "nearest_cut_after_ms": cut_after,
        "evidence": evidence,
        "alternatives": unique,
        # What the picture evidence was actually taken from. A composition note
        # derived from a 144p proxy must not read like a verified close-up.
        "analysis": dict(scene_analysis.analysis) if scene_analysis else {},
        "analysis_warnings": list(scene_analysis.warnings) if scene_analysis else [],
    }
