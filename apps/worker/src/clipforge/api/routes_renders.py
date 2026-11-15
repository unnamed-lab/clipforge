from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from clipforge.api.deps import get_state
from clipforge.api.models import RenderRequest
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.rendering.spec import CaptionSettings, RenderSpec, build_spec
from clipforge.security.limits import validate_span
from clipforge.security.paths import display_name
from clipforge.storage.workspace import absolute_from_relative

router = APIRouter(prefix="/api", tags=["renders"])

MIN_RENDER_MS = 2_000
MAX_RENDER_MS = 15 * 60 * 1000


@router.post("/projects/{project_id}/renders")
def create_render(project_id: str, payload: RenderRequest, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise ValidationFailed("Import a video before rendering", code="source_missing")

    candidate_id = None
    segments: list[dict] = []

    if payload.candidate_id:
        candidate = state.repos.candidates.get(project_id, payload.candidate_id)
        if candidate["decision"] == "rejected":
            raise Conflict("That candidate was rejected. Restore it before rendering.")
        candidate_id = candidate["id"]
        segments = [{"start_ms": candidate["start_ms"], "end_ms": candidate["end_ms"]}]
    elif payload.segments:
        for raw in payload.segments:
            try:
                start, end = validate_span(
                    int(raw.get("start_ms", 0)), int(raw.get("end_ms", 0)), int(source["duration_ms"])
                )
            except (TypeError, ValueError) as exc:
                raise ValidationFailed(f"Invalid segment: {exc}", code="invalid_segment") from exc
            segments.append({"start_ms": start, "end_ms": end})

    if not segments:
        raise ValidationFailed("No segments were provided", code="invalid_segment")

    total = sum(segment["end_ms"] - segment["start_ms"] for segment in segments)
    if total < MIN_RENDER_MS:
        raise ValidationFailed("The planned output is shorter than 2 seconds", code="invalid_segment")
    if total > MAX_RENDER_MS:
        raise ValidationFailed("The planned output is longer than 15 minutes", code="invalid_segment")

    caption_overrides: dict = {}
    if payload.captions is not None:
        caption_overrides = payload.captions.model_dump()

    try:
        spec = build_spec(
            source_id=source["id"],
            segments=segments,
            aspect=payload.aspect,
            resolution=int(payload.resolution),
            framing=payload.framing,
            caption_preset=payload.caption_preset,
            captions_enabled=payload.captions_enabled,
            caption_overrides=caption_overrides,
            fps=payload.fps,
            title=payload.title,
        )
        spec.validate_against(int(source["duration_ms"]))
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="invalid_render_spec") from exc

    render = state.repos.renders.create(
        project_id=project_id,
        job_id=None,
        candidate_id=candidate_id,
        spec=spec.model_dump(),
    )
    job = state.queue.enqueue(
        project_id=project_id,
        job_type="render",
        payload={"render_id": render["id"]},
    )
    state.repos.renders.attach_job(render["id"], job["id"])
    state.repos.projects.touch(project_id)
    return {"render": state.repos.renders.get(render["id"]), "job": job}


@router.get("/projects/{project_id}/renders")
def list_renders(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    return {"renders": state.repos.renders.list_for_project(project_id)}


@router.get("/renders/{render_id}")
def get_render(render_id: str, request: Request) -> dict:
    state = get_state(request)
    render = state.repos.renders.get(render_id)
    return {"render": render}


@router.get("/renders/{render_id}/download")
def download_render(render_id: str, request: Request):
    state = get_state(request)
    raw = state.repos.renders.get_raw(render_id)
    if raw["status"] != "completed" or not raw.get("relative_path"):
        raise NotFound("That render is not ready yet")
    path = absolute_from_relative(state.settings, raw["relative_path"])
    if not path.exists():
        raise NotFound("The rendered file is no longer on disk")

    project = state.repos.projects.get(raw["project_id"])
    spec = RenderSpec.model_validate(json.loads(raw["spec_json"] or "{}"))
    label = spec.title or "clip"
    filename = display_name(f"{project['name']} - {label}", fallback="clip")
    if not filename.lower().endswith(".mp4"):
        filename += ".mp4"
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/renders/{render_id}/stream")
def stream_render(render_id: str, request: Request):
    state = get_state(request)
    raw = state.repos.renders.get_raw(render_id)
    if raw["status"] != "completed" or not raw.get("relative_path"):
        raise NotFound("That render is not ready yet")
    path = absolute_from_relative(state.settings, raw["relative_path"])
    if not path.exists():
        raise NotFound("The rendered file is no longer on disk")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})
