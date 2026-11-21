from __future__ import annotations

from fastapi import APIRouter, Request

from clipforge.api.deps import get_state
from clipforge.api.models import DiscoveryRequest
from clipforge.discovery.criteria import normalise_criteria
from clipforge.discovery.state import build_discovery_state
from clipforge.errors import ValidationFailed

router = APIRouter(prefix="/api", tags=["discovery"])


def _speakers(transcript: dict) -> list[str]:
    return sorted(
        {
            str(segment.get("speaker"))
            for segment in transcript.get("segments") or []
            if segment.get("speaker")
        }
    )


@router.post("/projects/{project_id}/discovery")
def start_discovery(project_id: str, payload: DiscoveryRequest, request: Request) -> dict:
    """Ask for more moments without re-transcribing or re-downloading."""
    state = get_state(request)
    state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise ValidationFailed("Import a video before asking for more moments", code="source_missing")
    if not source["has_audio"]:
        raise ValidationFailed(
            "This source has no audio track, so there is no speech to discover.",
            code="no_audio_stream",
        )
    transcript = state.repos.transcripts.latest(project_id)
    if transcript is None:
        raise ValidationFailed(
            "This project has no saved transcript yet. Run the first analysis first.",
            code="transcript_missing",
        )
    if not state.repos.candidates.list_for_project(project_id):
        raise ValidationFailed(
            "Generate the first set of moments before asking for more.",
            code="analysis_required",
        )

    criteria, warnings = normalise_criteria(
        payload,
        source_duration_ms=int(source["duration_ms"]),
        max_count=state.settings.max_moments_per_request,
        speakers_available=bool(_speakers(transcript)),
    )
    criteria_key = criteria.key()

    active = state.repos.jobs.active_for(project_id, "discovery")
    if active and (active.get("payload") or {}).get("criteria_key") == criteria_key:
        return {
            "job": active,
            "criteria": criteria.model_dump(),
            "criteria_key": criteria_key,
            "warnings": warnings,
            "deduplicated": True,
        }

    job = state.queue.enqueue(
        project_id=project_id,
        job_type="discovery",
        payload={
            "project_id": project_id,
            "criteria": criteria.model_dump(),
            "criteria_key": criteria_key,
        },
    )
    state.repos.projects.update(project_id, status="analysing")
    return {
        "job": job,
        "criteria": criteria.model_dump(),
        "criteria_key": criteria_key,
        "warnings": warnings,
        "deduplicated": False,
    }


@router.get("/projects/{project_id}/discovery")
def get_discovery(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    return build_discovery_state(state.repos, project_id)
