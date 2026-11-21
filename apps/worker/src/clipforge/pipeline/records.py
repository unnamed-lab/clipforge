"""Build the persisted shape of a candidate.

Both the first analysis and an additional discovery pass write candidates with
the same fields. Keeping the ending review and the speaker lookup here means a
second pass cannot silently produce a candidate the editor does not understand.
"""

from __future__ import annotations

import logging
from pathlib import Path

from clipforge.db.repositories import new_id
from clipforge.pipeline.briefing import summarise
from clipforge.quality.boundaries import review_ending
from clipforge.ranking.score import ScoredCandidate
from clipforge.ranking.sentences import TranscriptModel

logger = logging.getLogger(__name__)

CATEGORY_TO_MOMENT = {
    "tutorial": "tutorial",
    "debate": "controversial",
    "story": "storytelling",
    "emotional": "emotional",
    "funny": "funny",
}


def dominant_speaker(segments: list[dict], start_ms: int, end_ms: int) -> str | None:
    totals: dict[str, int] = {}
    for segment in segments:
        speaker = segment.get("speaker")
        if not speaker:
            continue
        overlap = max(0, min(end_ms, int(segment.get("end_ms") or 0)) - max(start_ms, int(segment.get("start_ms") or 0)))
        if overlap:
            totals[str(speaker)] = totals.get(str(speaker), 0) + overlap
    if not totals:
        return None
    return max(totals, key=lambda name: totals[name])


def moment_type_for(candidate: ScoredCandidate) -> str:
    matched = (candidate.brief or {}).get("matched_types") or []
    if matched:
        return str(matched[0])[:40]
    return CATEGORY_TO_MOMENT.get(candidate.category, "")


def candidate_record(
    candidate: ScoredCandidate,
    *,
    model: TranscriptModel,
    source: dict,
    visual_path: Path | None,
    scene_dir: Path,
    scene_fps: float,
    analysis_height: int,
    segments: list[dict] | None = None,
) -> dict:
    try:
        boundary = review_ending(
            candidate_start_ms=candidate.start_ms,
            candidate_end_ms=candidate.end_ms,
            sentences=model.sentences,
            source_path=visual_path,
            source_duration_ms=int(source["duration_ms"]),
            cache_dir=scene_dir,
            scene_fps=scene_fps,
            analysis_height=analysis_height,
            source_height=source.get("height"),
        )
    except Exception as exc:  # ending review must never break analysis
        logger.warning("Ending review failed for %s: %s", candidate.draft.key, exc)
        boundary = {
            "version": 1,
            "suspicious": False,
            "reviewed": "pending",
            "evidence": ["Ending review could not run."],
            "alternatives": [],
        }

    return {
        "id": new_id(),
        "start_ms": candidate.start_ms,
        "end_ms": candidate.end_ms,
        "title": candidate.title,
        "hook": candidate.hook,
        "hook_detail": candidate.hook_detail or {},
        "summary": candidate.summary or summarise(candidate.excerpt),
        "reason": candidate.reason,
        "category": candidate.category,
        "excerpt": candidate.excerpt,
        "scores": candidate.scores_payload(),
        "brief": candidate.brief,
        "overall": candidate.overall,
        "warnings": candidate.warnings,
        "method": candidate.method,
        "method_version": candidate.method_version,
        "boundary": boundary,
        "editorial": candidate.editorial,
        "duration": candidate.duration,
        "moment_type": moment_type_for(candidate),
        "speaker": dominant_speaker(segments or [], candidate.start_ms, candidate.end_ms),
    }
