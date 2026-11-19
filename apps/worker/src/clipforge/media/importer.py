"""Source ingestion: local upload and remote URL download.

Two separate concerns are kept apart here:

* the *retained source* is whatever the user selected, copied once into durable
  storage and never silently replaced by a proxy;
* the *working copies* (16 kHz transcription audio, low-resolution analysis
  proxy) are derived from it and recorded as media assets so later stages reuse
  them instead of decoding the whole source again.

Remote downloads are chosen from the formats the extractor actually publishes.
Nothing here bypasses authentication, DRM or a paywall: when a site refuses
access, the user gets a specific explanation.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.errors import JobFailed, Unsupported, ValidationFailed
from clipforge.media.audio import extract_audio
from clipforge.media.fingerprint import sha256_file
from clipforge.media.policy import (
    FormatChoice,
    IngestPlan,
    PolicyError,
    choose_format,
    download_options,
    estimate_display,
    resolve_plan,
)
from clipforge.media.probe import MediaInfo, probe
from clipforge.media.process import ProcessCancelled
from clipforge.media.proxy import build_proxy, proxy_dimensions
from clipforge.security.paths import display_name
from clipforge.security.urls import ValidatedUrl
from clipforge.storage.base import StorageBackend
from clipforge.storage.workspace import ensure_layout, ensure_space

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
DOWNLOAD_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 8.0
SOCKET_TIMEOUT_SECONDS = 30
# A single long-lived connection to a video CDN is routinely throttled to a few
# KB/s. Chunked, parallel fragment requests keep a multi-hundred-MB download
# moving without raising the concurrency beyond a handful of sockets.
HTTP_CHUNK_BYTES = 10 * 1024 * 1024
FRAGMENT_CONNECTIONS = 4
# Below this rate a feature-length download cannot finish in a useful time, so
# the UI says so instead of showing an ETA measured in days.
SLOW_DOWNLOAD_BPS = 25_000
CONTAINER_EXTENSIONS = {
    "mp4": "mp4", "mov": "mov", "matroska": "mkv", "webm": "webm", "avi": "avi",
    "mpegts": "ts", "flv": "flv", "mpeg": "mpg", "3gp": "3gp", "ogg": "ogv", "asf": "wmv",
}

StageProgress = Callable[[str, float | None, str, dict], None]

_PERMANENT_ERRORS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("private video", "is private", "private"), "url_private",
     "That video is private. The owner has not made it available for download."),
    (("age-restricted", "age restricted", "sign in to confirm your age", "inappropriate"),
     "url_age_restricted",
     "That video is age-restricted, so it needs an authenticated session this worker does not have."),
    (("drm",), "url_drm", "That video is DRM-protected. Its streams cannot be downloaded."),
    (("members-only", "members only", "join this channel", "paid"),
     "url_members_only", "That video is only available to channel members."),
    (("login required", "sign in", "cookies", "authentication", "account"),
     "url_login_required", "That site requires a signed-in session before it will serve the video."),
    (("not available in your country", "geo restriction", "geo-restricted", "region"),
     "url_region_locked", "That video is not available from this worker's region."),
    (("unsupported url", "is not a valid url", "no suitable extractor"),
     "url_unsupported", "That link is not a supported video page."),
    (("requested format is not available", "no video formats found"),
     "url_format_unavailable", "The requested rendition is no longer offered by that site."),
    (("video unavailable", "has been removed", "does not exist", "no longer available", "404"),
     "url_unavailable", "That video is unavailable. It may have been removed or made private."),
)
_TRANSIENT_MARKERS = (
    "timed out", "timeout", "connection", "network", "temporary failure", "temporarily unavailable",
    "rate limit", "429", "too many requests", "http error 5", "502", "503", "504", "broken pipe",
    "reset by peer", "remote end closed", "ssl",
)


class UploadStream(Protocol):
    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


def _extension_for(info: MediaInfo, source: Path | None = None) -> str:
    if not info.has_video and source is not None:
        suffix = source.suffix.lower().lstrip(".")
        if suffix in {"m4a", "opus", "webm", "mp3", "ogg", "aac", "wav", "flac"}:
            return suffix
    container = (info.container or "").lower()
    return CONTAINER_EXTENSIONS.get(container, "mp4")


def _validate_duration(settings: Settings, duration_ms: int) -> None:
    if duration_ms <= 0:
        raise Unsupported("That file has no readable duration", code="invalid_duration")
    if duration_ms > settings.max_duration_ms:
        raise ValidationFailed(
            f"That video is longer than the {settings.max_duration_ms // 60000} minute limit.",
            code="duration_too_long",
        )


def _remove_replaced_source(storage: StorageBackend, existing: dict | None, keep_key: str) -> None:
    if not existing:
        return
    old_key = existing.get("relative_path")
    if old_key and old_key != keep_key:
        try:
            storage.delete(old_key)
        except Exception:
            logger.warning("Could not remove the replaced source file from storage")
    elif not old_key:
        old = Path(existing.get("storage_path") or "")
        if old.exists() and old != storage.path_for(keep_key):
            try:
                old.unlink()
            except OSError:
                logger.warning("Could not remove replaced source file")


def _remove_derived_assets(repos: Repositories, storage: StorageBackend, source_id: str) -> None:
    """Drop audio and proxy rows for a source that is being replaced."""
    for role in ("audio", "proxy"):
        row = repos.media.delete(source_id, role)
        if not row:
            continue
        key = row.get("relative_path")
        if key:
            try:
                storage.delete(key)
            except Exception:
                logger.warning("Could not remove the superseded %s asset", role)


def persist_source(
    repos: Repositories,
    settings: Settings,
    storage: StorageBackend,
    *,
    project_id: str,
    temporary_file: Path,
    kind: str,
    original_name: str,
    source_url: str | None = None,
    download: dict | None = None,
    allow_audio_only: bool = False,
) -> dict:
    info = probe(temporary_file, allow_audio_only=allow_audio_only)
    _validate_duration(settings, info.duration_ms)

    ensure_layout(settings, project_id)
    existing = repos.sources.find_optional(project_id)
    # The transcript cache is keyed by these bytes, so hash once at persist time
    # rather than on every analysis run.
    content_hash = sha256_file(temporary_file)
    key = f"projects/{project_id}/source/{uuid.uuid4().hex}.{_extension_for(info, temporary_file)}"
    storage.put_file(key, temporary_file, content_type=mimetypes.guess_type(key)[0] or "video/mp4")
    final_path = storage.path_for(key)

    try:
        record = repos.sources.upsert(
            project_id=project_id,
            kind=kind,
            original_name=display_name(original_name, fallback="source video"),
            storage_path=str(final_path),
            relative_path=key,
            size_bytes=info.size_bytes,
            duration_ms=info.duration_ms,
            width=info.width,
            height=info.height,
            fps=info.fps,
            has_audio=info.has_audio,
            has_video=info.has_video,
            video_codec=info.video_codec,
            audio_codec=info.audio_codec,
            container=info.container,
            source_url=source_url,
            probe=info.raw,
            content_hash=content_hash,
            download=download,
        )
    except Exception:
        try:
            storage.delete(key)
        except Exception:
            logger.warning("Could not remove the source file after a database failure")
        raise
    _remove_replaced_source(storage, existing, key)
    if existing:
        _remove_derived_assets(repos, storage, record["id"])
    repos.projects.update(project_id, status="imported")
    return record


async def ingest_upload(
    repos: Repositories,
    settings: Settings,
    storage: StorageBackend,
    *,
    project_id: str,
    upload: UploadStream,
) -> dict:
    project = repos.projects.get(project_id)
    paths = ensure_layout(settings, project_id)
    declared = getattr(upload, "size", None) or 0
    # Source plus PCM audio, waveform and a render can be several times the source.
    ensure_space(settings, paths, required_bytes=max(64 * 1024 * 1024, declared * 3))

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
            storage,
            project_id=project_id,
            temporary_file=part,
            kind="upload",
            original_name=upload.filename or project["name"],
            allow_audio_only=True,
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


def _base_options(settings: Settings) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 2,
        "http_chunk_size": HTTP_CHUNK_BYTES,
        "concurrent_fragment_downloads": FRAGMENT_CONNECTIONS,
        "playlist_items": "1",
        "cachedir": str(settings.tmp_dir / "yt-dlp-cache"),
    }


def _permanent_reason(message: str) -> tuple[str, str] | None:
    lowered = message.lower()
    for markers, code, explanation in _PERMANENT_ERRORS:
        if any(marker in lowered for marker in markers):
            return code, explanation
    return None


def _map_error(exc: Exception, *, context: str = "url_unsupported") -> JobFailed:
    """Turn an extractor error into a code and a message a user can act on."""
    if isinstance(exc, JobFailed):
        return exc
    message = str(exc)
    permanent = _permanent_reason(message)
    if permanent:
        return JobFailed(permanent[0], permanent[1])
    if context == "url_download_failed":
        if any(marker in message.lower() for marker in ("max_filesize", "max-filesize", "max file size", "larger than")):
            return JobFailed("file_too_large", "That download is larger than the allowed limit.")
        return JobFailed(
            "url_download_failed",
            "The download stopped before it finished. The source may be temporary unavailable; retry it.",
        )
    return JobFailed(
        "url_unsupported",
        "That link could not be read. The site may be unsupported, private, or require a login.",
    )


def _is_transient(exc: Exception) -> bool:
    message = str(exc).lower()
    if _permanent_reason(message):
        return False
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def _validate_info(settings: Settings, info: dict) -> int:
    if not info:
        raise JobFailed("url_unsupported", "No video was found at that link.")
    if info.get("_type") == "playlist":
        raise JobFailed("url_playlist", "That link is a playlist. Import one video at a time.")
    if info.get("is_live"):
        raise JobFailed("url_live", "Live streams cannot be imported. Use a recording once it ends.")
    duration_s = info.get("duration")
    try:
        duration_ms = int(round(float(duration_s) * 1000)) if duration_s else 0
    except (TypeError, ValueError):
        duration_ms = 0
    if duration_ms <= 0:
        raise JobFailed("invalid_duration", "That video's duration could not be read, so it cannot be planned.")
    _validate_duration(settings, duration_ms)
    return duration_ms


def _extract_info(yt_dlp, settings: Settings, url: str, *, should_cancel=None) -> dict:
    if should_cancel is not None and should_cancel():
        raise JobFailed("cancelled", "Cancelled")
    options = {**_base_options(settings)}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except JobFailed:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc
    if not info:
        raise JobFailed("url_unsupported", "No video was found at that link.")
    # Playlist entries are returned as a container even with noplaylist.
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if len(entries) == 1 and entries[0]:
            info = entries[0]
    return info


def inspect_remote(
    *,
    settings: Settings,
    validated: ValidatedUrl,
    quality: str | None = None,
    processing: str = "balanced",
    proxy_height: int | None = None,
    should_cancel=None,
) -> dict:
    """Read the published formats and describe exactly what a download would fetch."""
    yt_dlp = _yt_dlp_module()
    info = _extract_info(yt_dlp, settings, validated.resolved, should_cancel=should_cancel)
    duration_ms = _validate_info(settings, info)

    try:
        plan = resolve_plan(mode=processing, quality=quality, proxy_height=proxy_height)
        choice = choose_format(info, plan.quality)
    except PolicyError as exc:
        raise ValidationFailed(exc.message, code=exc.code) from exc
    # Re-resolve now that the chosen rendition's height is known, so the proxy
    # decision reflects the real download rather than the requested ceiling.
    plan = resolve_plan(
        mode=processing, quality=quality, proxy_height=proxy_height, source_height=choice.height
    )

    height = choice.height
    proxy = proxy_dimensions(choice.width, choice.height, plan.proxy_height) if (height and plan.proxy_height) else None
    return {
        "url": validated.resolved,
        "host": validated.host,
        "title": str(info.get("title") or "Imported video"),
        "uploader": info.get("uploader"),
        "duration_ms": duration_ms,
        "thumbnail": info.get("thumbnail"),
        "qualities": download_options(info),
        "selection": estimate_display(choice),
        "plan": {**plan.to_dict(), "proxy_dimensions": list(proxy) if proxy else None},
        "warnings": list(dict.fromkeys([*choice.warnings, *plan.warnings])),
        "approximate_sizes": True,
    }


def _stored_download(existing: dict) -> dict:
    """The download summary as persisted, whichever shape the row came back in."""
    download = existing.get("download")
    if isinstance(download, dict) and download:
        return download
    raw = existing.get("download_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _existing_reusable_source(
    repos: Repositories,
    storage: StorageBackend,
    project_id: str,
    *,
    url: str,
    choice: FormatChoice,
) -> dict | None:
    existing = repos.sources.find_optional(project_id)
    if not existing or existing.get("kind") != "url" or existing.get("source_url") != url:
        return None
    download = _stored_download(existing)
    if download.get("selector") != choice.selector:
        return None
    if bool(existing.get("has_video", True)) != bool(choice.has_video):
        return None
    key = existing.get("relative_path")
    if not key:
        return None
    path = storage.path_for(key)
    if path.exists() or storage.exists(key):
        return existing
    return None


def _clean_target_files(stem: Path) -> None:
    parent = stem.parent
    if not parent.exists():
        return
    for path in parent.glob(f"{stem.name}*"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove a partial download file")


def _sleep(seconds: float, should_cancel) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_cancel is not None and should_cancel():
            raise JobFailed("cancelled", "Cancelled")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _download(yt_dlp, settings: Settings, url: str, options: dict, *, should_cancel) -> dict:
    """Download with bounded backoff. Permanent failures are not retried."""
    last: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        if should_cancel is not None and should_cancel():
            raise JobFailed("cancelled", "Cancelled")
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=True)
        except JobFailed:
            raise
        except Exception as exc:
            mapped = _map_error(exc, context="url_download_failed")
            if not _is_transient(exc):
                raise mapped from exc
            last = mapped
        if attempt < DOWNLOAD_ATTEMPTS:
            delay = min(RETRY_BASE_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_SECONDS)
            logger.info("Download attempt %s failed; retrying in %.1fs", attempt, delay)
            _sleep(delay, should_cancel)
    raise JobFailed(
        "url_download_failed",
        "The download failed after three attempts. The connection or the site was unstable; retry later.",
    ) from last


def _download_summary(
    choice: FormatChoice,
    plan: IngestPlan,
    *,
    size_bytes: int,
    elapsed_seconds: float,
    bytes_downloaded: int,
) -> dict:
    speed = bytes_downloaded / elapsed_seconds if elapsed_seconds > 0 else None
    return {
        "quality": choice.quality,
        "requested_quality": choice.requested_quality,
        "selector": choice.selector,
        "width": choice.width,
        "height": choice.height,
        "fps": choice.fps,
        "video_codec": choice.video_codec,
        "audio_codec": choice.audio_codec,
        "container": choice.container,
        "has_video": choice.has_video,
        "estimated_bytes": choice.estimated_bytes,
        "approximate": True,
        "bytes_downloaded": bytes_downloaded,
        "size_bytes": size_bytes,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "speed_bps": int(speed) if speed else None,
        "explanation": choice.explanation,
        "warnings": choice.warnings,
        "processing": plan.to_dict(),
        "video_format_id": choice.video.format_id if choice.video else None,
        "audio_format_id": choice.audio.format_id if choice.audio else None,
    }


def progress_band(stage: str, percent: float | None) -> float:
    low, high = {
        "inspecting": (1.0, 5.0),
        "selecting_format": (5.0, 7.0),
        "downloading_video": (7.0, 72.0),
        "downloading_audio": (7.0, 72.0),
        "verifying": (72.0, 76.0),
        "preparing_audio": (76.0, 86.0),
        "preparing_proxy": (86.0, 98.0),
    }.get(stage, (0.0, 100.0))
    if percent is None:
        return low
    return low + (high - low) * max(0.0, min(100.0, percent)) / 100.0


def _make_audio_asset(
    repos: Repositories,
    storage: StorageBackend,
    *,
    project_id: str,
    source: dict,
    source_path: Path,
    duration_ms: int,
    cancel_event,
    report: StageProgress | None,
) -> dict | None:
    if not source["has_audio"]:
        return None
    key = f"projects/{project_id}/audio/audio.wav"
    destination = storage.path_for(key)
    existing = repos.media.find(source["id"], "audio")
    if existing and existing.get("relative_path") == key and destination.exists():
        # Extraction is the expensive half of ingestion; a retry must not redo it.
        return existing
    if report:
        report("preparing_audio", None, "Extracting audio for speech recognition", {"indeterminate": True})
    try:
        extract_audio(
            source_path,
            destination,
            cancel=cancel_event,
            duration_ms=duration_ms,
            on_progress=(
                (lambda percent: report("preparing_audio", percent, "Extracting audio for speech recognition", {}))
                if report else None
            ),
        )
    except ProcessCancelled:
        destination.unlink(missing_ok=True)
        raise JobFailed("cancelled", "Cancelled") from None
    except Exception:
        # A cancelled or failed decode leaves an unreferenced partial behind.
        destination.unlink(missing_ok=True)
        raise
    size = destination.stat().st_size if destination.exists() else 0
    if size <= 1024:
        destination.unlink(missing_ok=True)
        return None
    storage.put_file(key, destination, content_type="audio/wav")
    return repos.media.upsert(
        project_id=project_id,
        source_id=source["id"],
        role="audio",
        storage_path=str(storage.path_for(key)),
        relative_path=key,
        size_bytes=size,
        duration_ms=duration_ms,
        width=None,
        height=None,
        fps=None,
        has_audio=True,
        video_codec=None,
        audio_codec="pcm_s16le",
        container="wav",
        settings={"sample_rate": 16_000, "channels": 1, "purpose": "speech recognition and waveform"},
        source_identity=source.get("content_hash") or source["relative_path"],
    )


def _make_proxy_asset(
    repos: Repositories,
    storage: StorageBackend,
    *,
    project_id: str,
    source: dict,
    source_path: Path,
    plan: IngestPlan,
    cancel_event,
    report: StageProgress | None,
) -> dict | None:
    if not plan.download_proxy or not plan.proxy_height:
        return None
    if not source.get("has_video"):
        return None
    dimensions = proxy_dimensions(source.get("width"), source.get("height"), plan.proxy_height)
    if dimensions is None:
        return None
    width, height = dimensions
    key = f"projects/{project_id}/proxy/{height}p.mp4"
    destination = storage.path_for(key)
    existing = repos.media.find(source["id"], "proxy")
    if existing and existing.get("relative_path") == key and existing.get("width") == width \
            and existing.get("height") == height and destination.exists():
        return existing
    if report:
        report("preparing_proxy", None, f"Preparing a {height}p analysis proxy", {"indeterminate": True})
    try:
        result = build_proxy(
            source_path,
            destination,
            dimensions=dimensions,
            duration_ms=int(source["duration_ms"]),
            cancel=cancel_event,
            on_progress=(
                (lambda percent: report("preparing_proxy", percent, f"Preparing a {height}p analysis proxy", {}))
                if report else None
            ),
        )
    except ProcessCancelled:
        # The user asked to stop: that is not a proxy problem to tolerate, and
        # the job must not be reported as finished.
        destination.unlink(missing_ok=True)
        raise JobFailed("cancelled", "Cancelled") from None
    except Exception as exc:
        # A proxy is an optimisation. Analysis can fall back to the retained
        # source, so a failed proxy must not fail the import.
        destination.unlink(missing_ok=True)
        logger.warning("Proxy generation failed; analysis will use the source: %s", exc)
        if report:
            report("preparing_proxy", 100, "Proxy unavailable; analysis will use the original", {"failed": True})
        return None
    storage.put_file(key, destination, content_type="video/mp4")
    return repos.media.upsert(
        project_id=project_id,
        source_id=source["id"],
        role="proxy",
        storage_path=str(storage.path_for(key)),
        relative_path=key,
        size_bytes=result["size_bytes"],
        duration_ms=int(source["duration_ms"]),
        width=width,
        height=height,
        fps=source.get("fps"),
        has_audio=False,
        video_codec="h264",
        audio_codec=None,
        container="mp4",
        settings=result,
        source_identity=source.get("content_hash") or source["relative_path"],
    )


def download_remote(
    repos: Repositories,
    settings: Settings,
    storage: StorageBackend,
    *,
    project_id: str,
    validated: ValidatedUrl,
    quality: str | None = None,
    processing: str = "balanced",
    proxy_height: int | None = None,
    on_progress: StageProgress | None = None,
    should_cancel: Callable[[], bool] | None = None,
    cancel_event=None,
) -> tuple[dict, dict]:
    """Fetch one remote source. Returns the stored source and measured metrics."""
    yt_dlp = _yt_dlp_module()
    paths = ensure_layout(settings, project_id)
    # Remote size is unknown until the formats are read; reserve a sane budget.
    ensure_space(settings, paths, required_bytes=512 * 1024 * 1024)
    cleanup_partials(settings, project_id)

    def report(stage: str, percent: float | None, message: str, detail: dict) -> None:
        if on_progress is None:
            return
        on_progress(stage, percent, message, detail)

    report("inspecting", None, "Inspecting the source and its published formats", {"indeterminate": True})
    info = _extract_info(yt_dlp, settings, validated.resolved, should_cancel=should_cancel)
    _validate_info(settings, info)

    try:
        plan = resolve_plan(mode=processing, quality=quality, proxy_height=proxy_height)
        choice = choose_format(info, plan.quality)
    except PolicyError as exc:
        raise JobFailed(exc.code, exc.message) from exc
    plan = resolve_plan(
        mode=processing, quality=quality, proxy_height=proxy_height, source_height=choice.height
    )
    report(
        "selecting_format",
        100,
        choice.explanation,
        {
            "quality": choice.quality,
            "requested_quality": choice.requested_quality,
            "height": choice.height,
            "estimated_bytes": choice.estimated_bytes,
            "approximate": True,
        },
    )

    reusable = _existing_reusable_source(repos, storage, project_id, url=validated.resolved, choice=choice)
    if reusable is not None:
        report("verifying", 100, "Reusing the source already stored for this project", {"reused": True})
        summary = _download_summary(choice, plan, size_bytes=int(reusable["size_bytes"]),
                                    elapsed_seconds=0.0, bytes_downloaded=0)
        summary["reused"] = True
        # Reuse must land in the same state as a fresh import: if an earlier run
        # stopped before the audio or proxy existed, finish that work now.
        reuse_plan = resolve_plan(
            mode=processing,
            quality=quality,
            proxy_height=proxy_height,
            source_height=reusable.get("height"),
        )
        audio_asset, proxy_asset = _ensure_derived_assets(
            repos,
            storage,
            project_id=project_id,
            record=reusable,
            plan=reuse_plan,
            cancel_event=cancel_event,
            report=report,
        )
        return reusable, _ingest_metrics(
            summary, audio_asset, proxy_asset, reuse_plan, choice, reused=True
        )

    title = str(info.get("title") or "Imported video")
    target_stem = paths / "source" / uuid.uuid4().hex
    audio_format_id = choice.audio.format_id if choice.audio else None
    measured: dict[str, Any] = {"download": {}, "bytes_downloaded": 0, "eta_seconds": None}

    def progress_hook(status: dict) -> None:
        if should_cancel is not None and should_cancel():
            raise JobFailed("cancelled", "Cancelled")
        detail = status.get("info_dict") or {}
        format_id = str(detail.get("format_id") or "")
        stage = "downloading_audio" if (audio_format_id and format_id == audio_format_id) else "downloading_video"
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        downloaded = int(status.get("downloaded_bytes") or 0)
        if downloaded:
            measured["bytes_downloaded"] = max(measured["bytes_downloaded"], downloaded)
        if status.get("status") == "finished":
            report(stage, 100, f"Finished {stage.replace('downloading_', '')} for {title}", {})
            return
        percent = (min(100.0, downloaded / total * 100) if total else None)
        speed = status.get("speed")
        eta = status.get("eta")
        elapsed = status.get("elapsed") or 0
        measured["eta_seconds"] = eta
        label = "audio" if stage == "downloading_audio" else "video"
        if total:
            message = f"Downloading {label} {downloaded / (1024 ** 2):.1f} of {total / (1024 ** 2):.1f} MB"
        else:
            message = f"Downloading {label}"
        # A 27-hour ETA is not progress, it is a stall. Say that plainly rather
        # than letting the reader believe the download is merely slow.
        slow = bool(
            speed and speed < SLOW_DOWNLOAD_BPS and elapsed > 20 and downloaded > 100_000
        )
        if slow:
            message = f"{message} — the connection is very slow ({speed / 1024:.0f} KB/s)"
        report(stage, percent, message, {
            "stage": stage,
            "downloaded_bytes": downloaded,
            "total_bytes": int(total) if total else None,
            "speed_bps": int(speed) if speed else None,
            "eta_seconds": int(eta) if eta else None,
            "format_id": format_id or None,
            "indeterminate": total is None,
            "slow": slow,
        })

    download_options_payload = {
        **{k: v for k, v in _base_options(settings).items()},
        "format": choice.selector,
        "merge_output_format": choice.container if choice.container in {"mp4", "mkv", "webm"} else "mp4",
        "outtmpl": str(target_stem) + ".%(ext)s",
        "max_filesize": settings.max_upload_bytes,
        "continuedl": True,
        "nopart": False,
        "progress_hooks": [progress_hook],
    }

    started = time.monotonic()
    try:
        result = _download(yt_dlp, settings, validated.resolved, download_options_payload,
                           should_cancel=should_cancel)
    except JobFailed:
        _clean_target_files(target_stem)
        cleanup_partials(settings, project_id)
        raise
    except Exception as exc:
        _clean_target_files(target_stem)
        cleanup_partials(settings, project_id)
        raise _map_error(exc, context="url_download_failed") from exc

    filepath = None
    for download in result.get("requested_downloads") or []:
        candidate = download.get("filepath")
        if candidate and Path(candidate).exists():
            filepath = Path(candidate)
            break
    if filepath is None:
        matches = sorted(paths.glob(f"source/{target_stem.name}.*")) if target_stem.parent.exists() else []
        matches = [match for match in matches if match.suffix not in {".part", ".ytdl"}]
        filepath = matches[0] if matches else None
    if filepath is None or not filepath.exists():
        _clean_target_files(target_stem)
        raise JobFailed("url_download_failed", "The download finished but no file was produced.")

    size = filepath.stat().st_size
    if size > settings.max_upload_bytes:
        _clean_target_files(target_stem)
        raise JobFailed("file_too_large", "The downloaded file is larger than the allowed limit.")
    if size < 16 * 1024:
        _clean_target_files(target_stem)
        raise JobFailed("url_download_failed", "The downloaded file is too small to be a video.")

    elapsed = time.monotonic() - started
    summary = _download_summary(choice, plan, size_bytes=size, elapsed_seconds=elapsed,
                                bytes_downloaded=measured["bytes_downloaded"] or size)
    report("verifying", 50, "Verifying the downloaded file", {})
    try:
        record = persist_source(
            repos,
            settings,
            storage,
            project_id=project_id,
            temporary_file=filepath,
            kind="url",
            original_name=title,
            source_url=validated.resolved,
            download=summary,
            allow_audio_only=True,
        )
    finally:
        _clean_target_files(target_stem)
        cleanup_partials(settings, project_id)

    audio_asset, proxy_asset = _ensure_derived_assets(
        repos,
        storage,
        project_id=project_id,
        record=record,
        plan=plan,
        cancel_event=cancel_event,
        report=report,
    )

    report("verifying", 100, "Download complete", {})
    return record, _ingest_metrics(summary, audio_asset, proxy_asset, plan, choice, reused=False)


def _ensure_derived_assets(
    repos: Repositories,
    storage: StorageBackend,
    *,
    project_id: str,
    record: dict,
    plan: IngestPlan,
    cancel_event,
    report: StageProgress | None,
) -> tuple[dict | None, dict | None]:
    """Materialise the audio and proxy working copies if they are missing."""
    source_path = storage.path_for(record["relative_path"])
    audio_asset = _make_audio_asset(
        repos,
        storage,
        project_id=project_id,
        source=record,
        source_path=source_path,
        duration_ms=int(record["duration_ms"]),
        cancel_event=cancel_event,
        report=report,
    )
    proxy_asset = _make_proxy_asset(
        repos,
        storage,
        project_id=project_id,
        source=record,
        source_path=source_path,
        plan=plan,
        cancel_event=cancel_event,
        report=report,
    )
    return audio_asset, proxy_asset


def _ingest_metrics(
    summary: dict,
    audio_asset: dict | None,
    proxy_asset: dict | None,
    plan: IngestPlan,
    choice: FormatChoice,
    *,
    reused: bool,
) -> dict:
    return {
        "download": summary,
        "audio": {
            "present": bool(audio_asset),
            "size_bytes": audio_asset["size_bytes"] if audio_asset else 0,
        },
        "proxy": (
            {
                "present": True,
                "width": proxy_asset["width"],
                "height": proxy_asset["height"],
                "size_bytes": proxy_asset["size_bytes"],
                "settings": proxy_asset["settings"],
            }
            if proxy_asset else {"present": False, "reason": "not required for this source"}
        ),
        "plan": plan.to_dict(),
        "warnings": list(dict.fromkeys([*choice.warnings, *plan.warnings])),
        "reused": reused,
    }


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
