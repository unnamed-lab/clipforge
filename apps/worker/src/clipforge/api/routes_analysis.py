from __future__ import annotations

from fastapi import APIRouter, Request

from clipforge.api.deps import get_state
from clipforge.api.models import (
    AnalysisRequest,
    BoundaryAction,
    CandidatePatch,
    EditorialAction,
    SceneInspectRequest,
)
from clipforge.briefs.spec import ClippingBrief
from clipforge.durations import duration_record
from clipforge.editorial.narrative import build_retention_plan
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.hooks.select import build_hook_candidates
from clipforge.media.visual import TEXT_READABLE_HEIGHT, analyse_source_window
from clipforge.ranking.sentences import build_transcript_model, tokenize
from clipforge.security.limits import MIN_CLIP_MS, validate_span
from clipforge.storage.base import StorageObjectMissing, ensure_local
from clipforge.storage.workspace import ensure_layout

router = APIRouter(prefix="/api", tags=["analysis"])


@router.post("/projects/{project_id}/analysis")
def start_analysis(project_id: str, payload: AnalysisRequest, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise ValidationFailed("Import a video before analysing this project", code="source_missing")
    if not source["has_audio"]:
        raise ValidationFailed(
            "This video has no audio track, so there is no speech to analyse.",
            code="no_audio_stream",
        )

    active = state.repos.jobs.active_for(project_id, "analysis")
    if active:
        return {"job": active, "deduplicated": True}

    payload_dict = payload.model_dump()
    brief = payload.brief or {}
    editorial = brief.get("editorial") or {}
    job = state.queue.enqueue(project_id=project_id, job_type="analysis", payload=payload_dict)
    state.repos.projects.update(
        project_id,
        status="analysing",
        goals={
            "goal": payload.goal,
            "clip_count": payload.clip_count,
            "duration_band": payload.duration_band,
            "duration_preset": brief.get("duration_preset") or "extended",
            "duration_min_ms": brief.get("min_duration_ms"),
            "duration_max_ms": brief.get("max_duration_ms"),
            "editorial_style": editorial.get("style") or "automatic",
            "editorial_strategy": editorial.get("preferred_strategy") or "automatic",
            "genre": editorial.get("genre") or "auto",
            "pacing": (brief.get("pacing") or {}).get("profile") or "balanced",
            "flashback": editorial.get("flashback") or "automatic",
            "hook_preference": editorial.get("hook_preference") or "auto",
            "pattern_interrupts": editorial.get("pattern_interrupt_intensity") or "balanced",
            "reaction_shots": editorial.get("reaction_shots") or "automatic",
            "loop_ending": editorial.get("loop_ending") or "automatic",
        },
    )
    return {"job": job, "deduplicated": False}


@router.post("/projects/{project_id}/scene-inspect")
def inspect_scene(project_id: str, payload: SceneInspectRequest, request: Request) -> dict:
    """Re-sample one scene window at the best resolution the source can supply.

    The pipeline analyses cheap, low-resolution frames on purpose, and says so
    in its evidence. This endpoint is the user's way to ask for a second look at
    a single moment without re-running the whole analysis. It reads the retained
    source, never the analysis proxy, and it never upscales: if the source is
    480p, that is the best evidence there is and the response says so.
    """
    state = get_state(request)
    state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise ValidationFailed("Import a video before inspecting a scene", code="source_missing")
    if not source.get("has_video"):
        raise ValidationFailed(
            "This source was downloaded without video, so there are no frames to inspect.",
            code="no_video_stream",
        )

    duration_ms = int(source["duration_ms"])
    requested_start = max(0, int(payload.start_ms))
    if int(payload.end_ms) - requested_start < 500:
        # Widening silently would inspect a different moment than the one asked for.
        raise ValidationFailed(
            "Choose a window of at least half a second to inspect", code="invalid_span"
        )
    start_ms = min(requested_start, max(0, duration_ms - 500))
    end_ms = min(duration_ms, int(payload.end_ms))
    if end_ms - start_ms < 500:
        raise ValidationFailed(
            "Choose a window of at least half a second to inspect", code="invalid_span"
        )

    try:
        path = ensure_local(
            state.storage,
            source.get("relative_path"),
            legacy_path=source.get("storage_path"),
            missing_message="The source file is missing from this project",
        )
    except StorageObjectMissing as exc:
        raise NotFound(exc.message) from exc

    source_height = int(source.get("height") or 0) or None
    wanted = int(payload.height or TEXT_READABLE_HEIGHT)
    height = min(wanted, source_height) if source_height else wanted
    height = max(144, height)

    paths = ensure_layout(state.settings, project_id)
    result = analyse_source_window(
        path,
        start_ms,
        end_ms,
        cache_dir=paths / "analysis" / "scenes",
        source_duration_ms=duration_ms,
        scene_fps=1.0,
        analysis_height=height,
        source_height=source_height,
    )
    verified = bool(result.analysis.get("text_verified"))
    sampled = result.analysis.get("sampled_height") or height
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "analysis": result.analysis,
        "scenes": [scene.to_dict() for scene in result.scenes],
        "warnings": result.warnings,
        "text_verified": verified,
        "note": (
            f"Inspected at {sampled}p. On-screen text is readable at this resolution."
            if verified
            else f"Inspected at {sampled}p. The source cannot supply a frame where small text is readable."
        ),
    }


@router.get("/projects/{project_id}/candidates")
def list_candidates(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    run = state.repos.runs.latest(project_id)
    candidates = state.repos.candidates.list_for_project(project_id)
    return {"candidates": candidates, "analysis_run": run}


@router.get("/projects/{project_id}/candidates/{candidate_id}")
def get_candidate(project_id: str, candidate_id: str, request: Request) -> dict:
    state = get_state(request)
    return {"candidate": state.repos.candidates.get(project_id, candidate_id)}


@router.get("/projects/{project_id}/candidates/{candidate_id}/hooks")
def list_candidate_hooks(project_id: str, candidate_id: str, request: Request) -> dict:
    """Rank the grounded hook options for one candidate from its own transcript.

    Options are rebuilt with the brief that produced the candidate, so the list
    matches what analysis would have offered. Nothing is invented: every entry is
    an exact transcript span with its real wording.
    """
    state = get_state(request)
    candidate = state.repos.candidates.get(project_id, candidate_id)
    transcript = state.repos.transcripts.latest(project_id)
    if transcript is None:
        return {
            "hooks": [],
            "selected": candidate.get("hook_detail") or None,
            "policy": None,
            "reason": "no_transcript",
            "note": "Analyse this project before choosing a hook.",
        }

    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise NotFound("This project has no source video")
    run = state.repos.runs.latest(project_id)
    brief_data = ((run or {}).get("settings") or {}).get("brief") or {}
    try:
        brief = ClippingBrief.model_validate(brief_data) if brief_data else ClippingBrief()
    except ValueError as exc:
        raise ValidationFailed("The stored brief is not valid", code="invalid_brief") from exc

    model = build_transcript_model(
        transcript["segments"], int(source["duration_ms"]), transcript.get("language")
    )
    options = build_hook_candidates(
        model,
        start_ms=int(candidate["start_ms"]),
        end_ms=int(candidate["end_ms"]),
        policy=brief.hook,
        main_text=candidate.get("excerpt") or "",
        max_words=max(6, min(16, brief.hook.max_ms // 220)),
        preference=brief.editorial.hook_preference,
    )
    selected = candidate.get("hook_detail") or None
    reason = None
    if not brief.hook.enabled:
        reason = "disabled"
    elif not brief.hook.auto_select:
        reason = "auto_select_off"
    elif not options:
        reason = "no_candidates"
    return {
        "hooks": [option.to_dict() for option in options],
        "selected": selected,
        "policy": {
            "enabled": brief.hook.enabled,
            "auto_select": brief.hook.auto_select,
            "min_ms": brief.hook.min_ms,
            "max_ms": brief.hook.max_ms,
            "allow_external": brief.hook.allow_external,
            "strict_chronological": brief.hook.strict_chronological,
            "preference": brief.editorial.hook_preference,
            "min_score": brief.hook.min_score,
        },
        "reason": reason,
        "note": (
            "Choose a transcript line to open the clip. Earlier lines add context; "
            "later lines become a cold open and move the payoff."
        ),
    }


def _assert_hook_is_grounded(
    state, project_id: str, source_duration_ms: int, text: str, start_ms: int, end_ms: int
) -> None:
    """Reject hook text that is not a verbatim transcript span.

    The UI only offers transcript windows, but the API is the trust boundary: a
    hook must never become invented dialogue through a crafted request.
    """
    transcript = state.repos.transcripts.latest(project_id)
    if transcript is None:
        return
    model = build_transcript_model(
        transcript["segments"], source_duration_ms, transcript.get("language")
    )
    words = [word for sentence in model.sentences for word in sentence.words]
    if not words:
        # Legacy transcripts without word timings cannot be checked; analysis
        # remains the only producer of hooks for these projects.
        return
    window: list[str] = []
    for word in words:
        if word.end_ms >= start_ms - 100 and word.start_ms <= end_ms + 100:
            window.extend(tokenize(word.clean))
    requested = tokenize(text)
    if not requested or " " + " ".join(requested) + " " not in " " + " ".join(window) + " ":
        raise ValidationFailed(
            "This hook is not a verbatim line from the transcript; choose a real source line.",
            code="hook_not_grounded",
        )


@router.patch("/projects/{project_id}/candidates/{candidate_id}")
def patch_candidate(project_id: str, candidate_id: str, payload: CandidatePatch, request: Request) -> dict:
    state = get_state(request)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise NotFound("This project has no source video")
    current = state.repos.candidates.get(project_id, candidate_id)

    if payload.reset_edits:
        updated = state.repos.candidates.reset_edits(project_id, candidate_id)
        return {"candidate": updated}

    if payload.clear_hook:
        current = state.repos.candidates.clear_hook(project_id, candidate_id)

    start = payload.start_ms if payload.start_ms is not None else current["start_ms"]
    end = payload.end_ms if payload.end_ms is not None else current["end_ms"]
    if payload.start_ms is not None or payload.end_ms is not None:
        try:
            start, end = validate_span(
                int(start),
                int(end),
                int(source["duration_ms"]),
                min_ms=MIN_CLIP_MS,
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc), code="invalid_span") from exc

    hook_payload = None
    if payload.hook is not None:
        existing = dict(current.get("hook_detail") or {})
        changes = payload.hook.model_dump(exclude_none=True)
        merged = {**existing, **changes}
        hook_start = merged.get("start_ms")
        hook_end = merged.get("end_ms")
        text = (merged.get("text") or "").strip()
        if hook_start is None or hook_end is None:
            raise ValidationFailed("A hook needs a start and an end time", code="invalid_hook")
        try:
            hook_start, hook_end = validate_span(
                int(hook_start), int(hook_end), int(source["duration_ms"]), min_ms=500, max_ms=15_000
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc), code="invalid_hook") from exc
        if not text:
            raise ValidationFailed(
                "A hook needs its real transcript text; Clipforge does not invent dialogue.",
                code="hook_text_missing",
            )
        _assert_hook_is_grounded(
            state, project_id, int(source["duration_ms"]), text, hook_start, hook_end
        )
        hook_payload = {
            **merged,
            "text": text[:300],
            "start_ms": hook_start,
            "end_ms": hook_end,
            "duration_ms": hook_end - hook_start,
            "source": merged.get("source") or "external",
            "placement": merged.get("placement") or "before",
            "approved": merged.get("approved", True),
            "edited": True,
            "estimated": True,
        }

    updated = state.repos.candidates.update(
        project_id,
        candidate_id,
        start_ms=start if payload.start_ms is not None else None,
        end_ms=end if payload.end_ms is not None else None,
        decision=payload.decision,
        title=payload.title,
        summary=payload.summary,
        hook=hook_payload,
    )
    state.repos.projects.touch(project_id)
    return {"candidate": state.repos.candidates.get(project_id, candidate_id)}


@router.post("/projects/{project_id}/candidates/{candidate_id}/boundary")
def review_boundary(project_id: str, candidate_id: str, payload: BoundaryAction, request: Request) -> dict:
    state = get_state(request)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise NotFound("This project has no source video")
    candidate = state.repos.candidates.get(project_id, candidate_id)

    if payload.action == "accept":
        if payload.end_ms is None:
            raise ValidationFailed("Choose an ending before accepting it", code="boundary_end_missing")
        try:
            start, end = validate_span(
                int(candidate["start_ms"]),
                int(payload.end_ms),
                int(source["duration_ms"]),
                min_ms=MIN_CLIP_MS,
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc), code="invalid_span") from exc
        state.repos.candidates.update(
            project_id, candidate_id, start_ms=None, end_ms=end, decision=None, title=None, summary=None
        )
        updated = state.repos.candidates.set_boundary(
            project_id, candidate_id, {"reviewed": "accepted", "accepted_end_ms": end}
        )
    elif payload.action == "reject":
        updated = state.repos.candidates.set_boundary(
            project_id, candidate_id, {"reviewed": "rejected", "accepted_end_ms": None}
        )
    else:
        state.repos.candidates.reset_edits(project_id, candidate_id)
        updated = state.repos.candidates.set_boundary(
            project_id, candidate_id, {"reviewed": "pending", "accepted_end_ms": None}
        )
    state.repos.projects.touch(project_id)
    return {"candidate": updated}


@router.post("/projects/{project_id}/candidates/{candidate_id}/editorial")
def re_evaluate_editorial(
    project_id: str, candidate_id: str, payload: EditorialAction, request: Request
) -> dict:
    """Re-plan this candidate without re-transcribing or touching manual edits.

    This is a candidate-level re-evaluation, not a new analysis run. Captions,
    branding, composition overrides and the user's own trim are preserved.
    """
    state = get_state(request)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise NotFound("This project has no source video")
    candidate = state.repos.candidates.get(project_id, candidate_id)
    active = state.repos.jobs.active_for(project_id, "analysis")
    if active:
        # The analysis job rewrites candidates and their editorial plans. Mutating
        # one underneath it would be overwritten when the run finishes.
        raise Conflict(
            "Analysis is still running for this project; change the strategy when it finishes.",
        )
    transcript = state.repos.transcripts.latest(project_id)
    if transcript is None:
        raise ValidationFailed("Analyse this project before changing its editorial strategy", code="no_transcript")
    run = state.repos.runs.latest(project_id)
    brief_data = ((run or {}).get("settings") or {}).get("brief") or {}
    try:
        brief = ClippingBrief.model_validate(brief_data) if brief_data else ClippingBrief()
    except ValueError as exc:
        raise ValidationFailed("The stored brief is not valid", code="invalid_brief") from exc

    editorial = brief.editorial.model_dump()
    if payload.strategy == "chronological":
        editorial["style"] = "chronological"
        editorial["preferred_strategy"] = "chronological"
    elif payload.strategy == "manual":
        editorial["style"] = "manual"
        editorial["preferred_strategy"] = "manual"
    elif payload.strategy != "automatic":
        editorial["style"] = "automatic"
        editorial["preferred_strategy"] = payload.strategy
    pacing = brief.pacing.model_dump()
    if payload.pacing_profile:
        pacing["profile"] = payload.pacing_profile

    model = build_transcript_model(
        transcript["segments"], int(source["duration_ms"]), transcript.get("language")
    )
    hook = candidate.get("hook_detail") or {}
    plan = build_retention_plan(
        model,
        start_ms=int(candidate["start_ms"]),
        end_ms=int(candidate["end_ms"]),
        excerpt=candidate.get("excerpt") or "",
        pacing=pacing,
        hook_policy=brief.hook,
        hook=hook or None,
        hook_candidates=[hook] if hook else None,
        editorial=editorial,
        duration_preset=brief.duration_preset,
    )
    stored = plan.model_dump()
    if not payload.apply and stored.get("editorial"):
        stored["editorial"]["applied"] = False
    duration = dict(candidate.get("duration") or {})
    timeline = (stored.get("timeline") or {})
    actual = int(timeline.get("total_output_ms") or candidate.get("duration_ms") or 0)
    record = duration_record(brief.duration_range(), actual)
    generated = duration.get("generated_ms")
    duration.update(record)
    if generated is not None:
        duration["generated_ms"] = generated
    updated = state.repos.candidates.set_editorial(
        project_id, candidate_id, stored, duration=duration
    )
    state.repos.projects.touch(project_id)
    return {"candidate": updated}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    state = get_state(request)
    return {"job": state.repos.jobs.get(job_id)}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict:
    state = get_state(request)
    job = state.queue.cancel(job_id)
    return {"job": job}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request) -> dict:
    state = get_state(request)
    job = state.repos.jobs.get(job_id)
    if job["state"] in {"queued", "running"}:
        raise Conflict("That job is still active")
    requeued = state.queue.retry(job_id)
    if job["type"] == "render" and job.get("payload", {}).get("render_id"):
        state.repos.renders.mark_running(job["payload"]["render_id"])
    return {"job": requeued}


@router.get("/projects/{project_id}/jobs")
def list_jobs(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    return {"jobs": state.repos.jobs.list_for_project(project_id, limit=25)}
