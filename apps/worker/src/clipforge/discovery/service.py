"""Run one additional discovery pass over an existing analysis.

The pass is deliberately boring about reuse: it loads the stored transcript, the
stored source and the project's brief, and it never calls the transcription or
download providers. Everything expensive that already exists is used as-is.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from pydantic import ValidationError

from clipforge.briefs.spec import ClippingBrief
from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.discovery.criteria import (
    DiscoveryCriteria,
    build_discovery_brief,
    normalise_criteria,
)
from clipforge.discovery.coverage import build_coverage, coverage_summary
from clipforge.discovery.dedupe import select_additional
from clipforge.errors import JobFailed, ValidationFailed
from clipforge.media.assets import visual_input
from clipforge.pipeline.briefing import (
    apply_brief,
    apply_corrections,
    attach_editorial,
    attach_hooks,
    resolve_brief,
)
from clipforge.pipeline.records import candidate_record, dominant_speaker, moment_type_for
from clipforge.ranking.generate import generate_candidates
from clipforge.ranking.score import score_candidates
from clipforge.ranking.sentences import build_transcript_model
from clipforge.storage.base import StorageBackend, StorageObjectMissing, ensure_local
from clipforge.storage.workspace import ensure_layout, write_json_atomic

logger = logging.getLogger(__name__)

StageCallback = Callable[[str, float, str], None]

GOAL_BY_TYPE = {
    "funny": "funny",
    "joke": "funny",
    "reaction": "funny",
    "emotional": "emotional_stories",
    "storytelling": "emotional_stories",
    "tutorial": "tutorials",
    "practical_tip": "tutorials",
    "controversial": "debate",
}


def _check(should_cancel: Callable[[], bool]) -> None:
    if should_cancel():
        raise JobFailed("cancelled", "Cancelled")


def _base_brief(repos: Repositories, project: dict, project_id: str) -> tuple[ClippingBrief, str]:
    saved = project.get("brief") or {}
    if saved:
        try:
            return ClippingBrief.model_validate(saved), "project"
        except ValueError:
            logger.warning("Saved brief for %s is not valid; falling back", project_id)
    initial = repos.runs.latest_initial(project_id)
    run_brief = (initial or {}).get("settings", {}).get("brief") or {}
    if run_brief:
        try:
            return ClippingBrief.model_validate(run_brief), "initial_run"
        except ValueError:
            logger.warning("Initial run brief for %s is not valid; falling back", project_id)
    brief, source = resolve_brief(repos, project, {})
    return brief, source


def _goal_for(types: list[str], fallback: str) -> str:
    for moment_type in types:
        goal = GOAL_BY_TYPE.get(moment_type)
        if goal:
            return goal
    return fallback


def _empty_result(
    repos: Repositories,
    *,
    project_id: str,
    criteria: DiscoveryCriteria,
    criteria_key: str,
    brief: ClippingBrief,
    goal: str,
    requested: int,
    reason: str,
    warnings: list[str],
    existing: list[dict],
    duration_ms: int,
) -> dict:
    coverage = coverage_summary(build_coverage(duration_ms, existing))
    discovery = {
        "kind": "additional",
        "criteria": criteria.model_dump(),
        "criteria_key": criteria_key,
        "requested": requested,
        "found": 0,
        "reason": reason,
        "exhausted": True,
        "duplicate_of_existing": 0,
        "suppressed_duplicates": 0,
        "coverage": coverage,
        "warnings": warnings[-12:],
        "goal": goal,
    }
    run = repos.runs.create(
        project_id,
        {
            "goal": goal,
            "clip_count": criteria.count,
            "brief": brief.model_dump(),
            "brief_source": "discovery",
            "criteria": criteria.model_dump(),
            "criteria_key": criteria_key,
        },
        kind="additional",
        discovery=discovery,
    )
    repos.runs.finish(run["id"], status="completed", provider="baseline", warnings=warnings)
    repos.projects.update(project_id, status="analysed")
    return {
        "analysis_run_id": run["id"],
        "requested": requested,
        "found": 0,
        "total_candidates": len(existing),
        "reason": reason,
        "exhausted": True,
        "criteria": criteria.model_dump(),
        "criteria_key": criteria_key,
        "coverage": coverage,
        "warnings": warnings,
    }


def run_discovery(
    repos: Repositories,
    settings: Settings,
    storage: StorageBackend,
    *,
    project_id: str,
    request: dict,
    on_stage: StageCallback,
    should_cancel: Callable[[], bool],
    cancel_event: threading.Event,
) -> dict:
    project = repos.projects.get(project_id)
    source = repos.sources.get(project_id)
    if not source.get("has_audio"):
        raise JobFailed("no_audio_stream", "This source has no speech to discover moments from.")

    transcript = repos.transcripts.latest(project_id)
    if transcript is None:
        raise JobFailed(
            "transcript_missing",
            "This project has no saved transcript yet. Run the first analysis, then ask for more moments.",
        )
    existing = repos.candidates.list_for_project(project_id)
    if not existing:
        raise JobFailed(
            "analysis_required",
            "Generate the first set of moments before asking for more.",
        )

    duration_ms = int(source["duration_ms"])
    speakers = sorted(
        {
            str(segment.get("speaker"))
            for segment in transcript.get("segments") or []
            if segment.get("speaker")
        }
    )

    raw = request.get("criteria") if isinstance(request.get("criteria"), dict) else request
    try:
        criteria = DiscoveryCriteria.model_validate(raw or {})
        criteria, criteria_warnings = normalise_criteria(
            criteria,
            source_duration_ms=duration_ms,
            max_count=settings.max_moments_per_request,
            speakers_available=bool(speakers),
        )
    except ValidationError as exc:
        raise JobFailed("invalid_criteria", "The discovery request is not valid.") from exc
    except ValidationFailed as exc:
        raise JobFailed(exc.code, exc.message) from exc
    criteria_key = criteria.key()

    warnings: list[str] = list(criteria_warnings)
    base_brief, brief_source = _base_brief(repos, project, project_id)
    try:
        brief = build_discovery_brief(base_brief, criteria)
    except ValidationFailed as exc:
        raise JobFailed(exc.code, exc.message) from exc
    initial = repos.runs.latest_initial(project_id)
    stored_goal = str((initial or {}).get("settings", {}).get("goal") or project.get("goals", {}).get("goal") or "best_moments")
    goal = _goal_for(criteria.moment_types, stored_goal)

    on_stage("prepare", 4, "Loading the existing transcript and source")
    _check(should_cancel)
    try:
        source_path = ensure_local(
            storage,
            source.get("relative_path"),
            legacy_path=source.get("storage_path"),
            missing_message="The source video is missing from this project's workspace.",
        )
    except StorageObjectMissing as exc:
        raise JobFailed("source_missing", exc.message) from exc

    paths = ensure_layout(settings, project_id)
    visual_path, analysis_height = visual_input(repos, storage, source, source_path)
    if not source.get("has_video"):
        visual_path = source_path
        warnings.append(
            "This project has no video, so scene cuts and visual ending review are unavailable for new moments."
        )
    processing = (source.get("download") or {}).get("processing") or {}
    scene_fps = processing.get("scene_fps")

    segments = transcript.get("segments") or []
    corrections = repos.corrections.list(project_id)
    if corrections:
        segments = apply_corrections(segments, corrections)
        warnings.append(f"Applied {len(corrections)} manual transcript correction(s).")

    on_stage("discover", 18, "Finding moments in sections that are not covered yet")
    _check(should_cancel)
    model = build_transcript_model(segments, duration_ms, transcript.get("language"))
    if not model.sentences:
        raise JobFailed("no_speech_detected", "The transcript has no usable sentences to build moments from.")

    if criteria.min_duration_ms is not None or criteria.max_duration_ms is not None:
        duration_range = brief.duration_range()
        bands = ((duration_range.min_ms, duration_range.max_ms, "custom"),)
    else:
        bands = brief.generation_bands()
    drafts = generate_candidates(model, bands=bands)

    range_start = criteria.source_start_ms or 0
    range_end = criteria.source_end_ms or duration_ms
    drafts = [
        draft
        for draft in drafts
        if draft.start_ms >= range_start and draft.end_ms <= range_end
    ]
    if criteria.min_duration_ms is not None or criteria.max_duration_ms is not None:
        minimum = criteria.min_duration_ms or 0
        maximum = criteria.max_duration_ms or duration_ms
        drafts = [draft for draft in drafts if minimum <= draft.duration_ms <= maximum]

    if not drafts:
        reason = "no_moments_in_range" if (criteria.source_start_ms or criteria.source_end_ms) else "no_candidates"
        warnings.append(
            "No complete moment could be built inside that source range. "
            "Widen the range or choose a different length."
        )
        return _empty_result(
            repos,
            project_id=project_id,
            criteria=criteria,
            criteria_key=criteria_key,
            brief=brief,
            goal=goal,
            requested=criteria.count,
            reason=reason,
            warnings=warnings,
            existing=existing,
            duration_ms=duration_ms,
        )

    on_stage("rank", 42, "Ranking new moments against the project brief")
    _check(should_cancel)
    ranked = score_candidates(
        drafts,
        model,
        goal=goal,
        duration_range=brief.duration_range(),
    )
    ranked, blocked = apply_brief(ranked, brief)
    if blocked:
        warnings.append(f"{blocked} candidate(s) were excluded by the brief's exclusions or safety rules.")

    if not ranked:
        warnings.append(
            "No candidate survived the project's exclusions. "
            "Check the brief's blocked topics and safety rules."
        )
        return _empty_result(
            repos,
            project_id=project_id,
            criteria=criteria,
            criteria_key=criteria_key,
            brief=brief,
            goal=goal,
            requested=criteria.count,
            reason="no_candidates",
            warnings=warnings,
            existing=existing,
            duration_ms=duration_ms,
        )

    if criteria.moment_types:
        wanted = set(criteria.moment_types)
        ranked = [
            candidate
            for candidate in ranked
            if wanted & set((candidate.brief or {}).get("matched_types") or [])
        ]
        if not ranked:
            warnings.append(
                "No moment in this source matched the requested type(s). "
                "Try a different type, widen the range, or add a custom request."
            )
            return _empty_result(
                repos,
                project_id=project_id,
                criteria=criteria,
                criteria_key=criteria_key,
                brief=brief,
                goal=goal,
                requested=criteria.count,
                reason="no_matching_moments",
                warnings=warnings,
                existing=existing,
                duration_ms=duration_ms,
            )

    if criteria.include_speakers:
        wanted_speakers = {value.lower() for value in criteria.include_speakers}
        ranked = [
            candidate
            for candidate in ranked
            if (dominant_speaker(segments, candidate.start_ms, candidate.end_ms) or "").lower()
            in wanted_speakers
        ]
        if not ranked:
            warnings.append(
                "No moment matched the requested speaker(s). "
                "Speaker labels cover only part of this transcript."
            )
            return _empty_result(
                repos,
                project_id=project_id,
                criteria=criteria,
                criteria_key=criteria_key,
                brief=brief,
                goal=goal,
                requested=criteria.count,
                reason="no_matching_speaker",
                warnings=warnings,
                existing=existing,
                duration_ms=duration_ms,
            )

    eligible = [candidate for candidate in ranked if candidate.overall >= brief.min_quality]
    if not eligible:
        if criteria.moment_types:
            warnings.append(
                f"No {', '.join(criteria.moment_types)} moment reached the quality threshold of "
                f"{brief.min_quality:.0f}/100."
            )
            return _empty_result(
                repos,
                project_id=project_id,
                criteria=criteria,
                criteria_key=criteria_key,
                brief=brief,
                goal=goal,
                requested=criteria.count,
                reason="below_quality_threshold",
                warnings=warnings,
                existing=existing,
                duration_ms=duration_ms,
            )
        eligible = ranked[: min(3, len(ranked))]
        warnings.append(
            f"No new moment reached the brief's minimum quality of {brief.min_quality:.0f}/100. "
            "The closest matches are shown; review them before exporting."
        )
    elif len(eligible) < len(ranked):
        warnings.append(
            f"{len(ranked) - len(eligible)} candidate(s) scored below the brief's minimum quality threshold."
        )

    on_stage("dedupe", 62, "Removing moments that already exist in this project")
    _check(should_cancel)
    selection = select_additional(
        eligible,
        existing,
        limit=criteria.count,
        model=model,
        duration_ms=duration_ms,
        diversity=criteria.diversity,
        revisit_underexplored=criteria.revisit_underexplored,
        exclude_existing=criteria.exclude_existing,
    )
    if not selection.kept:
        warnings.append(
            "Every new moment overlapped one you already have. "
            "The source may be exhausted for this type and range."
        )
        return _empty_result(
            repos,
            project_id=project_id,
            criteria=criteria,
            criteria_key=criteria_key,
            brief=brief,
            goal=goal,
            requested=criteria.count,
            reason="duplicates_only",
            warnings=warnings,
            existing=existing,
            duration_ms=duration_ms,
        )

    warnings.extend(attach_hooks(selection.kept, model, brief))
    warnings.extend(attach_editorial(selection.kept, model, brief))

    fitting = [candidate for candidate in selection.kept if (candidate.duration or {}).get("status") == "fit"]
    if not fitting:
        duration_range = brief.duration_range()
        closest = sorted(
            selection.kept,
            key=lambda candidate: abs(
                candidate.duration_ms - (duration_range.target_ms or (duration_range.min_ms + duration_range.max_ms) // 2)
            ),
        )[:3]
        alternatives = "; ".join(
            f"{candidate.title} ({candidate.duration_ms / 1000:.0f}s, {(candidate.duration or {}).get('status', 'unknown')})"
            for candidate in closest
        )
        warnings.append(
            f"No new complete moment fits the {duration_range.label} target. "
            f"Closest coherent alternatives: {alternatives}. Nothing was padded or truncated."
        )

    on_stage("boundaries", 82, "Reviewing new clip endings")
    _check(should_cancel)
    records = [
        candidate_record(
            candidate,
            model=model,
            source=source,
            visual_path=visual_path if source.get("has_video") else None,
            scene_dir=paths / "analysis" / "scenes",
            scene_fps=scene_fps,
            analysis_height=analysis_height,
            segments=segments,
        )
        for candidate in selection.kept
    ]

    suspicious = [record for record in records if (record.get("boundary") or {}).get("suspicious")]
    if suspicious:
        warnings.append(
            f"{len(suspicious)} new ending(s) may include a trailing or unrelated shot. "
            "Check the ending review before exporting."
        )

    coverage = coverage_summary(build_coverage(duration_ms, existing, records))
    discovery = {
        "kind": "additional",
        "criteria": criteria.model_dump(),
        "criteria_key": criteria_key,
        "requested": criteria.count,
        "found": len(records),
        "reason": None,
        "exhausted": len(records) < criteria.count,
        "duplicate_of_existing": selection.duplicate_of_existing,
        "suppressed_duplicates": len(selection.suppressed),
        "spread_limited": selection.spread_limited,
        "coverage": coverage,
        "warnings": warnings[-12:],
        "goal": goal,
        "brief_source": brief_source,
        "moment_types": sorted({moment_type_for(candidate) for candidate in selection.kept} - {""}),
    }

    run = repos.runs.create(
        project_id,
        {
            "goal": goal,
            "clip_count": criteria.count,
            "brief": brief.model_dump(),
            "brief_source": "discovery",
            "criteria": criteria.model_dump(),
            "criteria_key": criteria_key,
        },
        kind="additional",
        discovery=discovery,
    )
    run_id = run["id"]
    repos.candidates.replace_for_run(project_id, run_id, records)
    repos.runs.finish(run_id, status="completed", provider="baseline", warnings=warnings)
    repos.projects.update(project_id, status="analysed")
    write_json_atomic(
        paths / "analysis" / f"discovery-{run_id}.json",
        {
            "run_id": run_id,
            "criteria": criteria.model_dump(),
            "brief": brief.model_dump(),
            "candidates": records,
            "coverage": coverage,
            "warnings": warnings,
        },
    )

    on_stage("done", 100, "Additional moments ready")
    return {
        "analysis_run_id": run_id,
        "requested": criteria.count,
        "found": len(records),
        "total_candidates": len(existing) + len(records),
        "exhausted": len(records) < criteria.count,
        "criteria": criteria.model_dump(),
        "criteria_key": criteria_key,
        "coverage": coverage,
        "duplicate_of_existing": selection.duplicate_of_existing,
        "suppressed_duplicates": len(selection.suppressed),
        "warnings": warnings,
    }
