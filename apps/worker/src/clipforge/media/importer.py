"""Source ingestion: local upload and remote URL download."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Callable, Protocol

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.errors import JobFailed, Unsupported, ValidationFailed
from clipforge.media.probe import MediaInfo, probe
from clipforge.security.paths import display_name
from clipforge.security.urls import ValidatedUrl
from clipforge.storage.workspace import ensure_layout, ensure_space

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
CONTAINER_EXTENSIONS = {
    "mp4": "mp4", "mov": "mov", "matroska": "mkv", "webm": "webm", "avi": "avi",
    "mpegts": "ts", "flv": "flv", "mpeg": "mpg", "3gp": "3gp", "ogg": "ogv", "asf": "wmv",
}


class UploadStream(Protocol):
    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


def _extension_for(info: MediaInfo) -> str:
    container = (info.container or "").lower()
    return CONTAINER_EXTENSIONS.get(container, "mp4")


def _validate_duration(settings: Settings, info: MediaInfo) -> None:
    if info.duration_ms <= 0:
        raise Unsupported("That file has no readable duration", code="invalid_duration")
    if info.duration_ms > settings.max_duration_ms:
        raise ValidationFailed(
            f"That video is longer than the {settings.max_duration_ms // 60000} minute limit.",
            code="duration_too_long",
        )


def _replace_existing_source_file(repos: Repositories, project_id: str, keep: Path) -> None:
    existing = repos.sources.find_optional(project_id)
    if not existing:
        return
    old = Path(existing["storage_path"])
    if old.exists() and old.resolve() != keep.resolve():
        try:
            old.unlink()
        except OSError:
            logger.warning("Could not remove replaced source file")


def persist_source(
    repos: Repositories,
    settings: Settings,
    *,
    project_id: str,
    temporary_file: Path,
    kind: str,
    original_name: str,
    source_url: str | None = None,
) -> dict:
    info = probe(temporary_file)
    _validate_duration(settings, info)

    paths = ensure_layout(settings, project_id)
    final_path = paths / "source" / f"{uuid.uuid4().hex}.{_extension_for(info)}"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_file.replace(final_path)

    relative = final_path.relative_to(settings.data_dir.resolve()).as_posix()
    _replace_existing_source_file(repos, project_id, final_path)

    record = repos.sources.upsert(
        project_id=project_id,
        kind=kind,
        original_name=display_name(original_name, fallback="source video"),
        storage_path=str(final_path),
        relative_path=relative,
        size_bytes=info.size_bytes,
        duration_ms=info.duration_ms,
        width=info.width,
        height=info.height,
        fps=info.fps,
        has_audio=info.has_audio,
        video_codec=info.video_codec,
        audio_codec=info.audio_codec,
        container=info.container,
        source_url=source_url,
        probe=info.raw,
    )
    repos.projects.update(project_id, status="imported")
    return record


async def ingest_upload(
    repos: Repositories,
    settings: Settings,
    *,
    project_id: str,
    upload: UploadStream,
) -> dict:
    project = repos.projects.get(project_id)
    paths = ensure_layout(settings, project_id)
    ensure_space(settings, paths, required_bytes=settings.min_free_bytes)

    part = paths / "source" / f"{uuid.uuid4().hex}.part"
    written = 0
    try:
        with part.open("wb") as handle:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise ValidationFailed(
                        f"That file is larger than the {settings.max_upload_bytes // (1024 ** 2)} MB limit.",
                        code="file_too_large",
                    )
                handle.write(chunk)
        if written == 0:
            raise ValidationFailed("That file was empty", code="empty_file")
        return persist_source(
            repos,
            settings,
            project_id=project_id,
            temporary_file=part,
            kind="upload",
            original_name=upload.filename or project["name"],
        )
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _yt_dlp_module():
    try:
        import yt_dlp  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise JobFailed(
            "url_import_unavailable",
            "URL importing needs the yt-dlp package, which is not installed in the worker environment.",
        ) from exc
    return yt_dlp


def download_remote(
    repos: Repositories,
    settings: Settings,
    *,
    project_id: str,
    validated: ValidatedUrl,
    on_progress: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict:
    yt_dlp = _yt_dlp_module()
    paths = ensure_layout(settings, project_id)
    ensure_space(settings, paths, required_bytes=settings.min_free_bytes)
    target_stem = paths / "source" / uuid.uuid4().hex

    base_options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 1,
        "playlist_items": "1",
        "cachedir": str(settings.tmp_dir / "yt-dlp-cache"),
    }

    try:
        with yt_dlp.YoutubeDL(base_options) as ydl:
            info = ydl.extract_info(validated.resolved, download=False)
    except Exception as exc:
        raise JobFailed(
            "url_unsupported",
            "That link could not be read. The site may be unsupported, private, or require a login.",
        ) from exc

    if not info:
        raise JobFailed("url_unsupported", "No video was found at that link.")
    if info.get("_type") == "playlist":
        raise JobFailed("url_playlist", "That link is a playlist. Import one video at a time.")
    if info.get("is_live"):
        raise JobFailed("url_live", "Live streams cannot be imported. Use a recording once it ends.")

    duration_s = info.get("duration")
    if duration_s and float(duration_s) * 1000 > settings.max_duration_ms:
        raise JobFailed(
            "duration_too_long",
            f"That video is longer than the {settings.max_duration_ms // 60000} minute limit.",
        )

    title = str(info.get("title") or "Imported video")

    def progress_hook(status: dict) -> None:
        if should_cancel is not None and should_cancel():
            raise JobFailed("cancelled", "Cancelled")
        if on_progress is None:
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        downloaded = status.get("downloaded_bytes") or 0
        if total:
            on_progress(min(99.0, downloaded / total * 100), f"Downloading {title}")
        else:
            on_progress(50.0, f"Downloading {title}")

    download_options = {
        **{k: v for k, v in base_options.items() if k not in {"skip_download"}},
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(target_stem) + ".%(ext)s",
        "max_filesize": settings.max_upload_bytes,
        "progress_hooks": [progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(download_options) as ydl:
            result = ydl.extract_info(validated.resolved, download=True)
    except JobFailed:
        raise
    except Exception as exc:
        raise JobFailed(
            "url_download_failed",
            "The download failed. The source may be region-locked, DRM-protected, or temporarily unavailable.",
        ) from exc

    filepath = None
    for download in result.get("requested_downloads") or []:
        candidate = download.get("filepath")
        if candidate and Path(candidate).exists():
            filepath = Path(candidate)
            break
    if filepath is None:
        matches = sorted(paths.glob(f"source/{target_stem.name}.*")) if target_stem.parent.exists() else []
        matches = [m for m in matches if m.suffix != ".part"]
        filepath = matches[0] if matches else None
    if filepath is None or not filepath.exists():
        raise JobFailed("url_download_failed", "The download finished but no file was produced.")

    size = filepath.stat().st_size
    if size > settings.max_upload_bytes:
        filepath.unlink(missing_ok=True)
        raise JobFailed("file_too_large", "The downloaded file is larger than the allowed limit.")
    if size < 16 * 1024:
        filepath.unlink(missing_ok=True)
        raise JobFailed("url_download_failed", "The downloaded file is too small to be a video.")

    record = persist_source(
        repos,
        settings,
        project_id=project_id,
        temporary_file=filepath,
        kind="url",
        original_name=title,
        source_url=validated.resolved,
    )
    # Keep a human-readable title when the extractor provides one.
    return record


def cleanup_partials(settings: Settings, project_id: str) -> None:
    try:
        paths = ensure_layout(settings, project_id)
    except Exception:
        return
    for pattern in ("*.part", "*.ytdl", "*.temp"):
        for path in paths.glob(f"source/{pattern}"):
            try:
                os.remove(path)
            except OSError:
                pass


def cancel_guard(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise JobFailed("cancelled", "Cancelled")
