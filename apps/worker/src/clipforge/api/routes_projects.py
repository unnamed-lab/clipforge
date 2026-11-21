from __future__ import annotations

from fastapi import APIRouter, Request

from clipforge.api.deps import get_state
from clipforge.api.models import ProjectCreate, ProjectPatch
from clipforge.briefs.spec import ClippingBrief
from clipforge.db.repositories import serialise_source
from clipforge.discovery.state import build_discovery_state
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.storage.cleanup import delete_project_media
from clipforge.storage.workspace import remove_project_workspace

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("")
def create_project(payload: ProjectCreate, request: Request) -> dict:
    state = get_state(request)
    brief = None
    if payload.brief:
        try:
            brief = ClippingBrief.model_validate(payload.brief).model_dump()
        except ValueError as exc:
            raise ValidationFailed(f"Brief is not valid: {exc}", code="invalid_brief") from exc
    if payload.profile_id:
        profile = state.repos.profiles.get(payload.profile_id)
        if profile["archived"]:
            raise ValidationFailed("That client profile is archived", code="profile_archived")
    project = state.repos.projects.create(
        name=payload.name.strip(), goals=payload.goals, profile_id=payload.profile_id, brief=brief
    )
    return {"project": project}


@router.get("")
def list_projects(request: Request) -> dict:
    state = get_state(request)
    return {"projects": state.repos.projects.list()}


@router.get("/{project_id}")
def get_project(project_id: str, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    transcript = state.repos.transcripts.latest(project_id)
    run = state.repos.runs.latest(project_id)

    transcript_summary = None
    if transcript is not None:
        transcript_summary = {
            "id": transcript["id"],
            "language": transcript["language"],
            "language_probability": transcript["language_probability"],
            "model": transcript["model"],
            "segment_count": len(transcript["segments"]),
            "word_count": sum(len(segment.get("words") or []) for segment in transcript["segments"]),
            "speakers": sorted(
                {
                    str(segment.get("speaker"))
                    for segment in transcript["segments"]
                    if segment.get("speaker")
                }
            ),
            "created_at": transcript["created_at"],
        }

    return {
        "project": project,
        "source": serialise_source(source) if source else None,
        "transcript": transcript_summary,
        "analysis": run,
        "candidates": state.repos.candidates.list_for_project(project_id),
        "renders": state.repos.renders.list_for_project(project_id),
        "jobs": state.repos.jobs.list_for_project(project_id, limit=10),
        "discovery": build_discovery_state(state.repos, project_id, source),
    }


@router.patch("/{project_id}")
def patch_project(project_id: str, payload: ProjectPatch, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.update(project_id, name=payload.name, goals=payload.goals)
    return {"project": project}


@router.delete("/{project_id}")
def delete_project(project_id: str, request: Request) -> dict:
    state = get_state(request)
    if not state.repos.projects.exists(project_id):
        raise NotFound("Project not found")
    active = (
        state.repos.jobs.active_for(project_id, "analysis")
        or state.repos.jobs.active_for(project_id, "discovery")
        or state.repos.jobs.active_for(project_id, "render")
    )
    if active:
        raise Conflict("Stop the running job before deleting this project.")
    delete_project_media(state.storage, state.repos, project_id)
    state.repos.projects.delete(project_id)
    remove_project_workspace(state.settings, project_id)
    return {"deleted": True, "project_id": project_id}
