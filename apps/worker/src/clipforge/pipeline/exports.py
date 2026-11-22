"""Export identity, human file names and the project manifest.

The database row id is the asset identity. A human filename is metadata used
for the download and the manifest, never a path or a primary key, so renaming a
project or exporting the same edit twice cannot break references.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone

from clipforge.db.repositories import Repositories
from clipforge.db.util import loads as _loads
from clipforge.media.fingerprint import sha256_file
from clipforge.rendering.spec import RenderSpec

PIPELINE_VERSION = 1
STRATEGY_SLUGS = {
    "cold_open": "hook-first",
    "in_medias_res": "in-medias-res",
    "result_first": "result-first",
    "question_answer": "question-answer",
    "story_escalation": "escalation",
    "reaction_led": "reaction-led",
}
ASPECT_SLUGS = {"9:16": "portrait", "16:9": "landscape", "1:1": "square"}


def slugify(value: str, *, fallback: str = "project", limit: int = 48) -> str:
    normalised = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)[:limit].strip("-")
    return cleaned or fallback


def strategy_slug(strategy: str | None) -> str:
    value = (strategy or "chronological").strip().lower()
    return STRATEGY_SLUGS.get(value, slugify(value.replace("_", "-"), fallback="chronological", limit=28))


def aspect_slug(aspect: str) -> str:
    return ASPECT_SLUGS.get(aspect, slugify(aspect, fallback="portrait", limit=12))


def export_filename(
    *,
    project_name: str,
    strategy: str | None,
    aspect: str,
    version: int,
    extension: str = "mp4",
) -> str:
    return (
        f"{slugify(project_name)}__{strategy_slug(strategy)}__{aspect_slug(aspect)}"
        f"__v{max(1, int(version)):03d}.{extension}"
    )


def spec_fingerprint(spec: RenderSpec) -> str:
    """Hash only the decisions that change the rendered bytes."""
    material = {
        "spec_version": spec.version,
        "source_id": spec.source_id,
        "segments": [
            {
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "role": segment.role,
                "composition": segment.composition.model_dump() if segment.composition else None,
            }
            for segment in spec.segments
        ],
        "video": spec.video.model_dump(),
        "encode": spec.encode.model_dump(),
        "enhancement": spec.enhancement.model_dump(),
        "captions": spec.captions.model_dump(),
        "audio": spec.audio.model_dump(),
        "logo": spec.logo.model_dump(),
        "facecam_track": spec.facecam_track.model_dump() if spec.facecam_track else None,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def source_identity(source: dict) -> str:
    if source.get("content_hash"):
        return str(source["content_hash"])
    return "|".join(
        str(source.get(key) or "")
        for key in ("id", "size_bytes", "duration_ms", "width", "height")
    )


def checksum_for(path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _saved_documents(repos: Repositories, render_id: str) -> list[dict]:
    try:
        from clipforge.pipeline.delivery import saved_documents
    except ImportError:  # pragma: no cover - delivery is part of the same package
        return []
    return saved_documents(repos, render_id)


def _review_document(render: dict) -> dict:
    review = render.get("review") or {}
    return {
        "asset_id": render["id"],
        "render_id": render["id"],
        "status": review.get("status") or render.get("review_status") or "unreviewed",
        "summary": review.get("summary") or "",
        "findings": len(review.get("findings") or []),
        "blocking": len(
            [item for item in review.get("findings") or [] if item.get("severity") == "blocking"]
        ),
        "model_review": (review.get("model_review") or {}).get("status") or "not_run",
        "edl_version": render.get("edl_version"),
        "generated_at": render.get("finished_at") or render.get("created_at"),
    }


def export_document(project: dict, render: dict, *, saves: list[dict] | None = None) -> dict:
    spec = render.get("spec") or {}
    editorial = spec.get("editorial") or {}
    strategy = editorial.get("strategy")
    aspect = (spec.get("video") or {}).get("aspect") or "9:16"
    version = int(render.get("version") or 1)
    return {
        "asset_id": render["id"],
        "render_id": render["id"],
        "project_id": render["project_id"],
        "filename": render.get("display_name")
        or export_filename(
            project_name=project.get("name") or "project",
            strategy=strategy,
            aspect=aspect,
            version=version,
        ),
        "version": version,
        "status": render.get("status"),
        "current": bool(render.get("status") == "completed" and not render.get("superseded")),
        "superseded": bool(render.get("superseded")),
        "review_status": render.get("review_status")
        or (render.get("review") or {}).get("status"),
        "strategy": strategy,
        "edl_version": render.get("edl_version") or 1,
        "size_bytes": render.get("size_bytes"),
        "checksum": render.get("checksum"),
        "duration_ms": render.get("duration_ms"),
        "width": render.get("width"),
        "height": render.get("height"),
        "created_at": render.get("created_at"),
        "finished_at": render.get("finished_at"),
        "download_url": render.get("download_url"),
        "review": _review_document(render),
        "saved": saves or [],
    }


def build_manifest(repos: Repositories, project_id: str) -> dict:
    project = repos.projects.get(project_id)
    source = repos.sources.find_optional(project_id)
    analysis = repos.runs.latest(project_id)
    candidates = repos.candidates.count_for_project(project_id)
    renders = repos.renders.list_for_project(project_id)
    runs = repos.project_runs.list_for_project(project_id, limit=5)
    latest_run = runs[0] if runs else None

    exports = [
        export_document(
            project,
            render,
            saves=_saved_documents(repos, render["id"]),
        )
        for render in renders
        if render.get("status") in {"completed", "failed", "cancelled"} or render.get("relative_path")
    ]
    reviews = [_review_document(render) for render in renders if render.get("review")]

    director = {}
    if latest_run:
        director = {
            "mode": (latest_run.get("provider") or {}).get("mode"),
            "provider": (latest_run.get("provider") or {}).get("provider"),
            "model": (latest_run.get("provider") or {}).get("model"),
            "privacy": (latest_run.get("provider") or {}).get("privacy"),
        }

    return {
        "version": 1,
        "project": {
            "id": project["id"],
            "name": project["name"],
            "slug": slugify(project["name"]),
            "status": project.get("status"),
            "created_at": project.get("created_at"),
            "updated_at": project.get("updated_at"),
            "profile_id": project.get("profile_id"),
        },
        "source": (
            {
                "asset_id": source["id"],
                "original_name": source.get("original_name"),
                "size_bytes": source.get("size_bytes"),
                "duration_ms": source.get("duration_ms"),
                "width": source.get("width"),
                "height": source.get("height"),
                "content_hash": source.get("content_hash"),
                "processing": (_loads(source.get("download_json"), {}) or {}).get("processing"),
                "created_at": source.get("created_at"),
            }
            if source
            else None
        ),
        "analysis": (
            {
                "run_id": analysis["id"],
                "status": analysis.get("status"),
                "provider": analysis.get("provider"),
                "created_at": analysis.get("created_at"),
                "candidate_count": candidates,
            }
            if analysis
            else None
        ),
        "production": (
            {
                "run_id": latest_run["id"],
                "status": latest_run["status"],
                "current_stage": latest_run.get("current_stage"),
                "progress": latest_run.get("progress"),
                "director": director,
                "settings": latest_run.get("settings"),
            }
            if latest_run
            else None
        ),
        "exports": exports,
        "reviews": reviews,
        "processing": {
            "pipeline_version": PIPELINE_VERSION,
            "render_spec_version": 4,
            "director_version": 1,
        },
        "provenance": {
            "source_identity": source_identity(source) if source else None,
            "edit_versions": sorted(
                {
                    int(render.get("edl_version") or 1)
                    for render in renders
                    if render.get("status") == "completed"
                }
            ),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
