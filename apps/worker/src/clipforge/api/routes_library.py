from __future__ import annotations

import json
import random

from fastapi import APIRouter, Request

from clipforge.api.deps import get_state
from clipforge.api.models import (
    DatasetBody,
    EvaluationBody,
    LibraryImportBody,
    LibrarySearchBody,
    LibrarySourceBody,
    LibrarySourcePatch,
    RightsBody,
)
from clipforge.db.extensions import PERMISSION_LABELS, RATING_KEYS
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.library.connectors import (
    ConnectorUnavailable,
    check_source,
    connector_report,
    detect_platform,
    search,
    youtube_oembed,
)
from clipforge.security.urls import validate_url_shape
from clipforge.storage.cleanup import delete_project_media
from clipforge.storage.workspace import remove_project_workspace

router = APIRouter(prefix="/api", tags=["library"])

IMPORT_OPERATION_PERMISSION = {
    "import": "import_media",
    "analyse": "transcribe",
    "render": "edit_export",
    "train": "train_model",
}


def _rights_or_default(state, source_id: str) -> dict:
    return state.repos.rights.get(source_id) or {
        "source_id": source_id,
        **{key: "unknown" for key in PERMISSION_LABELS},
        "review_status": "needs_review",
    }


def _require_permission(rights: dict, permission: str, action: str) -> None:
    if rights.get("review_status") == "revoked":
        raise ValidationFailed(
            f"{action} is blocked: the permissions for this source were revoked. "
            "Correct the rights record before using it again.",
            code="permissions_revoked",
        )
    state = rights.get(permission, "unknown")
    if state != "allowed":
        label = PERMISSION_LABELS.get(permission, permission)
        reason = {
            "denied": "The recorded permission denies it.",
            "unknown": "No permission has been recorded for it.",
        }.get(state, "It is not allowed.")
        raise ValidationFailed(
            f"{action} is blocked: '{label}' is {state}. {reason} Update the rights record first.",
            code=f"permission_{permission}_{state}",
        )


@router.get("/library/connectors")
def list_connectors(request: Request) -> dict:
    state = get_state(request)
    return connector_report(state.settings)


@router.post("/library/search")
def search_sources(payload: LibrarySearchBody, request: Request) -> dict:
    state = get_state(request)
    try:
        results = search(state.settings, payload.platform, payload.query.strip(), payload.max_results)
    except ConnectorUnavailable as exc:
        raise ValidationFailed(str(exc), code="connector_unavailable") from exc
    return {"results": results, "platform": payload.platform, "query": payload.query}


@router.get("/library/sources")
def list_sources(request: Request, query: str | None = None, platform: str | None = None,
                 availability: str | None = None) -> dict:
    state = get_state(request)
    sources = state.repos.library.list(platform=platform, availability=availability, query=query)
    items = []
    for source in sources:
        rights = state.repos.rights.get(source["id"])
        items.append({**source, "rights": rights})
    return {"sources": items}


@router.post("/library/sources")
def create_source(payload: LibrarySourceBody, request: Request) -> dict:
    state = get_state(request)
    try:
        validated = validate_url_shape(payload.url)
    except Exception as exc:  # noqa: BLE001 - URL validation raises a user-facing error
        raise ValidationFailed(str(exc), code="invalid_url") from exc
    platform = payload.platform if payload.platform in {"youtube", "twitch"} else detect_platform(validated.original)
    title, creator, thumbnail, metadata_status = payload.title, payload.creator, None, "manual"
    if platform == "youtube":
        info = youtube_oembed(validated.original)
        if info:
            title = title or info.get("title")
            creator = creator or info.get("creator")
            thumbnail = info.get("thumbnail_url")
            metadata_status = "oembed"
    source = state.repos.library.create(
        platform=platform,
        connector=platform if platform in {"youtube", "twitch"} else "manual",
        url=validated.original,
        title=(title or validated.original)[:300],
        creator=creator,
        thumbnail_url=thumbnail,
        metadata_status=metadata_status,
        metadata={"host": validated.host, "note": payload.notes},
    )
    rights = state.repos.rights.upsert(source["id"], {})
    return {"source": source, "rights": rights}


@router.get("/library/sources/{source_id}")
def get_source(source_id: str, request: Request) -> dict:
    state = get_state(request)
    source = state.repos.library.get(source_id)
    return {
        "source": source,
        "rights": _rights_or_default(state, source_id),
        "imports": state.repos.library_imports.list_for_source(source_id),
        "projects": state.repos.projects.find_by_library_source(source_id),
    }


@router.patch("/library/sources/{source_id}")
def patch_source(source_id: str, payload: LibrarySourcePatch, request: Request) -> dict:
    state = get_state(request)
    fields = payload.model_dump(exclude_none=True)
    return {"source": state.repos.library.update(source_id, **fields)}


@router.delete("/library/sources/{source_id}")
def delete_source(source_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.library.delete(source_id)
    return {"deleted": True, "source_id": source_id}


@router.post("/library/sources/{source_id}/check")
def check_availability(source_id: str, request: Request) -> dict:
    state = get_state(request)
    source = state.repos.library.get(source_id)
    try:
        availability, note = check_source(state.settings, source)
    except ConnectorUnavailable as exc:
        raise ValidationFailed(str(exc), code="connector_unavailable") from exc
    updated = state.repos.library.mark_checked(source_id, availability, note)
    return {"source": updated, "availability": availability, "note": note}


@router.get("/library/sources/{source_id}/rights")
def get_rights(source_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.library.get(source_id)
    return {"rights": _rights_or_default(state, source_id)}


@router.put("/library/sources/{source_id}/rights")
def put_rights(source_id: str, payload: RightsBody, request: Request) -> dict:
    state = get_state(request)
    state.repos.library.get(source_id)
    values = payload.model_dump(exclude_none=True)
    rights = state.repos.rights.upsert(source_id, values)
    return {"rights": rights}


@router.post("/library/sources/{source_id}/revoke")
def revoke_rights(source_id: str, request: Request, remove_media: bool = False) -> dict:
    state = get_state(request)
    source = state.repos.library.get(source_id)
    denied = {key: "denied" for key in PERMISSION_LABELS}
    rights = state.repos.rights.upsert(
        source_id,
        {**denied, "review_status": "revoked", "notes": "Permissions revoked by the user."},
    )

    with state.repos.db.session() as conn:
        removed_items = conn.execute(
            "DELETE FROM dataset_items WHERE source_id = ?", (source_id,)
        ).rowcount

    removed_projects = 0
    if remove_media:
        for project in state.repos.projects.find_by_library_source(source_id):
            delete_project_media(state.storage, state.repos, project["id"])
            with state.repos.db.session() as conn:
                conn.execute("DELETE FROM sources WHERE project_id = ?", (project["id"],))
            remove_project_workspace(state.settings, project["id"])
            removed_projects += 1

    state.repos.library.mark_checked(source_id, "revoked", "Permissions revoked; dependent data removed.")
    return {
        "source": source,
        "rights": rights,
        "removed_dataset_items": removed_items,
        "removed_project_media": removed_projects,
        "note": "Media removal only happens when remove_media is true; dataset entries are always removed.",
    }


@router.post("/library/sources/{source_id}/import")
def import_source(source_id: str, payload: LibraryImportBody, request: Request) -> dict:
    state = get_state(request)
    source = state.repos.library.get(source_id)
    rights = _rights_or_default(state, source_id)
    _require_permission(rights, "import_media", "Importing this source")
    if payload.auto_analyse:
        _require_permission(rights, "transcribe", "Transcribing this source")

    profile_id = payload.profile_id
    if profile_id:
        profile = state.repos.profiles.get(profile_id)
        if profile["archived"]:
            raise ValidationFailed("That client profile is archived", code="profile_archived")

    project = state.repos.projects.create(
        name=payload.project_name or source["title"],
        profile_id=profile_id,
        brief=payload.brief,
        library_source_id=source_id,
    )
    job = state.queue.enqueue(
        project_id=project["id"],
        job_type="url_import",
        payload={"url": source["url"], "host": detect_platform(source["url"])},
    )
    state.repos.library_imports.log(
        source_id=source_id, project_id=project["id"], operation="import", status="queued",
        detail="Import queued after a rights check.",
    )
    analysis_job = None
    if payload.auto_analyse:
        analysis_job = state.queue.enqueue(
            project_id=project["id"],
            job_type="analysis",
            payload={"brief": payload.brief} if payload.brief else {},
        )
    return {"project": project, "job": job, "analysis_job": analysis_job, "rights": rights}


@router.post("/projects/{project_id}/evaluations")
def create_evaluation(project_id: str, payload: EvaluationBody, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    ratings = {}
    for key, value in (payload.ratings or {}).items():
        if key not in RATING_KEYS:
            raise ValidationFailed(f"Unknown rating '{key}'", code="invalid_rating")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed(f"Rating '{key}' must be a number from 1 to 5", code="invalid_rating") from exc
        if not 1 <= number <= 5:
            raise ValidationFailed(f"Rating '{key}' must be between 1 and 5", code="invalid_rating")
        ratings[key] = number
    if ratings and "overall" not in ratings:
        ratings["overall"] = round(sum(ratings.values()) / len(ratings))

    run = state.repos.runs.latest(project_id)
    render_spec = {}
    if payload.render_id:
        raw = state.repos.renders.get_raw(payload.render_id)
        if raw["project_id"] != project_id:
            raise NotFound("That render belongs to a different project")
        render_spec = json.loads(raw["spec_json"]) if raw.get("spec_json") else {}
    if payload.candidate_id:
        state.repos.candidates.get(project_id, payload.candidate_id)
    rights = state.repos.rights.get(project.get("library_source_id") or "")
    if rights and rights.get("train_model") != "allowed":
        train_note = "Training permission is not recorded for this source."
    else:
        train_note = None

    evaluation = state.repos.evaluations.create(
        project_id=project_id,
        candidate_id=payload.candidate_id,
        render_id=payload.render_id,
        library_source_id=project.get("library_source_id"),
        decision=payload.decision,
        ratings=ratings,
        brief=project.get("brief") or {},
        model_config=(run or {}).get("settings") or {},
        edit_spec=render_spec,
        rejection_reason=payload.rejection_reason,
        notes=payload.notes,
    )
    return {"evaluation": evaluation, "dataset_note": train_note}


@router.get("/evaluations")
def list_all_evaluations(request: Request) -> dict:
    state = get_state(request)
    return {"evaluations": state.repos.evaluations.list_all()}


@router.get("/datasets")
def list_datasets(request: Request) -> dict:
    state = get_state(request)
    return {"datasets": state.repos.datasets.list()}


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, request: Request) -> dict:
    state = get_state(request)
    return {"dataset": state.repos.datasets.get(dataset_id)}


@router.post("/datasets")
def create_dataset(payload: DatasetBody, request: Request) -> dict:
    state = get_state(request)
    evaluations = state.repos.evaluations.list_all(limit=2000)
    if not evaluations:
        raise ValidationFailed("There are no evaluations to export yet", code="no_evaluations")

    groups: dict[str, list[dict]] = {}
    excluded: list[dict] = []
    for evaluation in evaluations:
        source_id = evaluation.get("library_source_id")
        rights = state.repos.rights.get(source_id) if source_id else None
        allowed = bool(rights and rights.get("train_model") == "allowed")
        if not allowed:
            excluded.append({
                "evaluation_id": evaluation["id"],
                "source_id": source_id,
                "exclusion_reason": (
                    "No library source is linked to this evaluation."
                    if not source_id
                    else "Training permission is not allowed for this source."
                ),
            })
            continue
        groups.setdefault(source_id, []).append(evaluation)

    if not groups:
        raise Conflict(
            "No evaluated source has training permission. Record 'Model training: allowed' "
            "for each source you intend to export."
        )

    source_ids = sorted(groups)
    random.Random(payload.seed).shuffle(source_ids)
    target_train = int(round(len(source_ids) * payload.train_ratio))
    train_ids = set(source_ids[: max(1, target_train)]) if len(source_ids) > 1 else set(source_ids)

    items = [
        {
            "evaluation_id": evaluation["id"],
            "source_id": source_id,
            "split": "train" if source_id in train_ids else "test",
            "included": 1,
        }
        for source_id in source_ids
        for evaluation in groups[source_id]
    ]
    items.extend({
        "evaluation_id": item["evaluation_id"],
        "source_id": item["source_id"],
        "split": "excluded",
        "included": 0,
        "exclusion_reason": item["exclusion_reason"],
    } for item in excluded)

    dataset = state.repos.datasets.create(
        name=payload.name,
        filters={**payload.filters, "train_ratio": payload.train_ratio, "grouped_by": "source"},
        seed=payload.seed,
        items=items,
    )
    return {
        "dataset": dataset,
        "counts": {
            "train": sum(1 for item in dataset["items"] if item["split"] == "train"),
            "test": sum(1 for item in dataset["items"] if item["split"] == "test"),
            "excluded": sum(1 for item in dataset["items"] if item["split"] == "excluded"),
        },
        "note": "Splits are grouped by source so one creator's work cannot appear in both sets.",
    }


@router.post("/datasets/{dataset_id}/freeze")
def freeze_dataset(dataset_id: str, request: Request) -> dict:
    state = get_state(request)
    return {"dataset": state.repos.datasets.freeze(dataset_id)}


@router.delete("/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.datasets.delete(dataset_id)
    return {"deleted": True, "dataset_id": dataset_id}


@router.get("/library/sources/{source_id}/dataset-items")
def dataset_items_for_source(source_id: str, request: Request) -> dict:
    state = get_state(request)
    with state.repos.db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM dataset_items WHERE source_id = ?", (source_id,)
        ).fetchall()
    return {"items": [dict(row) for row in rows]}
