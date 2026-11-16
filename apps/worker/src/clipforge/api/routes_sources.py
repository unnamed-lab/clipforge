from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from clipforge.api.deps import get_state
from clipforge.api.models import UrlImportRequest
from clipforge.db.repositories import serialise_source
from clipforge.errors import AppError, NotFound, ValidationFailed
from clipforge.media.importer import ingest_upload
from clipforge.security.urls import validate_url_shape
from clipforge.storage.base import StorageObjectMissing, ensure_local
from clipforge.storage.workspace import ensure_layout

router = APIRouter(prefix="/api/projects", tags=["sources"])

RANGE_CHUNK = 512 * 1024


@router.post("/{project_id}/sources/upload")
async def upload_source(project_id: str, request: Request, file: UploadFile = File(...)) -> dict:
    state = get_state(request)
    record = await ingest_upload(
        state.repos, state.settings, state.storage, project_id=project_id, upload=file
    )
    return {"source": serialise_source(record)}


@router.post("/{project_id}/sources/url")
def import_url(project_id: str, payload: UrlImportRequest, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    if not payload.rights_confirmed:
        raise ValidationFailed(
            "Confirm that you have the rights to reuse this video before importing it.",
            code="rights_not_confirmed",
        )
    validated = validate_url_shape(payload.url)
    active = state.repos.jobs.active_for(project_id, "url_import")
    if active:
        return {"job": active, "deduplicated": True}
    job = state.queue.enqueue(
        project_id=project_id,
        job_type="url_import",
        payload={"url": validated.original, "host": validated.host},
    )
    return {"job": job, "deduplicated": False}


@router.get("/{project_id}/media")
def get_media(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        return {"source": None}
    paths = ensure_layout(state.settings, project_id)
    waveform = paths / "analysis" / "waveform.png"
    return {
        "source": serialise_source(source),
        "waveform": waveform.exists(),
        "stream_url": f"/api/projects/{project_id}/media/stream",
    }


@router.get("/{project_id}/media/stream")
def stream_media(project_id: str, request: Request) -> StreamingResponse:
    state = get_state(request)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise NotFound("This project has no source video")
    try:
        path = ensure_local(
            state.storage,
            source.get("relative_path"),
            legacy_path=source.get("storage_path"),
            missing_message="The source file is missing from this project",
        )
    except StorageObjectMissing as exc:
        raise NotFound(exc.message) from exc
    return _ranged_response(request, path)


@router.get("/{project_id}/media/waveform")
def waveform_image(project_id: str, request: Request):
    state = get_state(request)
    state.repos.projects.get(project_id)
    paths = ensure_layout(state.settings, project_id)
    path = paths / "analysis" / "waveform.png"
    if not path.exists():
        raise NotFound("No waveform has been generated for this project yet")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=300"})


def _ranged_response(request: Request, path: Path) -> StreamingResponse:
    file_size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1
    status_code = 200
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}

    if range_header and range_header.startswith("bytes="):
        spec = range_header.split("=", 1)[1].split(",")[0]
        raw_start, _, raw_end = spec.partition("-")
        try:
            if raw_start.strip():
                start = int(raw_start)
            if raw_end.strip():
                end = int(raw_end)
        except ValueError:
            start, end = 0, file_size - 1
        if start >= file_size:
            return StreamingResponse(
                iter(()),
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        end = min(end, file_size - 1)
        if end < start:
            end = file_size - 1
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    length = end - start + 1
    headers["Content-Length"] = str(length)

    def iterator():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(RANGE_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(iterator(), status_code=status_code, headers=headers, media_type=media_type)


@router.get("/{project_id}/transcript")
def get_transcript(project_id: str, request: Request) -> JSONResponse:
    state = get_state(request)
    state.repos.projects.get(project_id)
    transcript = state.repos.transcripts.latest(project_id)
    if transcript is None:
        raise AppError("This project has not been transcribed yet", code="transcript_missing", status_code=404)
    return JSONResponse(
        {
            "transcript": {
                "id": transcript["id"],
                "language": transcript["language"],
                "language_probability": transcript["language_probability"],
                "model": transcript["model"],
                "duration_ms": transcript["duration_ms"],
                "created_at": transcript["created_at"],
                "segments": transcript["segments"],
            }
        }
    )
