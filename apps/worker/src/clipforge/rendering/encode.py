"""Compression policy for exports and proxies.

Resolution and compression are independent choices: a 1080p export can still be
encoded at a small file size, and a 720p render can be tuned for quality. The
presets here are intentionally few, and every value is validated before it
reaches FFmpeg. Encoder availability is probed from the installed FFmpeg rather
than assumed, so an unavailable codec falls back with an explicit note instead
of failing halfway through a render.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, ValidationError, model_validator

from clipforge.media.process import which

CompressionPreset = Literal["smallest", "balanced", "higher", "custom"]
CodecId = Literal["h264", "hevc"]

CODECS: dict[str, dict[str, str]] = {
    "h264": {"ffmpeg": "libx264", "label": "H.264", "extension": "mp4"},
    "hevc": {"ffmpeg": "libx265", "label": "H.265", "extension": "mp4"},
}

PRESETS: dict[str, dict] = {
    "smallest": {
        "label": "Smallest file",
        "codec": "h264",
        "crf": 28,
        "audio_bitrate_kbps": 96,
        "resolution_cap": 720,
        "max_fps": 30,
        "x264_preset": "veryfast",
        "note": "Smaller files with softer fine detail. Good for previews and talking-head clips.",
    },
    "balanced": {
        "label": "Balanced",
        "codec": "h264",
        "crf": 23,
        "audio_bitrate_kbps": 128,
        "resolution_cap": 1080,
        "max_fps": 30,
        "x264_preset": "veryfast",
        "note": "The default: small enough to share, sharp enough for captions.",
    },
    "higher": {
        "label": "Higher quality",
        "codec": "h264",
        "crf": 18,
        "audio_bitrate_kbps": 192,
        "resolution_cap": 1920,
        "max_fps": None,
        "x264_preset": "medium",
        "note": "Keeps more detail for slides, charts and on-screen text. Larger files.",
    },
}
PRESET_IDS = (*PRESETS, "custom")
DEFAULT_PRESET = "balanced"

# Bits per pixel per frame at a given CRF. Only used for a labelled estimate;
# FFmpeg decides the real size and the UI never presents this as a promise.
_BPP_BY_CRF: tuple[tuple[int, float], ...] = (
    (0, 0.24), (14, 0.16), (18, 0.11), (20, 0.085), (23, 0.055),
    (26, 0.038), (28, 0.030), (32, 0.018), (40, 0.008), (51, 0.003),
)


class EncodeSettings(BaseModel):
    preset: CompressionPreset = DEFAULT_PRESET
    codec: CodecId = "h264"
    crf: int = Field(default=23, ge=0, le=51)
    audio_bitrate_kbps: int = Field(default=128, ge=64, le=320)
    # The shorter side is capped, which is how "720p" and "1080p" are defined
    # for both landscape and portrait frames. A 16:9 1080p render stays
    # 1920x1080; a 9:16 one stays 1080x1920. The alias keeps specs written
    # before the rename readable.
    resolution_cap: int = Field(
        default=1080,
        ge=320,
        le=3840,
        validation_alias=AliasChoices("resolution_cap", "max_width"),
    )
    max_fps: int | None = Field(default=30, ge=15, le=60)
    x264_preset: Literal["ultrafast", "veryfast", "faster", "fast", "medium", "slow"] = "veryfast"
    container: Literal["mp4"] = "mp4"
    note: str = ""

    @model_validator(mode="after")
    def _codec_settings(self) -> "EncodeSettings":
        if self.codec == "h264" and self.x264_preset == "slow":
            raise ValueError("H.264 does not accept the slow preset here; choose medium or faster")
        return self

    @property
    def ffmpeg_codec(self) -> str:
        return CODECS[self.codec]["ffmpeg"]

    @property
    def audio_bitrate(self) -> str:
        return f"{self.audio_bitrate_kbps}k"

    def dimensions_for(self, dimensions: tuple[int, int]) -> tuple[int, int]:
        """Cap the shorter side to the resolution class, keeping the aspect ratio."""
        width, height = dimensions
        shortest = min(width, height)
        if shortest <= self.resolution_cap:
            return _even(width), _even(height)
        factor = self.resolution_cap / max(1, shortest)
        return _even(width * factor), _even(height * factor)

    def estimate_bytes(self, *, dimensions: tuple[int, int], fps: float, duration_ms: int) -> int:
        width, height = self.dimensions_for(dimensions)
        fps = min(float(self.max_fps or fps or 30), 60.0)
        bpp = bpp_for_crf(self.crf)
        video_bps = width * height * fps * bpp
        audio_bps = self.audio_bitrate_kbps * 1_000
        return int((video_bps + audio_bps) * max(0, duration_ms) / 1000 / 8)

    def to_dict(self) -> dict:
        return self.model_dump()


def _even(value: float) -> int:
    rounded = max(2, int(round(value)))
    return rounded + (rounded % 2)


def bpp_for_crf(crf: int) -> float:
    points = sorted(_BPP_BY_CRF)
    if crf <= points[0][0]:
        return points[0][1]
    if crf >= points[-1][0]:
        return points[-1][1]
    for (low_crf, low_bpp), (high_crf, high_bpp) in zip(points, points[1:]):
        if low_crf <= crf <= high_crf:
            span = max(1, high_crf - low_crf)
            fraction = (crf - low_crf) / span
            return low_bpp + (high_bpp - low_bpp) * fraction
    return 0.055


def normalise_preset(value: str | None) -> str:
    preset = (value or DEFAULT_PRESET).strip().lower()
    aliases = {"small": "smallest", "high": "higher", "best": "higher", "quality": "higher"}
    preset = aliases.get(preset, preset)
    if preset not in PRESET_IDS:
        raise ValueError(f"'{value}' is not a compression preset. Choose one of: {', '.join(PRESET_IDS)}.")
    return preset


def resolve_encode(preset: str | None = None, custom: dict | None = None) -> EncodeSettings:
    """Build validated settings from a preset and optional custom overrides.

    Raises ``ValueError`` with a user-facing sentence for anything the encoder
    would reject, so callers can surface it without leaking pydantic internals.
    """
    resolved = normalise_preset(preset)
    if resolved == "custom":
        payload = {**(custom or {})}
        payload.pop("preset", None)
        try:
            settings = EncodeSettings(preset="custom", **payload)
        except ValidationError as exc:
            raise ValueError(_describe_validation_error(exc)) from exc
        return settings
    values = {key: value for key, value in PRESETS[resolved].items() if key not in {"label", "note"}}
    return EncodeSettings(preset=resolved, **values)


def _describe_validation_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error.get("loc", ())) or "value"
    message = str(error.get("msg", "is not valid")).rstrip(".")
    return f"Compression setting '{field}' is invalid: {message}."


def encoder_candidates(codec: str) -> tuple[str, ...]:
    if codec == "hevc":
        return ("libx265", "hevc_nvenc", "hevc_qsv")
    return ("libx264", "h264_nvenc", "h264_qsv", "h264_amf")


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    resolved = which("ffmpeg")
    if not resolved:
        return frozenset()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [resolved, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names: set[str] = set()
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return frozenset(names)


def encoder_available(codec: str) -> bool:
    encoders = available_encoders()
    if not encoders:
        # No probe result is not proof of absence; FFmpeg itself reports a hard
        # failure if the encoder is missing.
        return True
    return any(candidate in encoders for candidate in encoder_candidates(codec))


def validate_encoders(settings: EncodeSettings) -> tuple[EncodeSettings, str | None]:
    """Return settings that this FFmpeg can run, plus a note when it changed."""
    if encoder_available(settings.codec):
        return settings, None
    if settings.codec != "h264" and encoder_available("h264"):
        fallback = settings.model_copy(update={"codec": "h264"})
        return fallback, (
            f"{CODECS[settings.codec]['label']} is not available in this FFmpeg build; H.264 was used."
        )
    return settings, None


def estimate_table() -> list[dict]:
    """Preset summaries for the capabilities payload."""
    rows = []
    for preset_id, preset in PRESETS.items():
        settings = EncodeSettings(
            preset=preset_id,
            **{key: value for key, value in preset.items() if key not in {"label", "note"}},
        )
        rows.append(
            {
                "id": preset_id,
                "label": preset["label"],
                "codec": settings.codec,
                "crf": settings.crf,
                "audio_bitrate_kbps": settings.audio_bitrate_kbps,
                "resolution_cap": settings.resolution_cap,
                "max_fps": settings.max_fps,
                "note": preset["note"],
            }
        )
    return rows
