"""Project-level autonomous production endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request

from clipforge.api.deps import get_state
from clipforge.api.models import AutoproduceRequest, ClipApprovalBody, ExportSettingsBody
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.pipeline.delivery import (
    delete_render_assets,
    display_path,
    export_settings_document,
    save_render,
    saved_documents,
    validate_export_dir,
)
from clipforge.pipeline.exports import PIPELINE_VERSION, build_manifest, export_document
from clipforge.pipeline.orchestrator import STAGES, STAGE_LABELS, create_run, pipeline_state
from clipforge.providers.llm.registry import LlmProviderRegistry

router = APIRouter(prefix="/api", tags=["production"])

ACTIVE_JOB_STATES = {"queued", "running", "waiting"}
RETRYABLE_RUN_STATES = {
    "failed", "partially_completed", "awaiting_review", "cancelled", "retrying", "queued",
}


def _run_payload(state, project_id: str, run_id: str) -> dict:
    run = state.repos.project_runs.get(run_id)
    stages = state.repos.project_stages.list(run_id)
    clips = state.repos.project_clips.list(run_id)
    job = None
    if run.get("job_id"):
        try:
            job = state.repos.jobs.get(run["job_id"])
        except NotFound:
            job = None
    return {
        "run": run,
        "stages": [
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
            for stage in stages
        ] or [
            {"name": name, "label": STAGE_LABELS[name], "status": "pending", "progress": 0, "message": ""}
            for name in STAGES
        ],
        "clips": clips,
        "counts": state.repos.project_clips.counts(run_id),
        "job": job,
    }


def _recompute_run_status(state, run_id: str) -> None:
    clips = state.repos.project_clips.list(run_id)
    passed = len([clip for clip in clips if clip["status"] in {"passed", "passed_with_warnings"}])
    needs_review = len([clip for clip in clips if clip["status"] == "needs_review"])
    failed = len([clip for clip in clips if clip["status"] == "failed"])
    if not clips:
        state.repos.project_runs.set_status(
            run_id, "awaiting_review", message="No clips remain in this production run."
        )
    elif needs_review == 0 and passed == len(clips):
        state.repos.project_runs.set_status(
            run_id, "completed", message=f"All {passed} clip(s) passed or were approved."
        )
    elif passed:
        state.repos.project_runs.set_status(
            run_id,
            "partially_completed",
            message=f"{passed} passed, {needs_review} need review, {failed} failed.",
        )
    elif needs_review:
        state.repos.project_runs.set_status(
            run_id, "awaiting_review", message=f"{needs_review} clip(s) need review."
        )
    else:
        state.repos.project_runs.set_status(
            run_id, "failed", message="No clip in this run is valid."
        )


@router.post("/projects/{project_id}/autoproduce")
def start_autoproduce(project_id: str, payload: AutoproduceRequest, request: Request) -> dict:
    state = get_state(request)
    if not state.settings.autoproduce_enabled:
        raise ValidationFailed(
            "Automatic production is disabled on this worker.", code="autoproduce_disabled"
        )
    project = state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise ValidationFailed("Import a video before starting production.", code="source_missing")
    if not source.get("has_video"):
        raise ValidationFailed("This source has no video stream.", code="no_video_stream")
    if int(source.get("duration_ms") or 0) <= 0:
        raise ValidationFailed("The source duration is unknown.", code="invalid_source")

    active = state.repos.project_runs.active_for(project_id)
    if active is not None:
        job = None
        if active.get("job_id"):
            try:
                job = state.repos.jobs.get(active["job_id"])
            except NotFound:
                job = None
        if job and job["state"] in ACTIVE_JOB_STATES:
            return {
                "run": active,
                "job": job,
                "reused": True,
                "message": "A production run is already active for this project.",
            }
        # A crashed worker left an active run without a live job. Resume it from
        # its checkpoints instead of creating a second run over the same work.
        state.repos.project_stages.reset_for_retry(active["id"])
        state.repos.project_runs.set_status(
            active["id"], "queued", message="Resuming the interrupted production run"
        )
        job = state.queue.enqueue(
            project_id=project_id,
            job_type="autoproduce",
            payload={"run_id": active["id"]},
        )
        state.repos.project_runs.attach_job(active["id"], job["id"])
        return {
            "run": state.repos.project_runs.get(active["id"]),
            "job": job,
            "reused": True,
            "message": "Resuming the interrupted production run from its last checkpoint.",
        }

    run_request = payload.model_dump(exclude_unset=True)
    requested_clip_count = int(payload.clip_count)
    run_request["clip_count"] = max(1, min(requested_clip_count, state.settings.autoproduce_clip_limit))
    if payload.platform != "auto" and "aspect" not in run_request:
        run_request["aspect"] = {
            "portrait_short": "9:16",
            "landscape": "16:9",
            "square": "1:1",
        }[payload.platform]
    registry = LlmProviderRegistry(state.settings)
    selection = registry.select()
    provider = {
        **selection.info,
        "available": selection.available,
        "reason": selection.reason,
    }
    run = create_run(
        state.repos,
        state.settings,
        project_id=project_id,
        request=run_request,
        provider=provider,
    )
    job = state.queue.enqueue(
        project_id=project_id,
        job_type="autoproduce",
        payload={"run_id": run["id"]},
    )
    state.repos.project_runs.attach_job(run["id"], job["id"])
    state.repos.projects.touch(project_id)
    return {
        "run": state.repos.project_runs.get(run["id"]),
        "job": job,
        "reused": False,
        "project": project["id"],
    }


@router.get("/projects/{project_id}/pipeline")
def get_pipeline(project_id: str, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    registry = LlmProviderRegistry(state.settings)
    selection = registry.select()
    payload = pipeline_state(state.repos, project_id)
    payload["capabilities"] = {
        "autoproduce_enabled": state.settings.autoproduce_enabled,
        "repair_limit": state.settings.autoproduce_repair_limit,
        "clip_limit": state.settings.autoproduce_clip_limit,
        "pipeline_version": PIPELINE_VERSION,
        "export_settings": export_settings_document(state.settings, project),
        "provider": {
            **selection.info,
            "available": selection.available,
            "reason": selection.reason,
        },
    }
    return payload


@router.get("/pipeline/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    state = get_state(request)
    run = state.repos.project_runs.get(run_id)
    project = state.repos.projects.get(run["project_id"])
    return _run_payload(state, run["project_id"], run_id) | {
        "capabilities": {
            "autoproduce_enabled": state.settings.autoproduce_enabled,
            "repair_limit": state.settings.autoproduce_repair_limit,
            "export_settings": export_settings_document(state.settings, project),
        }
    }


@router.post("/pipeline/{run_id}/cancel")
def cancel_run(run_id: str, request: Request) -> dict:
    state = get_state(request)
    run = state.repos.project_runs.get(run_id)
    updated = state.repos.project_runs.request_cancel(run_id)
    job_id = run.get("job_id")
    if job_id:
        try:
            job = state.repos.jobs.get(job_id)
        except NotFound:
            job = None
        if job and job["state"] in ACTIVE_JOB_STATES:
            state.repos.jobs.request_cancel(job_id)
            state.queue.cancel(job_id)
        if job and job["state"] in {"queued", "waiting"}:
            state.repos.project_runs.set_status(run_id, "cancelled", message="Cancelled by user")
    elif updated["status"] in {"queued", "retrying"}:
        state.repos.project_runs.set_status(run_id, "cancelled", message="Cancelled by user")
    return {"run": state.repos.project_runs.get(run_id)}


@router.post("/pipeline/{run_id}/retry")
def retry_run(run_id: str, request: Request) -> dict:
    state = get_state(request)
    run = state.repos.project_runs.get(run_id)
    if run["status"] not in RETRYABLE_RUN_STATES:
        raise Conflict("This production run is still active.")
    if run.get("job_id"):
        try:
            job = state.repos.jobs.get(run["job_id"])
        except NotFound:
            job = None
        if job and job["state"] in ACTIVE_JOB_STATES:
            raise Conflict("This run's job is still active.")
    # Keep every completed stage. Reset only work that did not finish.
    state.repos.project_stages.reset_for_retry(run_id)
    for clip in state.repos.project_clips.list(run_id):
        plan = clip.get("plan") or {}
        if clip["status"] == "failed" and plan.get("spec"):
            state.repos.project_clips.update(clip["id"], status="configured", render_id=None)
        elif clip["status"] in {"rendering", "repair_planned"}:
            state.repos.project_clips.update(clip["id"], status="configured")
    state.repos.project_runs.set_status(run_id, "queued", message="Queued to resume")
    job = state.queue.enqueue(
        project_id=run["project_id"], job_type="autoproduce", payload={"run_id": run_id}
    )
    state.repos.project_runs.attach_job(run_id, job["id"])
    return {"run": state.repos.project_runs.get(run_id), "job": job}


@router.post("/pipeline/{run_id}/clips/{clip_id}/approve")
def approve_clip(run_id: str, clip_id: str, payload: ClipApprovalBody, request: Request) -> dict:
    state = get_state(request)
    run = state.repos.project_runs.get(run_id)
    clip = state.repos.project_clips.get(clip_id)
    if clip["run_id"] != run_id:
        raise NotFound("That clip belongs to a different run")
    if not clip.get("render_id"):
        raise Conflict("This clip has no completed render to approve.")
    render = state.repos.renders.get(clip["render_id"])
    if render["status"] != "completed":
        raise Conflict("Only a completed render can be approved.")
    gate_status = (render.get("review") or {}).get("gate_status")
    if gate_status == "fail":
        raise Conflict("The technical export gate failed; this render cannot be approved.")
    review = dict(clip.get("review") or {})
    review["approved_by_user"] = bool(payload.approved)
    review["approval_note"] = payload.note
    review["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if payload.approved:
        review["status"] = "warn"
        state.repos.project_clips.update(
            clip_id,
            status="passed_with_warnings",
            review=review,
        )
        state.repos.renders.set_review_status(clip["render_id"], "warn")
    else:
        state.repos.project_clips.update(clip_id, status="needs_review", review=review)
    saved = None
    if payload.approved:
        try:
            saved = save_render(
                state.repos,
                state.settings,
                state.storage,
                state.repos.renders.get(clip["render_id"]),
            )
        except Exception as exc:
            # The approval is valid even if the destination is unwritable; the
            # user keeps the in-app export and can retry the save separately.
            saved = {"error": f"The clip could not be copied to the export folder: {type(exc).__name__}"}
    state.repos.project_clips.append_history(
        clip_id,
        {
            "action": "approved" if payload.approved else "approval_withdrawn",
            "note": payload.note,
            "review_status": render.get("review_status"),
            "saved": saved,
        },
    )
    clips = state.repos.project_clips.list(run_id)
    passed = len([item for item in clips if item["status"] in {"passed", "passed_with_warnings"}])
    needs_review = len([item for item in clips if item["status"] == "needs_review"])
    if needs_review == 0 and passed == len(clips) and clips:
        state.repos.project_runs.set_status(
            run_id, "completed", message="All clips approved by the user."
        )
    elif passed:
        state.repos.project_runs.set_status(
            run_id,
            "partially_completed",
            message=f"{passed} clip(s) approved, {needs_review} still need review.",
        )
    return {
        "clip": state.repos.project_clips.get(clip_id),
        "run": state.repos.project_runs.get(run["id"]),
        "saved": saved,
    }


@router.get("/projects/{project_id}/manifest")
def get_manifest(project_id: str, request: Request) -> dict:
    state = get_state(request)
    return build_manifest(state.repos, project_id)


@router.get("/projects/{project_id}/exports")
def list_exports(project_id: str, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    renders = state.repos.renders.list_for_project(project_id)
    exports = [
        export_document(project, render, saves=saved_documents(state.repos, render["id"]))
        for render in renders
    ]
    return {
        "project_id": project_id,
        "exports": exports,
        "current": [item for item in exports if item["current"]],
        "superseded": [item for item in exports if item["superseded"]],
    }


@router.get("/exports/{render_id}")
def get_export(render_id: str, request: Request) -> dict:
    state = get_state(request)
    render = state.repos.renders.get(render_id)
    project = state.repos.projects.get(render["project_id"])
    return {
        "export": export_document(
            project, render, saves=saved_documents(state.repos, render["id"])
        )
    }


@router.delete("/pipeline/{run_id}/clips/{clip_id}")
def delete_clip(
    run_id: str,
    clip_id: str,
    request: Request,
    delete_exports: bool = False,
) -> dict:
    """Remove one clip from the production list.

    Exports are kept by default: the user asked to clear the list, not to
    destroy a deliverable. ``delete_exports=true`` is the explicit destructive
    choice and is never implied by the UI's normal Remove action.
    """
    state = get_state(request)
    run = state.repos.project_runs.get(run_id)
    if run["status"] in {"queued", "running", "retrying"}:
        raise Conflict("Stop the active production run before removing a clip.")
    clip = state.repos.project_clips.get(clip_id)
    if clip["run_id"] != run_id:
        raise NotFound("That clip belongs to a different run")
    renders = state.repos.renders.list_for_clip(clip_id)
    removed = 0
    if delete_exports:
        for render in renders:
            try:
                delete_render_assets(state.repos, state.storage, render)
                removed += 1
            except Exception:
                continue
    state.repos.project_clips.delete(clip_id)
    _recompute_run_status(state, run_id)
    return {
        "deleted": True,
        "clip_id": clip_id,
        "removed_exports": removed,
        "run": state.repos.project_runs.get(run_id),
        "counts": state.repos.project_clips.counts(run_id),
    }


@router.delete("/exports/{render_id}")
def delete_export(render_id: str, request: Request) -> dict:
    state = get_state(request)
    render = state.repos.renders.get(render_id)
    if render["status"] == "running":
        raise Conflict("That export is still being rendered.")
    affected = state.repos.project_clips.clear_render(render_id)
    result = delete_render_assets(state.repos, state.storage, render)
    for clip in affected:
        _recompute_run_status(state, clip["run_id"])
    return result


@router.post("/exports/{render_id}/save")
def save_export(render_id: str, request: Request) -> dict:
    state = get_state(request)
    render = state.repos.renders.get(render_id)
    saved = save_render(state.repos, state.settings, state.storage, render)
    return {"render_id": render_id, "saved": saved}


@router.post("/projects/{project_id}/exports/save")
def save_current_exports(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    renders = [
        render
        for render in state.repos.renders.list_for_project(project_id)
        if render["status"] == "completed" and not render.get("superseded")
    ]
    saved: list[dict] = []
    errors: list[str] = []
    for render in renders:
        try:
            saved.append({"render_id": render["id"], **save_render(state.repos, state.settings, state.storage, render)})
        except Exception as exc:
            errors.append(f"{render.get('display_name') or render['id']}: {type(exc).__name__}")
    return {
        "saved": saved,
        "errors": errors,
        "export_settings": export_settings_document(
            state.settings, state.repos.projects.get(project_id)
        ),
    }


@router.get("/projects/{project_id}/export-settings")
def get_export_settings(project_id: str, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    return {
        "export_settings": export_settings_document(state.settings, project),
        "saved": saved_documents_for_project(state, project_id),
    }


@router.put("/projects/{project_id}/export-settings")
def put_export_settings(
    project_id: str, payload: ExportSettingsBody, request: Request
) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    resolved = validate_export_dir(payload.export_dir)
    project = state.repos.projects.set_export_dir(
        project_id, str(resolved) if resolved else None
    )
    return {
        "export_settings": export_settings_document(state.settings, project),
        "saved": saved_documents_for_project(state, project_id),
    }


def saved_documents_for_project(state, project_id: str) -> list[dict]:
    rows = state.repos.export_saves.list_for_project(project_id)
    return [
        {
            "render_id": row["render_id"],
            "filename": row["filename"],
            "directory": display_path(Path(row["directory"])),
            "checksum": row.get("checksum"),
            "size_bytes": row.get("size_bytes"),
            "saved_at": row.get("updated_at") or row.get("created_at"),
        }
        for row in rows
    ]
