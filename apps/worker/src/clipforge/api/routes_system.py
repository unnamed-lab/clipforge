from __future__ import annotations

import os
import platform
import subprocess
import time
from functools import lru_cache

from fastapi import APIRouter, Request

from clipforge import __version__
from clipforge.api.deps import get_state
from clipforge.media.policy import (
    DEFAULT_QUALITY,
    MAX_PROXY_HEIGHT,
    MIN_PROXY_HEIGHT,
    PROCESSING_MODES,
    QUALITY_LADDER,
)
from clipforge.media.process import which
from clipforge.platforms import platform_summary, safe_area_for
from clipforge.providers.diarisation.base import UnavailableDiarisation
from clipforge.providers.transcription.faster_whisper import MODEL_SIZES_MB, SUPPORTED_MODELS, FasterWhisperTranscription
from clipforge.rendering.encode import CODECS, available_encoders, estimate_table
from clipforge.storage.workspace import free_bytes

router = APIRouter(prefix="/api")
STARTED_AT = time.time()


@lru_cache(maxsize=4)
def _binary_version(program: str) -> str | None:
    resolved = which(program)
    if not resolved:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [resolved, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first_line = (completed.stdout or "").splitlines()
    return first_line[0] if first_line else None


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "uptime_seconds": int(time.time() - STARTED_AT),
    }


@router.get("/platforms")
def platforms() -> dict:
    """Central platform constraints, separate from editorial duration targets."""
    return platform_summary()


@router.get("/platforms/{platform}/safe-area")
def platform_safe_area(platform: str, aspect: str = "9:16") -> dict:
    return {"platform": platform, "aspect": aspect, "caption_safe_area": safe_area_for(platform, aspect)}


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    state = get_state(request)
    settings = state.settings
    provider = FasterWhisperTranscription()
    whisper_available, whisper_detail = provider.available()
    ffmpeg_version = _binary_version("ffmpeg")
    ffprobe_version = _binary_version("ffprobe")
    diarisation = UnavailableDiarisation()

    return {
        "version": __version__,
        "media": {
            "ffmpeg": {"available": bool(ffmpeg_version), "version": ffmpeg_version},
            "ffprobe": {"available": bool(ffprobe_version), "version": ffprobe_version},
            "hwaccel": _hwaccel_summary(),
        },
        "transcription": {
            "provider": provider.name,
            "available": whisper_available and bool(ffmpeg_version),
            "detail": whisper_detail,
            "device": settings.whisper_device,
            "compute_type": settings.whisper_compute_type,
            "default_model": settings.whisper_model,
            "models": [
                {"id": name, "download_mb": MODEL_SIZES_MB.get(name, 0)} for name in SUPPORTED_MODELS
            ],
        },
        "diarisation": {
            "available": diarisation.available,
            "provider": diarisation.name,
            "detail": "Speaker separation is planned but not implemented in this version.",
        },
        "llm": {
            "enabled": settings.llm_enabled,
            "configured": bool(settings.llm_enabled and settings.llm_base_url and settings.llm_model),
            "model": settings.llm_model or None,
            "requires_consent": True,
        },
        "storage": {
            **state.storage.describe(),
            "free_bytes": free_bytes(settings.data_dir),
            "min_free_bytes": settings.min_free_bytes,
            "data_dir": "data",
        },
        "limits": {
            "max_upload_bytes": settings.max_upload_bytes,
            "max_duration_ms": settings.max_duration_ms,
        },
        # What the UI is allowed to offer. Values describe policy, not a promise
        # that a given source publishes that rendition.
        "download": {
            "qualities": [
                {"id": quality_id, "label": label, "height": height or None}
                for quality_id, label, height in QUALITY_LADDER
            ],
            "processing": [
                {
                    "id": mode_id,
                    "label": mode["label"],
                    "default_quality": mode["quality"],
                    "proxy_height": mode["proxy_height"],
                    "scene_fps": mode["scene_fps"],
                    "warnings": mode["warnings"],
                }
                for mode_id, mode in PROCESSING_MODES.items()
            ] + [
                {
                    "id": "custom",
                    "label": "Custom",
                    "default_quality": None,
                    "proxy_height": None,
                    "scene_fps": None,
                    "warnings": ["Custom accepts audio-only downloads and an explicit proxy height."],
                }
            ],
            "default_quality": DEFAULT_QUALITY,
            "proxy_height_range": {"min": MIN_PROXY_HEIGHT, "max": MAX_PROXY_HEIGHT},
            "approximate_sizes": True,
        },
        "compression": {
            "presets": estimate_table(),
            "codecs": [{"id": codec_id, "label": codec["label"]} for codec_id, codec in CODECS.items()],
            "available_encoders": sorted(available_encoders()),
            "gpu_required": False,
        },
        "platforms": platform_summary(),
        "platform": {
            "os": platform.system(),
            "release": platform.release(),
            "cpu_count": os.cpu_count() or 1,
            "python": platform.python_version(),
        },
    }


def _hwaccel_summary() -> dict:
    """Report what FFmpeg can actually use, without promising GPU inference."""
    encoders = _ffmpeg_encoders()
    available = [name for name in ("h264_qsv", "h264_nvenc", "h264_amf") if name in encoders]
    return {"encoders": available, "acceleration_for_transcription": False}


@lru_cache(maxsize=1)
def _ffmpeg_encoders() -> str:
    resolved = which("ffmpeg")
    if not resolved:
        return ""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [resolved, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout or ""
