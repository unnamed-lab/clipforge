from __future__ import annotations

import copy

from fastapi import APIRouter, Request

from clipforge.api.deps import get_state
from clipforge.api.models import BriefBody, CorrectionPatch, EditSpecBody, ProjectSettingsBody
from clipforge.briefs.spec import ClippingBrief
from clipforge.errors import NotFound, ValidationFailed
from clipforge.profiles.spec import (
    DEFAULT_PROFILE,
    ClientProfileDocument,
    normalise_export_values,
    profile_snapshot,
    resolve_settings,
)
from clipforge.rendering.spec import RenderSpec

router = APIRouter(prefix="/api/projects", tags=["project settings"])

OVERRIDE_SECTIONS = ("export", "captions", "audio", "branding", "brief", "rules", "packaging")


def _merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalised_overrides(overrides: dict) -> dict:
    """Repair legacy values before validation and storage.

    A stale client can still send the old ``resolution_mode: "1080"`` shape.
    Repairing it here keeps that save working and leaves the project row clean
    for every later reader.
    """
    cleaned = copy.deepcopy(overrides)
    export = cleaned.get("export")
    if isinstance(export, dict):
        normalise_export_values(export)
    return cleaned


def _validate_overrides(profile: dict | None, overrides: dict) -> None:
    if not isinstance(overrides, dict):
        raise ValidationFailed("Overrides must be an object", code="invalid_overrides")
    unknown = sorted(set(overrides) - set(OVERRIDE_SECTIONS))
    if unknown:
        raise ValidationFailed(
            f"Unknown override sections: {', '.join(unknown)}", code="invalid_overrides"
        )
    base = copy.deepcopy((profile or {}).get("document") or DEFAULT_PROFILE)
    try:
        ClientProfileDocument.model_validate(_merge(base, overrides))
    except ValueError as exc:
        raise ValidationFailed(f"Override is not valid: {exc}", code="invalid_overrides") from exc


@router.get("/{project_id}/settings")
def get_settings(project_id: str, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    return {
        "profile": None if profile is None else {
            "id": profile["id"], "name": profile["name"], "version": profile["version"],
            "archived": profile["archived"],
        },
        "overrides": project.get("overrides") or {},
        "resolved": resolved["settings"],
        "provenance": resolved["provenance"],
        "brief": project.get("brief") or resolved["settings"].get("brief"),
        "profiles": state.repos.profiles.list(),
    }


@router.patch("/{project_id}/settings")
def patch_settings(project_id: str, payload: ProjectSettingsBody, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    profile = None
    if payload.profile_id is not None:
        profile = state.repos.profiles.get(payload.profile_id)
        if profile["archived"]:
            raise ValidationFailed(
                "That client profile is archived. Restore it before assigning it.",
                code="profile_archived",
            )
    elif project.get("profile_id"):
        profile = state.repos.profiles.get(project["profile_id"])

    overrides = project.get("overrides") or {}
    if payload.overrides is not None:
        overrides = _normalised_overrides(payload.overrides)
        _validate_overrides(profile, overrides)

    updated = state.repos.projects.set_extension(
        project_id,
        profile_id=payload.profile_id if payload.profile_id is not None else project.get("profile_id"),
        overrides=overrides,
    )
    resolved = resolve_settings(
        state.repos.profiles.get(updated["profile_id"]) if updated.get("profile_id") else None,
        updated.get("overrides"),
    )
    return {
        "project": updated,
        "resolved": resolved["settings"],
        "provenance": resolved["provenance"],
    }


@router.get("/{project_id}/brief")
def get_brief(project_id: str, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    current = state.repos.briefs.current(project_id)
    return {
        "brief": project.get("brief") or resolved["settings"].get("brief") or {},
        "source": "project" if project.get("brief") else ("profile" if profile else "default"),
        "history": state.repos.briefs.history(project_id),
        "version": current["version"] if current else None,
        "profile_brief": (profile or {}).get("document", {}).get("brief") if profile else None,
    }


@router.put("/{project_id}/brief")
def put_brief(project_id: str, payload: BriefBody, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    try:
        brief = ClippingBrief.model_validate(payload.brief)
    except ValueError as exc:
        raise ValidationFailed(f"Brief is not valid: {exc}", code="invalid_brief") from exc
    saved = state.repos.briefs.save(project_id, brief.model_dump())
    return {"brief": brief.model_dump(), "version": saved["version"]}


@router.get("/{project_id}/corrections")
def list_corrections(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    return {"corrections": state.repos.corrections.list(project_id)}


@router.patch("/{project_id}/corrections/{word_index}")
def correct_word(project_id: str, word_index: int, payload: CorrectionPatch, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    transcript = state.repos.transcripts.latest(project_id)
    if transcript is None:
        raise NotFound("Transcribe this project before correcting words")
    total = sum(len(segment.get("words") or []) for segment in transcript["segments"])
    if word_index < 0 or word_index >= total:
        raise ValidationFailed(
            f"Word index must be between 0 and {max(0, total - 1)}", code="invalid_word_index"
        )
    if payload.text is None and payload.start_ms is None and payload.end_ms is None:
        raise ValidationFailed("Nothing to correct", code="empty_patch")
    record = state.repos.corrections.upsert(
        project_id, word_index, text=payload.text, start_ms=payload.start_ms, end_ms=payload.end_ms
    )
    state.repos.projects.touch(project_id)
    return {"correction": record}


@router.delete("/{project_id}/corrections")
def reset_corrections(project_id: str, request: Request, word_index: int | None = None) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    removed = state.repos.corrections.reset(project_id, word_index)
    return {"removed": removed}


@router.post("/{project_id}/edit-specs")
def save_edit_spec(project_id: str, payload: EditSpecBody, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise ValidationFailed("Import a video before saving an edit", code="source_missing")
    spec = validate_spec(payload.spec, source)
    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    saved = state.repos.edit_specs.create(
        project_id=project_id,
        candidate_id=payload.candidate_id,
        profile_id=project.get("profile_id"),
        spec=spec.model_dump(),
        profile_snapshot=profile_snapshot(profile),
    )
    return {"edit_spec": saved}


@router.get("/{project_id}/edit-specs")
def list_edit_specs(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    return {"edit_specs": state.repos.edit_specs.list_for_project(project_id)}


@router.get("/{project_id}/evaluations")
def list_evaluations(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    return {"evaluations": state.repos.evaluations.list_for_project(project_id)}


def validate_spec(spec_payload: dict, source: dict) -> RenderSpec:
    try:
        spec = RenderSpec.model_validate(spec_payload)
        spec.validate_against(int(source["duration_ms"]))
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="invalid_edit_spec") from exc
    return spec
