"""Durable project-level production orchestrator.

One job runs the stages in order. Every stage and every clip has a persisted
checkpoint, so a restart resumes after the last completed stage and never
re-runs transcription because a caption preset changed. The renderer, export
gate and editorial planner are the existing ones; this module only sequences
them and records what actually happened.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.discovery.service import run_discovery
from clipforge.editorial.director import (
    DIRECTOR_VERSION,
    director_document,
    engine_descriptor,
    run_director,
    strategy_label,
)
from clipforge.errors import AppError, JobFailed
from clipforge.jobs.queue import JobContext
from clipforge.media.process import ProcessCancelled
from clipforge.pipeline.analysis import run_analysis
from clipforge.pipeline.delivery import save_render
from clipforge.pipeline.exports import (
    PIPELINE_VERSION,
    build_manifest,
    checksum_for,
    export_filename,
    slugify,
    source_identity,
    spec_fingerprint,
)
from clipforge.profiles.spec import profile_snapshot, resolve_settings
from clipforge.providers.llm.registry import LlmProviderRegistry
from clipforge.quality.review import (
    apply_repair,
    build_review_report,
    choose_repair,
    measure_audio,
)
from clipforge.rendering import intake
from clipforge.rendering.service import perform_render
from clipforge.rendering.spec import (
    DEFAULT_CAPTION_PRESET,
    RenderSpec,
    build_spec,
    normalise_target,
)
from clipforge.storage.base import StorageBackend, StorageObjectMissing, ensure_local
from clipforge.storage.workspace import ensure_layout, ensure_space, write_json_atomic

logger = logging.getLogger(__name__)

STAGES = [
    "SOURCE_VALIDATION",
    "SOURCE_ANALYSIS",
    "MOMENT_DISCOVERY",
    "EDITORIAL_PLANNING",
    "PLAN_VALIDATION",
    "CONFIGURATION",
    "RENDERING",
    "RENDER_VALIDATION",
    "EDITORIAL_REVIEW",
    "REPAIR_PLANNING",
    "RENDER_REPAIR",
    "FINAL_QA",
    "PACKAGE_DELIVERABLES",
    "COMPLETED",
]

STAGE_LABELS = {
    "SOURCE_VALIDATION": "Checking the source",
    "SOURCE_ANALYSIS": "Transcribing and finding moments",
    "MOMENT_DISCOVERY": "Collecting candidate moments",
    "EDITORIAL_PLANNING": "Choosing clips and strategies",
    "PLAN_VALIDATION": "Validating the edit plan",
    "CONFIGURATION": "Configuring composition and captions",
    "RENDERING": "Rendering clips",
    "RENDER_VALIDATION": "Checking rendered files",
    "EDITORIAL_REVIEW": "Reviewing the rendered edit",
    "REPAIR_PLANNING": "Planning bounded repairs",
    "RENDER_REPAIR": "Repairing and re-rendering",
    "FINAL_QA": "Final validation",
    "PACKAGE_DELIVERABLES": "Organising exports",
    "COMPLETED": "Complete",
}

TERMINAL_CLIP_STATES = {"passed", "passed_with_warnings", "needs_review", "failed"}
_STAGE_STARTED: dict[tuple[str, str], float] = {}


def _parse_deadline(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).timestamp()


def _check_cancel(ctx: JobContext, run_id: str) -> None:
    if ctx.should_cancel() or ctx.repos.project_runs.is_cancel_requested(run_id):
        raise JobFailed("cancelled", "Cancelled")


def _progress(ctx: JobContext, run_id: str, stage: str, fraction: float, message: str) -> None:
    started = _STAGE_STARTED.get((run_id, stage))
    if started is not None:
        elapsed = time.monotonic() - started
        if elapsed > ctx.settings.pipeline_stage_timeout_seconds:
            raise JobFailed(
                "stage_timeout",
                f"The {STAGE_LABELS.get(stage, stage)} stage exceeded its configured "
                f"{ctx.settings.pipeline_stage_timeout_seconds}-second limit.",
            )
    index = STAGES.index(stage)
    total = len(STAGES) - 1
    overall = min(99.0, ((index + max(0.0, min(1.0, fraction))) / total) * 100)
    ctx.repos.project_runs.progress(run_id, progress=overall, message=message, stage=stage)
    ctx.repos.project_stages.progress(
        run_id, stage, progress=max(0.0, min(100.0, fraction * 100)), message=message
    )
    ctx.repos.project_runs.heartbeat(run_id)
    ctx.on_stage(stage.lower(), overall, message)


def _stage_valid(repos: Repositories, project_id: str, run_id: str, name: str,
                 output: dict) -> bool:
    """Cheap proof that a completed checkpoint still describes reality."""
    try:
        if name == "SOURCE_VALIDATION":
            return repos.sources.find_optional(project_id) is not None
        if name == "SOURCE_ANALYSIS":
            return (
                repos.transcripts.latest(project_id) is not None
                and repos.candidates.count_for_project(project_id) > 0
            )
        if name == "MOMENT_DISCOVERY":
            return repos.candidates.count_for_project(project_id) > 0
        if name == "EDITORIAL_PLANNING":
            return bool(output.get("selected")) and len(repos.project_clips.list(run_id)) > 0
        if name == "PLAN_VALIDATION":
            clips = repos.project_clips.list(run_id)
            return any(clip["status"] not in {"failed"} for clip in clips)
        if name == "CONFIGURATION":
            clips = repos.project_clips.list(run_id)
            return any((clip.get("plan") or {}).get("spec") for clip in clips)
        if name == "RENDERING":
            clips = repos.project_clips.list(run_id)
            return all(clip.get("render_id") for clip in clips if clip["status"] not in {"failed"})
        if name in {"RENDER_VALIDATION", "EDITORIAL_REVIEW", "REPAIR_PLANNING", "RENDER_REPAIR"}:
            clips = repos.project_clips.list(run_id)
            return bool(clips) and all(clip["status"] in TERMINAL_CLIP_STATES for clip in clips)
        if name == "FINAL_QA":
            clips = repos.project_clips.list(run_id)
            return bool(clips) and all(clip["status"] in TERMINAL_CLIP_STATES for clip in clips)
        if name == "PACKAGE_DELIVERABLES":
            return bool(output.get("exports")) or repos.candidates.count_for_project(project_id) == 0
        return True
    except AppError:
        return False


# --------------------------------------------------------------------- stages


def _source_validation(ctx: JobContext, run_id: str) -> dict:
    repos, storage = ctx.repos, ctx.storage
    project_id = repos.project_runs.get(run_id)["project_id"]
    if not repos.projects.exists(project_id):
        raise JobFailed("project_missing", "This project no longer exists.")
    source = repos.sources.find_optional(project_id)
    if source is None:
        raise JobFailed("source_missing", "Import a video before starting production.")
    try:
        path = ensure_local(
            storage,
            source.get("relative_path"),
            legacy_path=source.get("storage_path"),
            missing_message="The source video is missing from this project's workspace.",
        )
    except StorageObjectMissing as exc:
        raise JobFailed("source_missing", exc.message) from exc
    if not path.exists() or path.stat().st_size <= 1024:
        raise JobFailed("source_missing", "The source file is empty or unreadable.")
    if int(source.get("duration_ms") or 0) <= 0:
        raise JobFailed("invalid_source", "The source duration could not be measured.")
    if not source.get("has_video"):
        raise JobFailed("no_video_stream", "This source has no video stream to export.")
    _progress(ctx, run_id, "SOURCE_VALIDATION", 1.0, "Source is readable")
    return {
        "source_id": source["id"],
        "duration_ms": int(source["duration_ms"]),
        "has_video": bool(source.get("has_video")),
        "has_audio": bool(source.get("has_audio")),
        "identity": source_identity(source),
    }


def _source_analysis(ctx: JobContext, run_id: str) -> dict:
    repos, settings, storage = ctx.repos, ctx.settings, ctx.storage
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    request = run["request"]
    if repos.transcripts.latest(project_id) and repos.candidates.count_for_project(project_id):
        _progress(ctx, run_id, "SOURCE_ANALYSIS", 1.0, "Reusing the stored transcript and moments")
        return {"reused": True, "candidates": repos.candidates.count_for_project(project_id)}
    analysis_request = {
        "goal": request.get("goal") or "best_moments",
        "clip_count": int(request.get("clip_count") or 5),
        "duration_band": request.get("duration_band") or "auto",
        "quality": request.get("quality") or "balanced",
        "language": request.get("language"),
        "vad": True,
        # The director is the single AI editorial step; rescoring here would be
        # a second, unreviewable opinion paid for twice.
        "use_llm": False,
        "llm_consent": False,
    }

    def on_stage(stage: str, percent: float, message: str) -> None:
        _progress(
            ctx,
            run_id,
            "SOURCE_ANALYSIS",
            max(0.0, min(1.0, percent / 100.0)),
            message,
        )

    result = run_analysis(
        repos,
        settings,
        storage,
        project_id=project_id,
        request=analysis_request,
        on_stage=on_stage,
        should_cancel=ctx.should_cancel,
        cancel_event=ctx.cancel_event,
    )
    return {
        "reused": bool(result.get("transcript_cached")),
        "analysis_run_id": result.get("analysis_run_id"),
        "candidates": int(result.get("candidates") or 0),
        "language": result.get("language"),
        "warnings": result.get("warnings") or [],
    }


def _moment_discovery(ctx: JobContext, run_id: str) -> dict:
    repos, settings, storage = ctx.repos, ctx.settings, ctx.storage
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    request = run["request"]
    candidates = repos.candidates.list_for_project(project_id)
    wanted = max(1, int(request.get("clip_count") or 5))
    if len(candidates) >= wanted or not request.get("allow_extra_discovery", True):
        _progress(ctx, run_id, "MOMENT_DISCOVERY", 1.0, f"{len(candidates)} candidate moment(s) available")
        return {"count": len(candidates), "requested": wanted, "exhausted": len(candidates) < wanted}

    try:
        result = run_discovery(
            repos,
            settings,
            storage,
            project_id=project_id,
            request={
                "count": min(settings.max_moments_per_request, wanted - len(candidates)),
                "exclude_existing": True,
            },
            on_stage=lambda stage, percent, message: _progress(
                ctx, run_id, "MOMENT_DISCOVERY", max(0.0, min(1.0, percent / 100.0)), message
            ),
            should_cancel=ctx.should_cancel,
            cancel_event=ctx.cancel_event,
        )
        total = repos.candidates.count_for_project(project_id)
        return {
            "count": total,
            "requested": wanted,
            "added": int(result.get("found") or 0),
            "exhausted": bool(result.get("exhausted")) or total < wanted,
            "warnings": result.get("warnings") or [],
        }
    except AppError as exc:
        logger.warning("Additional discovery failed; continuing with existing moments: %s", exc)
        return {
            "count": len(candidates),
            "requested": wanted,
            "exhausted": True,
            "warnings": [f"Additional moment discovery failed: {exc.message}"],
        }


def _editorial_planning(ctx: JobContext, run_id: str) -> dict:
    repos, settings = ctx.repos, ctx.settings
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    project = repos.projects.get(project_id)
    source = repos.sources.get(project_id)
    transcript = repos.transcripts.latest(project_id)
    candidates = repos.candidates.list_for_project(project_id)
    if not candidates:
        raise JobFailed("no_candidates", "No candidate moments exist for this project.")
    profile = repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    request = run["request"]
    registry = LlmProviderRegistry(settings)
    _progress(ctx, run_id, "EDITORIAL_PLANNING", 0.2, "Preparing the editorial brief")

    decision = run_director(
        repos,
        settings,
        registry,
        project=project,
        source=source,
        candidates=candidates,
        transcript=transcript,
        resolved_settings=resolved["settings"],
        request=request,
    )
    _progress(ctx, run_id, "EDITORIAL_PLANNING", 0.75, "Validating the selected clips")

    wanted = max(1, int(request.get("clip_count") or 5))
    discovery_stage = repos.project_stages.get(run_id, "MOMENT_DISCOVERY") or {}
    discovery_output = dict(discovery_stage.get("output") or {})
    if (
        len(decision.get("selected") or []) < wanted
        and request.get("allow_extra_discovery", True)
        and not discovery_output.get("director_extra")
        and bool(decision.get("requests_more_moments"))
    ):
        # The director may ask for more material. One extra targeted pass is
        # allowed; it reuses the stored transcript and never re-runs analysis.
        missing = max(1, wanted - len(decision.get("selected") or []))
        try:
            extra = run_discovery(
                repos,
                settings,
                ctx.storage,
                project_id=project_id,
                request={"count": missing, "exclude_existing": True},
                on_stage=lambda stage, percent, message: _progress(
                    ctx, run_id, "EDITORIAL_PLANNING", 0.8 + min(0.1, percent / 1000), message
                ),
                should_cancel=ctx.should_cancel,
                cancel_event=ctx.cancel_event,
            )
            added = int(extra.get("found") or 0)
            discovery_output.update(
                {
                    "director_extra": True,
                    "added": added,
                    "exhausted": bool(extra.get("exhausted")) or added == 0,
                }
            )
            repos.project_stages.complete(
                run_id, "MOMENT_DISCOVERY", discovery_output,
                message=f"{discovery_output.get('count', len(candidates))} candidate moment(s); "
                        f"director requested {added} more",
            )
            if added:
                candidates = repos.candidates.list_for_project(project_id)
                decision = run_director(
                    repos,
                    settings,
                    registry,
                    project=project,
                    source=source,
                    candidates=candidates,
                    transcript=transcript,
                    resolved_settings=resolved["settings"],
                    request=request,
                )
        except AppError as exc:
            logger.warning("Director-requested discovery failed: %s", exc)
            discovery_output["director_extra"] = True
            repos.project_stages.complete(run_id, "MOMENT_DISCOVERY", discovery_output)

    clips = repos.project_clips.replace(
        run_id,
        project_id,
        [
            {"candidate_id": clip["candidate_id"], "strategy": clip.get("strategy"), "plan": clip}
            for clip in decision.get("selected") or []
        ],
    )
    if not clips:
        raise JobFailed(
            "no_grounded_selection",
            "The editorial director produced no clip that could be grounded in the source.",
        )
    provider = {
        **engine_descriptor(decision),
        "summary": director_document(decision),
    }
    repos.project_runs.set_provider(run_id, provider)
    _progress(
        ctx,
        run_id,
        "EDITORIAL_PLANNING",
        1.0,
        f"{len(clips)} clip(s) selected",
    )
    return {
        **director_document(decision),
        "decision": decision,
        "clips": len(clips),
        "engine": provider,
    }


def _plan_validation(ctx: JobContext, run_id: str) -> dict:
    repos = ctx.repos
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    source = repos.sources.get(project_id)
    duration = int(source.get("duration_ms") or 0)
    clips = repos.project_clips.list(run_id)
    valid = 0
    invalid = 0
    for clip in clips:
        plan = clip.get("plan") or {}
        segments = plan.get("segments") or []
        problems: list[str] = []
        if not segments:
            problems.append("No segments were planned.")
        total = 0
        for _, segment in enumerate(segments):
            try:
                start, end = int(segment["start_ms"]), int(segment["end_ms"])
            except (KeyError, TypeError, ValueError):
                problems.append("A segment has no usable source times.")
                continue
            if start < 0 or end <= start or end > duration:
                problems.append(f"Segment {start}-{end} ms is outside the source.")
            total += max(0, end - start)
        if total < 2_000:
            problems.append("The clip is shorter than 2 seconds.")
        if not plan.get("strategy"):
            problems.append("No editorial strategy was recorded.")
        if problems:
            invalid += 1
            repos.project_clips.update(
                clip["id"],
                status="failed",
                findings=[
                    {
                        "id": "plan_invalid",
                        "severity": "blocking",
                        "category": "editorial",
                        "title": "Edit plan could not be rendered",
                        "detail": " ".join(problems)[:600],
                        "output_range": None,
                        "source_range": None,
                        "evidence": problems,
                        "confidence": None,
                        "auto_repairable": False,
                        "proposed_repair": None,
                        "validation": "Re-plan the clip.",
                    }
                ],
            )
        else:
            valid += 1
    if valid == 0:
        raise JobFailed("plan_invalid", "Every planned clip failed source-time validation.")
    _progress(ctx, run_id, "PLAN_VALIDATION", 1.0, f"{valid} plan(s) valid")
    return {"valid": valid, "invalid": invalid}


def _requested_render_values(run_request: dict) -> tuple[dict, set[str]]:
    """Map pipeline options into the same shape the HTTP render route accepts."""
    mapping = {
        "aspect": "aspect",
        "resolution": "resolution",
        "resolution_mode": "resolution_mode",
        "custom_width": "custom_width",
        "custom_height": "custom_height",
        "fps": "fps",
        "composition": "composition",
        "framing": "framing",
        "enhancement": "enhancement",
        "compression": "compression",
        "caption_preset": "caption_preset",
        "captions_enabled": "captions_enabled",
        "captions_burn_in": "captions_burn_in",
        "captions_sidecar": "captions_sidecar",
        "captions": "captions",
        "normalize_audio": "normalize_audio",
        "face_cam_position": "face_cam_position",
        "face_cam_rect": "face_cam_rect",
        "face_cam_overrides": "face_cam_overrides",
        "stack_split_pct": "stack_split_pct",
        "adaptive_stack": "adaptive_stack",
        "crop_x_pct": "crop_x_pct",
        "crop_y_pct": "crop_y_pct",
    }
    values: dict = {}
    explicit: set[str] = set()
    for source_key, target_key in mapping.items():
        if run_request.get(source_key) is not None:
            values[target_key] = run_request[source_key]
            explicit.add(target_key)
    return values, explicit


def _spec_for_clip(
    *,
    repos: Repositories,
    project: dict,
    source: dict,
    profile: dict | None,
    resolved: dict,
    run_request: dict,
    clip: dict,
) -> RenderSpec:
    plan = clip.get("plan") or {}
    candidate = repos.candidates.get(project["id"], clip["candidate_id"])
    requested, explicit = _requested_render_values(run_request)
    requested.setdefault("aspect", "9:16")
    requested.setdefault("resolution", 1080)
    requested.setdefault("resolution_mode", "class")
    requested.setdefault("framing", "crop")
    requested.setdefault("composition", "auto")
    requested.setdefault("face_cam_position", "bottom")
    requested.setdefault("stack_split_pct", 60.0)
    requested.setdefault("adaptive_stack", False)
    requested.setdefault("fps", None)
    requested.setdefault("custom_width", None)
    requested.setdefault("custom_height", None)
    requested.setdefault("face_cam_rect", None)
    requested.setdefault("face_cam_overrides", [])

    settings = resolved["settings"]
    composition = plan.get("composition") or "auto"
    if composition and composition != "auto":
        requested["composition"] = composition
        explicit.add("composition")
    aspect = plan.get("aspect")
    if aspect:
        requested["aspect"] = aspect
        explicit.add("aspect")

    layout = intake.layout_payload(settings, requested, explicit)
    try:
        target = normalise_target(
            aspect=layout["aspect"],
            resolution=layout["resolution"],
            resolution_mode=layout["resolution_mode"],
            source_width=source.get("width"),
            source_height=source.get("height"),
            custom_width=layout.get("custom_width"),
            custom_height=layout.get("custom_height"),
        )
    except ValueError as exc:
        raise JobFailed("invalid_resolution", str(exc)) from exc

    caption_preset, captions_enabled = intake.caption_settings(
        settings,
        requested=requested,
        explicit=explicit,
        default_preset=DEFAULT_CAPTION_PRESET,
    )
    if plan.get("caption_preset"):
        caption_preset = plan["caption_preset"]
    caption_overrides = intake.caption_overrides(
        settings,
        captions=requested.get("captions") if "captions" in explicit else None,
        captions_explicit="captions" in explicit,
    )
    audio = intake.audio_payload(
        settings,
        audio=None,
        normalize_audio=bool(requested.get("normalize_audio")),
    )
    logo, _ = intake.logo_payload(settings, logo=None)
    compression = intake.compression_payload(
        settings,
        compression=requested.get("compression"),
        explicit="compression" in explicit,
    )
    segments = [
        {
            "start_ms": int(segment["start_ms"]),
            "end_ms": int(segment["end_ms"]),
            "role": segment.get("role", "main"),
        }
        for segment in plan.get("segments") or []
    ]
    editorial = {
        "strategy": plan.get("strategy"),
        "label": strategy_label(str(plan.get("strategy") or "chronological")),
        "rationale": plan.get("rationale"),
        "confidence": plan.get("confidence"),
        "applied": True,
        "reordered": len(segments) > 1 and [s["start_ms"] for s in segments]
        != sorted(s["start_ms"] for s in segments),
        "director": {
            "mode": plan.get("mode"),
            "provider": plan.get("provider"),
            "model": plan.get("model"),
            "privacy": plan.get("privacy"),
            "risks": plan.get("risks") or [],
            "evidence": plan.get("evidence") or [],
        },
    }
    spec = build_spec(
        source_id=source["id"],
        segments=segments,
        aspect=layout["aspect"],
        resolution=target["resolution"],
        resolution_mode=target["resolution_mode"],
        custom_dimensions=target["custom_dimensions"],
        framing=layout["framing"],
        composition=layout["composition"],
        compression=compression,
        enhancement=layout.get("enhancement"),
        crop_x_pct=layout.get("crop_x_pct"),
        crop_y_pct=layout.get("crop_y_pct"),
        face_cam_position=layout.get("face_cam_position") or "bottom",
        face_cam_rect=layout.get("face_cam_rect"),
        face_cam_overrides=layout.get("face_cam_overrides") or [],
        stack_split_pct=float(layout.get("stack_split_pct") or 60.0),
        adaptive_stack=bool(layout.get("adaptive_stack")),
        caption_preset=caption_preset,
        captions_enabled=captions_enabled,
        caption_overrides=caption_overrides,
        captions_burn_in=bool(requested.get("captions_burn_in", True)),
        captions_sidecar=bool(requested.get("captions_sidecar", False)),
        audio=audio,
        logo=logo,
        fps=layout.get("fps"),
        title=candidate.get("title"),
        hook=plan.get("hook"),
        editorial=editorial,
        duration=candidate.get("duration") or {},
        chronological=bool((settings.get("brief") or {}).get("hook", {}).get("strict_chronological", False)),
        profile={
            "id": (profile or {}).get("id"),
            "name": (profile or {}).get("name"),
            "version": (profile or {}).get("version"),
        },
        profile_snapshot=profile_snapshot(profile),
        edit_spec_id=None,
    )
    spec.validate_against(int(source["duration_ms"]))
    return spec


def _configuration(ctx: JobContext, run_id: str) -> dict:
    repos = ctx.repos
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    project = repos.projects.get(project_id)
    source = repos.sources.get(project_id)
    profile = repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    clips = repos.project_clips.list(run_id)
    configured = 0
    failed = 0
    for index, clip in enumerate(clips):
        _check_cancel(ctx, run_id)
        # A retry re-enters this stage after the user fixed the export settings.
        # Clips that failed only here have no spec to preserve, so re-attempt
        # them; render failures and plan failures keep their existing verdict.
        failed_configuration = any(
            finding.get("id") == "configuration_invalid"
            for finding in clip.get("findings") or []
        )
        if clip["status"] in {"failed", "needs_review"} and not failed_configuration:
            continue
        try:
            spec = _spec_for_clip(
                repos=repos,
                project=project,
                source=source,
                profile=profile,
                resolved=resolved,
                run_request=run["request"],
                clip=clip,
            )
        except (JobFailed, ValueError) as exc:
            failed += 1
            repos.project_clips.update(
                clip["id"],
                status="failed",
                findings=[
                    {
                        "id": "configuration_invalid",
                        "severity": "blocking",
                        "category": "technical",
                        "title": "Clip configuration failed",
                        "detail": getattr(exc, "message", str(exc))[:600],
                        "output_range": None,
                        "source_range": None,
                        "evidence": [],
                        "confidence": None,
                        "auto_repairable": False,
                        "proposed_repair": None,
                        "validation": "Correct the project export settings.",
                    }
                ],
            )
            continue
        plan = dict(clip.get("plan") or {})
        plan["spec"] = spec.model_dump()
        repos.project_clips.update(
            clip["id"],
            status="configured",
            strategy=str(plan.get("strategy") or ""),
            plan=plan,
            edl_version=1,
            findings=[],
        )
        configured += 1
        _progress(
            ctx,
            run_id,
            "CONFIGURATION",
            (index + 1) / max(1, len(clips)),
            f"Configured clip {index + 1} of {len(clips)}",
        )
    if configured == 0:
        raise JobFailed("configuration_failed", "No clip could be configured for rendering.")
    _progress(ctx, run_id, "CONFIGURATION", 1.0, f"{configured} clip(s) configured")
    return {"configured": configured, "failed": failed}


def _render_clip(
    ctx: JobContext,
    *,
    run_id: str,
    project: dict,
    clip: dict,
    spec: RenderSpec,
    display_strategy: str | None,
    attempt_label: str,
    repair_attempts: int,
    technical_error_count: int,
) -> dict:
    repos, settings, storage = ctx.repos, ctx.settings, ctx.storage
    project_id = project["id"]
    source = repos.sources.get(project_id)
    identity = source_identity(source)
    fingerprint = spec_fingerprint(spec)
    reusable = repos.renders.find_reusable(
        project_id, spec_hash=fingerprint, source_identity=identity
    )
    if reusable is not None:
        return {"render": reusable, "reused": True, "attempts": 0}

    attempts = 0
    last_error: JobFailed | None = None
    while attempts <= settings.autoproduce_technical_retries:
        attempts += 1
        version = repos.renders.next_version(project_id)
        filename = export_filename(
            project_name=project.get("name") or "project",
            strategy=display_strategy,
            aspect=spec.video.aspect,
            version=version,
        )
        render = repos.renders.create(
            project_id=project_id,
            job_id=ctx.job["id"],
            candidate_id=clip.get("candidate_id"),
            spec=spec.model_dump(),
            status="queued",
            kind="export",
            profile_snapshot=spec.profile_snapshot,
            version=version,
            display_name=filename,
            spec_hash=fingerprint,
            source_identity=identity,
            run_id=run_id,
            clip_id=clip["id"],
            edl_version=int((clip.get("plan") or {}).get("edl_version") or 1),
        )
        repos.project_clips.append_history(
            clip["id"],
            {
                "at": render["created_at"],
                "action": "render_started",
                "render_id": render["id"],
                "version": version,
                "filename": filename,
                "label": attempt_label,
                "technical_attempt": attempts,
                "repair_attempt": repair_attempts,
            },
        )
        repos.project_clips.update(
            clip["id"],
            status="rendering",
            render_id=render["id"],
            technical_errors=technical_error_count + attempts - 1,
            repair_attempts=repair_attempts,
        )
        repos.renders.mark_running(render["id"])
        try:
            perform_render(
                repos,
                settings,
                storage,
                render_id=render["id"],
                on_stage=lambda stage, percent, message: _progress(
                    ctx,
                    run_id,
                    "RENDERING",
                    percent / 100.0,
                    f"{attempt_label}: {message}",
                ),
                cancel_event=ctx.cancel_event,
                should_cancel=ctx.should_cancel,
                timeout_seconds=settings.render_timeout_seconds,
            )
        except ProcessCancelled:
            raise
        except JobFailed as exc:
            last_error = exc
            repos.project_clips.append_history(
                clip["id"],
                {
                    "at": render.get("created_at"),
                    "action": "render_failed",
                    "render_id": render["id"],
                    "code": exc.code,
                    "message": exc.message,
                    "technical_attempt": attempts,
                },
            )
            if exc.retryable or attempts <= settings.autoproduce_technical_retries:
                continue
            break
        except Exception as exc:
            last_error = JobFailed("render_failed", str(exc)[:400])
            repos.project_clips.append_history(
                clip["id"],
                {
                    "at": render.get("created_at"),
                    "action": "render_failed",
                    "render_id": render["id"],
                    "code": "render_failed",
                    "message": str(exc)[:300],
                    "technical_attempt": attempts,
                },
            )
            if attempts <= settings.autoproduce_technical_retries:
                continue
            break

        local = ensure_local(
            storage,
            f"projects/{project_id}/renders/{render['id']}.mp4",
            missing_message="The rendered file is missing after encoding.",
        )
        checksum = checksum_for(local)
        if checksum:
            repos.renders.set_checksum(render["id"], checksum)
        completed = repos.renders.get(render["id"])
        return {"render": completed, "reused": False, "attempts": attempts}

    raise last_error or JobFailed("render_failed", "The clip could not be rendered.")


def _rendering(ctx: JobContext, run_id: str) -> dict:
    repos = ctx.repos
    run = repos.project_runs.get(run_id)
    project = repos.projects.get(run["project_id"])
    clips = repos.project_clips.list(run_id)
    rendered = 0
    failed = 0
    for index, clip in enumerate(clips):
        _check_cancel(ctx, run_id)
        if clip["status"] in TERMINAL_CLIP_STATES:
            continue
        plan = clip.get("plan") or {}
        raw_spec = plan.get("spec")
        if not raw_spec:
            continue
        spec = RenderSpec.model_validate(raw_spec)
        try:
            outcome = _render_clip(
                ctx,
                run_id=run_id,
                project=project,
                clip=clip,
                spec=spec,
                display_strategy=plan.get("strategy"),
                attempt_label="Initial render",
                repair_attempts=int(clip.get("repair_attempts") or 0),
                technical_error_count=int(clip.get("technical_errors") or 0),
            )
        except JobFailed as exc:
            failed += 1
            repos.project_clips.update(
                clip["id"],
                status="failed",
                findings=[
                    {
                        "id": "render_failed",
                        "severity": "blocking",
                        "category": "technical",
                        "title": "Render failed",
                        "detail": exc.message[:600],
                        "output_range": None,
                        "source_range": None,
                        "evidence": [f"code={exc.code}"],
                        "confidence": None,
                        "auto_repairable": False,
                        "proposed_repair": None,
                        "validation": "Retry the run after correcting the technical cause.",
                    }
                ],
            )
            continue
        repos.project_clips.update(
            clip["id"], status="rendered", render_id=outcome["render"]["id"]
        )
        # The renderer expands the scene plan before encoding. Persist that
        # expanded spec on the clip so a repair edits the layout that actually
        # produced the file, not the pre-render auto placeholder.
        persisted_plan = dict(clip.get("plan") or {})
        persisted_plan["spec"] = outcome["render"].get("spec") or persisted_plan.get("spec")
        repos.project_clips.update(clip["id"], plan=persisted_plan)
        rendered += 1
        _progress(
            ctx,
            run_id,
            "RENDERING",
            (index + 1) / max(1, len(clips)),
            f"Rendered clip {index + 1} of {len(clips)}",
        )
    if rendered == 0 and failed > 0:
        raise JobFailed("render_failed", "No clip rendered successfully.")
    _progress(ctx, run_id, "RENDERING", 1.0, f"{rendered} clip(s) rendered")
    return {"rendered": rendered, "failed": failed}


def _review_context(repos: Repositories, project_id: str, render: dict, spec: RenderSpec):
    source = repos.sources.get(project_id)
    transcript = repos.transcripts.latest(project_id)
    segments = (transcript or {}).get("segments") or []
    return source, segments


def _audio_path(storage: StorageBackend, project_id: str, render_id: str) -> Path | None:
    try:
        return ensure_local(
            storage,
            f"projects/{project_id}/renders/{render_id}.mp4",
            missing_message="Rendered file missing",
        )
    except StorageObjectMissing:
        return None


def _review_clip(
    repos: Repositories,
    storage: StorageBackend,
    project_id: str,
    clip: dict,
) -> dict | None:
    render_id = clip.get("render_id")
    if not render_id:
        return None
    try:
        render = repos.renders.get(render_id)
    except AppError:
        return None
    spec = RenderSpec.model_validate(render.get("spec") or {})
    source, transcript_segments = _review_context(repos, project_id, render, spec)
    path = _audio_path(storage, project_id, render_id)
    audio = measure_audio(path) if path is not None else {"measured": False, "detail": "file missing"}
    report = build_review_report(
        render=render,
        spec=spec,
        source=source,
        gate=render.get("review") or {},
        transcript_segments=transcript_segments,
        audio=audio,
        strategy=(clip.get("strategy") or None),
    )
    report["audio"] = audio
    repos.renders.set_review(render_id, report)
    repos.renders.set_review_status(render_id, report["status"])
    status = {
        "pass": "passed",
        "warn": "passed_with_warnings",
        "needs_review": "needs_review",
        "fail": "needs_review",
    }.get(report["status"], "needs_review")
    repos.project_clips.update(
        clip["id"],
        status=status if status != "needs_review" or not report.get("findings") else "requires_repair",
        findings=report.get("findings") or [],
        review=report,
    )
    return report


def _render_validation(ctx: JobContext, run_id: str) -> dict:
    repos = ctx.repos
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    clips = repos.project_clips.list(run_id)
    reviewed = 0
    for clip in clips:
        _check_cancel(ctx, run_id)
        if clip["status"] not in {"rendered", "requires_repair"}:
            continue
        report = _review_clip(repos, ctx.storage, project_id, clip)
        if report:
            reviewed += 1
        _progress(
            ctx,
            run_id,
            "RENDER_VALIDATION",
            min(1.0, (reviewed + 1) / max(1, len(clips))),
            f"Checked {reviewed} rendered clip(s)",
        )
    _progress(ctx, run_id, "RENDER_VALIDATION", 1.0, f"{reviewed} clip(s) checked")
    return {"reviewed": reviewed}


def _model_review(ctx: JobContext, run_id: str) -> dict:
    """Optional model review of measured evidence; never claims to watch frames."""
    repos, settings = ctx.repos, ctx.settings
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    if not settings.llm_editorial_enabled or not run["request"].get("use_model_review", True):
        for clip in repos.project_clips.list(run_id):
            review = clip.get("review") or {}
            if not review:
                continue
            review["model_review"] = {
                "status": "not_run",
                "reason": "Model review is disabled; deterministic checks ran on the rendered file.",
                "basis": "deterministic media, EDL and transcript evidence",
                "findings": [],
            }
            repos.project_clips.update(clip["id"], review=review)
        return {"status": "not_run", "clips": 0}

    registry = LlmProviderRegistry(settings)
    selection = registry.select()
    if (
        selection.usable
        and selection.info.get("privacy") == "remote"
        and not run["request"].get("model_review_consent", False)
    ):
        for clip in repos.project_clips.list(run_id):
            review = clip.get("review") or {}
            if not review:
                continue
            review["model_review"] = {
                "status": "unavailable",
                "reason": "Remote model review requires explicit consent for this request.",
                "basis": "not run",
                "findings": [],
            }
            repos.project_clips.update(clip["id"], review=review)
        return {"status": "unavailable", "clips": 0}
    if not selection.usable or selection.adapter is None:
        for clip in repos.project_clips.list(run_id):
            review = clip.get("review") or {}
            if not review:
                continue
            review["model_review"] = {
                "status": "unavailable",
                "reason": selection.reason or "No usable model.",
                "basis": "not run",
                "findings": [],
            }
            repos.project_clips.update(clip["id"], review=review)
        return {"status": "unavailable", "clips": 0}

    reviewed = 0
    for clip in repos.project_clips.list(run_id):
        review = clip.get("review") or {}
        if not review:
            continue
        try:
            findings = _ask_model_review(selection.adapter, clip, review, repos, project_id)
            review["model_review"] = {
                "status": "completed",
                "provider": selection.info.get("provider"),
                "model": selection.info.get("model"),
                "privacy": selection.info.get("privacy"),
                "basis": "transcript boundaries and measured composition metadata; no frame inspection",
                "findings": findings,
            }
            reviewed += 1
        except Exception as exc:
            review["model_review"] = {
                "status": "unavailable",
                "reason": f"The model review failed: {type(exc).__name__}",
                "basis": "not run",
                "findings": [],
            }
        all_findings = [
            finding
            for finding in review.get("findings") or []
            if finding.get("origin") != "model"
        ] + list((review.get("model_review") or {}).get("findings") or [])
        review["findings"] = all_findings
        if all_findings and review.get("status") == "pass":
            review["status"] = "warn"
            review["summary"] = (
                f"{len(all_findings)} review note(s), including model observations; "
                "the deterministic checks passed."
            )
        repos.project_clips.update(clip["id"], review=review)
        if clip.get("render_id"):
            repos.renders.set_review(clip["render_id"], review)
            repos.renders.set_review_status(clip["render_id"], review.get("status") or "warn")
        _progress(
            ctx,
            run_id,
            "EDITORIAL_REVIEW",
            min(1.0, (reviewed + 1) / max(1, len(repos.project_clips.list(run_id)))),
            "Reviewing the rendered edit",
        )
    _progress(ctx, run_id, "EDITORIAL_REVIEW", 1.0, f"{reviewed} clip(s) reviewed")
    return {"status": "completed", "clips": reviewed}


def _ask_model_review(adapter, clip: dict, review: dict, repos: Repositories,
                      project_id: str) -> list[dict]:
    transcript = repos.transcripts.latest(project_id)
    spec = RenderSpec.model_validate((repos.renders.get(clip["render_id"]).get("spec") or {}))
    duration_ms = spec.total_duration_ms
    evidence = {
        "strategy": clip.get("strategy"),
        "segments": [
            {
                "source_start_ms": segment.start_ms,
                "source_end_ms": segment.end_ms,
                "role": segment.role,
                "composition": segment.composition.model_dump() if segment.composition else None,
            }
            for segment in spec.segments
        ],
        "output_duration_ms": duration_ms,
        "transcript": [
            {
                "start_ms": segment.get("start_ms"),
                "end_ms": segment.get("end_ms"),
                "text": (segment.get("text") or "")[:240],
            }
            for segment in (transcript or {}).get("segments", [])
            if int(segment.get("end_ms") or 0) > spec.segments[0].start_ms
            and int(segment.get("start_ms") or 0) < spec.segments[-1].end_ms
        ][:40],
        "deterministic_findings": [
            {
                "id": finding.get("id"),
                "severity": finding.get("severity"),
                "title": finding.get("title"),
                "detail": finding.get("detail"),
            }
            for finding in review.get("findings") or []
        ],
    }
    system = (
        "You review a clip edit from measured evidence. You have NOT seen or heard the video; "
        "never claim you did. Judge only coherence, whether the edit starts and ends at a "
        "supported thought, and whether the stated strategy is supported by the segment order. "
        "Do not invent dialogue or events, and do not output a virality or audience score. "
        "Return JSON: {\"findings\":[{\"id\":\"short_snake_case\",\"severity\":\"warning\","
        "\"category\":\"editorial\",\"title\":\"...\",\"detail\":\"...\","
        "\"output_start_ms\":0,\"output_end_ms\":1000,\"confidence\":0.0}]}"
    )
    parsed, _meta = adapter.complete_json_meta(
        system, "Evidence:\n" + json.dumps(evidence, ensure_ascii=False),
        max_tokens=1_500,
    )
    findings: list[dict] = []
    for index, raw in enumerate(parsed.get("findings") or []):
        if not isinstance(raw, dict):
            continue
        try:
            start = int(raw.get("output_start_ms") or 0)
            end = int(raw.get("output_end_ms") or start)
        except (TypeError, ValueError):
            start = end = 0
        start = max(0, min(duration_ms, start))
        end = max(start, min(duration_ms, end))
        confidence = raw.get("confidence")
        try:
            confidence = None if confidence is None else max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None
        findings.append(
            {
                "id": f"model_{str(raw.get('id') or f'finding_{index}')[:60]}",
                "severity": "warning",
                "category": "editorial",
                "title": str(raw.get("title") or "Editorial review note")[:160],
                "detail": str(raw.get("detail") or "")[:800],
                "output_range": {"start_ms": start, "end_ms": end},
                "source_range": None,
                "evidence": ["model_review", "no frame inspection"],
                "confidence": confidence,
                "auto_repairable": False,
                "proposed_repair": None,
                "validation": "Human review.",
                "origin": "model",
            }
        )
    return findings[:8]


def _repair_planning(ctx: JobContext, run_id: str) -> dict:
    repos = ctx.repos
    limit = ctx.settings.autoproduce_repair_limit
    planned = 0
    for clip in repos.project_clips.list(run_id):
        review = clip.get("review") or {}
        if not review or clip["status"] in {"failed", "passed", "passed_with_warnings"}:
            continue
        plan = dict(clip.get("plan") or {})
        applied = set(plan.get("applied_repairs") or [])
        repair = choose_repair(
            review,
            applied=applied,
            attempts_left=max(0, limit - int(clip.get("repair_attempts") or 0)),
        )
        if repair is None:
            if clip["status"] == "requires_repair":
                repos.project_clips.update(clip["id"], status="needs_review")
            continue
        plan["pending_repair"] = repair
        repos.project_clips.update(
            clip["id"], plan=plan, status="repair_planned",
            repair_attempts=int(clip.get("repair_attempts") or 0),
        )
        planned += 1
    _progress(ctx, run_id, "REPAIR_PLANNING", 1.0, f"{planned} repair(s) planned")
    return {"planned": planned, "limit": limit}


def _render_repair(ctx: JobContext, run_id: str) -> dict:
    repos = ctx.repos
    run = repos.project_runs.get(run_id)
    clips = repos.project_clips.list(run_id)
    project = repos.projects.get(run["project_id"])
    source = repos.sources.get(run["project_id"])
    limit = ctx.settings.autoproduce_repair_limit
    repaired = 0
    unresolved = 0
    for clip in clips:
        _check_cancel(ctx, run_id)
        if clip["status"] != "repair_planned":
            continue
        plan = dict(clip.get("plan") or {})
        applied_repairs = set(plan.get("applied_repairs") or [])
        attempts = int(clip.get("repair_attempts") or 0)
        best: tuple[tuple[int, int, int], str, dict] | None = None
        current_report = clip.get("review") or {}
        if current_report:
            best = (_report_rank(current_report), clip["render_id"], current_report)
        spec = RenderSpec.model_validate(plan.get("spec") or {})
        while True:
            repair = plan.get("pending_repair")
            if not repair or attempts >= limit:
                break
            updated, change = apply_repair(
                spec, repair, source_duration_ms=int(source["duration_ms"])
            )
            if not change.get("applied"):
                applied_repairs.add(str(repair.get("kind")))
                plan["applied_repairs"] = sorted(applied_repairs)
                plan.pop("pending_repair", None)
                break
            attempts += 1
            repaired += 1
            edl_version = int(plan.get("edl_version") or 1) + 1
            plan["spec"] = updated.model_dump()
            plan["edl_version"] = edl_version
            applied_repairs.add(str(repair.get("kind")))
            plan["applied_repairs"] = sorted(applied_repairs)
            plan.pop("pending_repair", None)
            spec = updated
            repos.project_clips.update(
                clip["id"],
                plan=plan,
                edl_version=edl_version,
                repair_attempts=attempts,
                status="rendering",
            )
            try:
                outcome = _render_clip(
                    ctx,
                    run_id=run_id,
                    project=project,
                    clip=clip,
                    spec=updated,
                    display_strategy=plan.get("strategy"),
                    attempt_label=f"Repair {attempts}: {repair.get('kind')}",
                    repair_attempts=attempts,
                    technical_error_count=int(clip.get("technical_errors") or 0),
                )
            except JobFailed as exc:
                plan["pending_repair"] = None
                plan["applied_repairs"] = sorted(applied_repairs)
                repos.project_clips.update(
                    clip["id"], plan=plan, status="needs_review",
                    findings=[*(clip.get("findings") or []), {
                        "id": "repair_render_failed",
                        "severity": "blocking",
                        "category": "technical",
                        "title": "Repair render failed",
                        "detail": exc.message[:500],
                        "output_range": None,
                        "source_range": None,
                        "evidence": [f"code={exc.code}", f"repair={repair.get('kind')}"],
                        "confidence": None,
                        "auto_repairable": False,
                        "proposed_repair": None,
                        "validation": "Human review.",
                    }],
                )
                unresolved += 1
                break
            plan["spec"] = outcome["render"].get("spec") or updated.model_dump()
            repos.project_clips.update(clip["id"], plan=plan)
            report = _review_clip(repos, ctx.storage, run["project_id"], {**clip, "render_id": outcome["render"]["id"]})
            if report is None:
                break
            repos.project_clips.append_history(
                clip["id"],
                {
                    "action": "repair_applied",
                    "repair": repair,
                    "changes": change.get("changes") or [],
                    "render_id": outcome["render"]["id"],
                    "report_status": report.get("status"),
                    "edl_version": edl_version,
                    "attempt": attempts,
                },
            )
            candidate_rank = _report_rank(report)
            if best is None or candidate_rank < best[0]:
                best = (candidate_rank, outcome["render"]["id"], report)
            if report.get("status") in {"pass", "warn"}:
                break
            next_repair = choose_repair(
                report,
                applied=applied_repairs,
                attempts_left=limit - attempts,
            )
            if next_repair is None:
                break
            plan["pending_repair"] = next_repair
            # Persist which repair will be tried next so a restart can continue.
            repos.project_clips.update(clip["id"], plan=plan)

        if best is not None:
            best_rank, best_render_id, best_report = best
            status = "passed" if best_report.get("status") == "pass" else (
                "passed_with_warnings" if best_report.get("status") == "warn" else "needs_review"
            )
            repos.project_clips.update(
                clip["id"],
                status=status,
                render_id=best_render_id,
                findings=best_report.get("findings") or [],
                review=best_report,
                repair_attempts=attempts,
            )
            repos.renders.set_review_status(best_render_id, best_report.get("status") or "")
            repos.renders.mark_superseded(run["project_id"], clip.get("candidate_id"), best_render_id)
            if status == "needs_review":
                unresolved += 1
        else:
            repos.project_clips.update(clip["id"], status="needs_review", repair_attempts=attempts)
            unresolved += 1
        _progress(
            ctx,
            run_id,
            "RENDER_REPAIR",
            min(1.0, (repaired + 1) / max(1, len(clips))),
            f"{repaired} repair render(s) completed",
        )
    _progress(ctx, run_id, "RENDER_REPAIR", 1.0, f"{repaired} repair render(s); {unresolved} need review")
    return {"repaired": repaired, "unresolved": unresolved}


def _report_rank(report: dict) -> tuple[int, int, int]:
    findings = report.get("findings") or []
    blocking = len([item for item in findings if item.get("severity") == "blocking"])
    warnings = len([item for item in findings if item.get("severity") == "warning"])
    repairable = len([item for item in findings if item.get("auto_repairable")])
    return (blocking, warnings, repairable)


def _final_qa(ctx: JobContext, run_id: str) -> dict:
    repos = ctx.repos
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    clips = repos.project_clips.list(run_id)
    passed = 0
    warnings = 0
    needs_review = 0
    failed = 0
    for clip in clips:
        if clip["status"] in {"repair_planned", "requires_repair", "rendered", "rendering"}:
            repos.project_clips.update(clip["id"], status="needs_review")
            clip = repos.project_clips.get(clip["id"])
        if clip["status"] in {"passed", "passed_with_warnings"}:
            render = repos.renders.get(clip["render_id"]) if clip.get("render_id") else None
            technical_ok = bool(
                render
                and render.get("status") == "completed"
                and render.get("size_bytes")
                and render.get("duration_ms")
                and render.get("review_status") in {"pass", "warn"}
                and (render.get("review") or {}).get("checks")
            )
            if not technical_ok:
                repos.project_clips.update(clip["id"], status="needs_review")
                needs_review += 1
                continue
            if not render.get("checksum"):
                try:
                    local = ensure_local(
                        ctx.storage,
                        f"projects/{project_id}/renders/{render['id']}.mp4",
                        missing_message="Rendered file missing",
                    )
                    checksum = checksum_for(local)
                    if checksum:
                        repos.renders.set_checksum(render["id"], checksum)
                except StorageObjectMissing:
                    repos.project_clips.update(clip["id"], status="needs_review")
                    needs_review += 1
                    continue
            if clip["status"] == "passed":
                passed += 1
            else:
                warnings += 1
        elif clip["status"] == "needs_review":
            needs_review += 1
        else:
            failed += 1

    total = len(clips)
    if passed + warnings > 0 and (needs_review or failed):
        status = "partially_completed"
    elif passed + warnings > 0:
        status = "completed"
    elif needs_review:
        status = "awaiting_review"
    else:
        status = "failed"
    repos.project_runs.set_status(
        run_id,
        status,
        message=(
            f"{passed} clean, {warnings} with warnings, {needs_review} need review,"
            f" {failed} failed"
        ),
    )
    _progress(
        ctx,
        run_id,
        "FINAL_QA",
        1.0,
        f"{passed + warnings} of {total} clip(s) passed technical validation",
    )
    return {
        "passed": passed,
        "with_warnings": warnings,
        "needs_review": needs_review,
        "failed": failed,
        "total": total,
        "status": status,
    }


def _package(ctx: JobContext, run_id: str) -> dict:
    repos, settings = ctx.repos, ctx.settings
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    clips = repos.project_clips.list(run_id)
    current: list[str] = []
    for clip in clips:
        if clip["status"] not in {"passed", "passed_with_warnings"} or not clip.get("render_id"):
            continue
        render = repos.renders.get(clip["render_id"])
        repos.renders.mark_superseded(project_id, clip.get("candidate_id"), render["id"])
        current.append(render["id"])
    saved: list[dict] = []
    save_errors: list[str] = []
    for render_id in current:
        try:
            render = repos.renders.get(render_id)
            result = save_render(repos, settings, ctx.storage, render)
            saved.append({"render_id": render_id, **result})
        except Exception as exc:
            logger.warning("Could not copy export %s to the export folder: %s", render_id, exc)
            save_errors.append(f"Could not save export {render_id}: {type(exc).__name__}")
    manifest = build_manifest(repos, project_id)
    try:
        paths = ensure_layout(settings, project_id)
        ensure_space(settings, paths, required_bytes=64 * 1024)
        write_json_atomic(paths / "manifest.json", manifest)
        manifest["written"] = True
    except (OSError, AppError) as exc:
        logger.warning("Manifest file could not be written; the database copy remains authoritative: %s", exc)
        manifest["written"] = False
    repos.project_runs.progress(
        run_id, progress=100, message="Project manifest updated", stage="PACKAGE_DELIVERABLES"
    )
    if save_errors:
        run_row = repos.project_runs.get(run_id)
        repos.project_runs.set_status(
            run_id,
            run_row["status"],
            message=(run_row.get("message") or "") + " Export copy warning: " + save_errors[0],
        )
    _progress(
        ctx, run_id, "PACKAGE_DELIVERABLES", 1.0,
        f"{len(current)} current export(s) recorded",
    )
    return {
        "exports": current,
        "saved": saved,
        "save_errors": save_errors,
        "manifest_version": manifest.get("version"),
        "written": manifest.get("written"),
        "total_exports": len(manifest.get("exports") or []),
    }


def run_project_pipeline(ctx: JobContext, run_id: str) -> dict:
    repos = ctx.repos
    run = repos.project_runs.get(run_id)
    project_id = run["project_id"]
    if repos.project_runs.is_cancel_requested(run_id):
        repos.project_runs.set_status(run_id, "cancelled", message="Cancelled before starting")
        return {"run_id": run_id, "status": "cancelled"}

    repos.project_stages.ensure(run_id, STAGES)
    repos.project_stages.reset_incomplete(run_id)
    repos.project_runs.start(run_id)
    started = time.monotonic()
    persisted = _parse_deadline(run.get("deadline_at"))
    hard_deadline = persisted if persisted is not None else started + ctx.settings.pipeline_deadline_seconds
    # A resumed run must not begin a stage it cannot finish inside the deadline.
    hard_deadline = max(started + 60, hard_deadline)

    handlers: dict[str, Callable[[JobContext, str], dict]] = {
        "SOURCE_VALIDATION": _source_validation,
        "SOURCE_ANALYSIS": _source_analysis,
        "MOMENT_DISCOVERY": _moment_discovery,
        "EDITORIAL_PLANNING": _editorial_planning,
        "PLAN_VALIDATION": _plan_validation,
        "CONFIGURATION": _configuration,
        "RENDERING": _rendering,
        "RENDER_VALIDATION": _render_validation,
        "EDITORIAL_REVIEW": _model_review,
        "REPAIR_PLANNING": _repair_planning,
        "RENDER_REPAIR": _render_repair,
        "FINAL_QA": _final_qa,
        "PACKAGE_DELIVERABLES": _package,
    }

    try:
        for name in STAGES:
            if name == "COMPLETED":
                break
            _check_cancel(ctx, run_id)
            if time.monotonic() > hard_deadline:
                raise JobFailed(
                    "deadline_exceeded",
                    "This run reached its configured maximum runtime. Completed work was kept; retry to resume.",
                )
            existing = repos.project_stages.get(run_id, name)
            if existing and existing["status"] in {"completed", "skipped"}:
                if _stage_valid(repos, project_id, run_id, name, existing.get("output") or {}):
                    _progress(
                        ctx,
                        run_id,
                        name,
                        1.0,
                        f"{STAGE_LABELS.get(name, name)} (already complete)",
                    )
                    continue
                repos.project_stages.start(run_id, name)
            else:
                repos.project_stages.start(run_id, name)
            _STAGE_STARTED[(run_id, name)] = time.monotonic()
            _progress(ctx, run_id, name, 0.02, STAGE_LABELS.get(name, name))
            handler = handlers[name]
            output = handler(ctx, run_id)
            _STAGE_STARTED.pop((run_id, name), None)
            repos.project_stages.complete(run_id, name, output)
            _progress(ctx, run_id, name, 1.0, STAGE_LABELS.get(name, name))

        final_run = repos.project_runs.get(run_id)
        if final_run["status"] not in {"completed", "partially_completed", "awaiting_review"}:
            # FINAL_QA sets the honest status; never upgrade a failed run here.
            if final_run["status"] != "failed":
                repos.project_runs.set_status(
                    run_id, final_run["status"], message=final_run.get("message")
                )
        repos.project_stages.complete(
            run_id, "COMPLETED", {"status": final_run["status"]}
        )
        _progress(ctx, run_id, "COMPLETED", 1.0, "Production run finished")
        return {
            "run_id": run_id,
            "status": repos.project_runs.get(run_id)["status"],
            "clips": repos.project_clips.counts(run_id),
        }
    except ProcessCancelled:
        repos.project_runs.set_status(run_id, "cancelled", message="Cancelled during rendering")
        raise
    except JobFailed as exc:
        job_row = None
        try:
            job_row = repos.jobs.get(ctx.job["id"])
        except AppError:
            job_row = None
        interruption_code = (job_row or {}).get("error_code")
        if interruption_code in {"stalled", "deadline_exceeded"}:
            repos.project_runs.set_status(
                run_id,
                "failed",
                message=(job_row or {}).get("error_message")
                or "The worker stopped this run. Completed work was kept.",
                error_code=interruption_code,
                error_message=(job_row or {}).get("error_message") or "",
            )
            raise
        if exc.code == "cancelled" or ctx.should_cancel():
            repos.project_runs.set_status(run_id, "cancelled", message="Cancelled")
            raise
        current = repos.project_runs.get(run_id)
        stage = current.get("current_stage") or "unknown"
        repos.project_stages.fail(run_id, stage, code=exc.code, message=exc.message)
        if getattr(exc, "retryable", False):
            repos.project_runs.set_status(
                run_id, "retrying", message=f"{exc.message} Retrying automatically."
            )
        else:
            repos.project_runs.set_status(
                run_id, "failed", message=exc.message, error_code=exc.code, error_message=exc.message
            )
        raise
    except Exception as exc:  # unexpected
        current = repos.project_runs.get(run_id)
        stage = current.get("current_stage") or "unknown"
        repos.project_stages.fail(
            run_id, stage, code="internal_error", message="Unexpected worker error"
        )
        repos.project_runs.set_status(
            run_id,
            "failed",
            message="An unexpected error stopped this run. Completed stages and exports were kept.",
            error_code="internal_error",
            error_message=str(exc)[:500],
        )
        raise


def pipeline_state(repos: Repositories, project_id: str) -> dict:
    """The exact state the UI shows; no invented percentages."""
    run = repos.project_runs.latest(project_id)
    if run is None:
        return {
            "run": None,
            "stages": [
                {"name": name, "label": STAGE_LABELS[name], "status": "pending", "progress": 0}
                for name in STAGES
            ],
            "clips": [],
            "counts": {"total": 0},
            "manifest": build_manifest(repos, project_id) if repos.projects.exists(project_id) else None,
        }
    stages = repos.project_stages.list(run["id"])
    known = {stage["name"] for stage in stages}
    for name in STAGES:
        if name not in known:
            stages.append(
                {
                    "name": name,
                    "status": "pending",
                    "progress": 0,
                    "message": "",
                    "output": {},
                }
            )
    stage_views = []
    for stage in stages:
        stage_views.append(
            {
                "name": stage["name"],
                "label": STAGE_LABELS.get(stage["name"], stage["name"]),
                "status": stage["status"],
                "progress": stage.get("progress") or 0,
                "message": stage.get("message") or "",
                "attempt": stage.get("attempt") or 0,
                "error": stage.get("error_message"),
                "started_at": stage.get("started_at"),
                "finished_at": stage.get("finished_at"),
            }
        )
    clips = [
        {
            "id": clip["id"],
            "candidate_id": clip["candidate_id"],
            "ordinal": clip["ordinal"],
            "status": clip["status"],
            "strategy": clip["strategy"],
            "edl_version": clip["edl_version"],
            "render_id": clip["render_id"],
            "repair_attempts": clip["repair_attempts"],
            "technical_errors": clip["technical_errors"],
            "findings": clip["findings"],
            "review": clip["review"],
            "plan": clip["plan"],
            "history": clip["history"],
        }
        for clip in repos.project_clips.list(run["id"])
    ]
    return {
        "run": run,
        "stages": stage_views,
        "clips": clips,
        "counts": repos.project_clips.counts(run["id"]),
        "manifest": build_manifest(repos, project_id),
    }


def create_run(
    repos: Repositories,
    settings: Settings,
    *,
    project_id: str,
    request: dict,
    provider: dict,
) -> dict:
    existing = repos.project_runs.active_for(project_id)
    if existing is not None:
        return existing
    project = repos.projects.get(project_id)
    profile = repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    snapshot = {
        "project": {
            "id": project["id"],
            "name": project["name"],
            "slug": slugify(project["name"]),
            "profile_id": project.get("profile_id"),
        },
        "profile": profile_snapshot(profile),
        "resolved": resolved["settings"],
        "pipeline": {
            "version": PIPELINE_VERSION,
            "director_version": DIRECTOR_VERSION,
            "repair_limit": settings.autoproduce_repair_limit,
            "stage_timeout_seconds": settings.pipeline_stage_timeout_seconds,
            "deadline_seconds": settings.pipeline_deadline_seconds,
            "render_timeout_seconds": settings.render_timeout_seconds,
        },
        "request": request,
    }
    run = repos.project_runs.create(
        project_id=project_id,
        request=request,
        settings=snapshot,
        provider=provider,
        deadline_seconds=settings.pipeline_deadline_seconds,
    )
    repos.project_stages.ensure(run["id"], STAGES)
    return run
