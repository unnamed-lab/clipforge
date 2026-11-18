"""Render specification: a validated, versioned document.

The same document is stored on a render, shown as a preview summary in the UI, and
turned into FFmpeg arguments by ``ffmpeg.py``. There is exactly one renderer.

Version 2 adds explicit hook segments, audio policy, logo overlays and a profile
snapshot. Version 3 adds per-segment composition decisions and measured caption
geometry. Older documents still load, so old render records remain readable.

The segments are the edit decision list. Each segment stores source-time
coordinates; output-time coordinates are derived from the order of the segments.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from clipforge.editorial.models import NarrativeRole

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_CAPTION_PRESET = "clean"
DEFAULT_CAPTION_POSITION = "bottom"
DEFAULT_CAPTION_ALIGNMENT = "center"
DEFAULT_CAPTION_MARGIN_V = 150

ASPECT_DIMENSIONS: dict[str, dict[int, tuple[int, int]]] = {
    "9:16": {1080: (1080, 1920), 720: (720, 1280)},
    "16:9": {1080: (1920, 1080), 720: (1280, 720)},
    "1:1": {1080: (1080, 1080), 720: (720, 720)},
}

CAPTION_PRESETS: dict[str, dict] = {
    "clean": {
        "label": "Clean podcast",
        "font_family": "Montserrat",
        "font_size": 54,
        "bold": True,
        "weight": "bold",
        "position": "bottom",
        "margin_v": 150,
        "primary_color": "#FFFFFF",
        "highlight_color": "#FFD166",
        "uppercase": False,
        "max_words_per_line": 5,
        "max_lines": 2,
        "background": False,
    },
    "large": {
        "label": "Large highlighted",
        "font_family": "The Bold Font",
        "font_size": 76,
        "bold": True,
        "weight": "black",
        "position": "center",
        "margin_v": 0,
        "primary_color": "#FFFFFF",
        "highlight_color": "#22D3EE",
        "uppercase": True,
        "max_words_per_line": 3,
        "max_lines": 2,
        "background": False,
    },
    "minimal": {
        "label": "Minimal subtitles",
        "font_family": "Inter",
        "font_size": 40,
        "bold": False,
        "weight": "regular",
        "position": "bottom",
        "margin_v": 96,
        "primary_color": "#F5F5F5",
        "highlight_color": "#F5F5F5",
        "uppercase": False,
        "max_words_per_line": 7,
        "max_lines": 2,
        "background": False,
    },
    "branded": {
        "label": "Branded bar",
        "font_family": "Bebas Neue",
        "font_size": 52,
        "bold": True,
        "weight": "bold",
        "position": "bottom",
        "margin_v": 180,
        "primary_color": "#FFFFFF",
        "highlight_color": "#A3E635",
        "uppercase": True,
        "max_words_per_line": 4,
        "max_lines": 2,
        "background": True,
    },
}


class CaptionSettings(BaseModel):
    enabled: bool = True
    burn_in: bool = True
    export_sidecar: bool = False
    preset: Literal["clean", "large", "minimal", "branded", "custom"] = DEFAULT_CAPTION_PRESET
    font_family: str = Field(default="Montserrat", max_length=60)
    font_size: int = Field(default=54, ge=18, le=140)
    bold: bool = True
    weight: Literal["auto", "regular", "medium", "bold", "black"] = "auto"
    position: Literal["bottom", "center", "top"] = DEFAULT_CAPTION_POSITION
    margin_v: int = Field(default=DEFAULT_CAPTION_MARGIN_V, ge=0, le=900)
    margin_h: int = Field(default=64, ge=0, le=360)
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD166"
    outline_color: str = "#000000"
    outline_width: float = Field(default=2.0, ge=0, le=8)
    shadow: float = Field(default=0.0, ge=0, le=8)
    uppercase: bool = False
    max_words_per_line: int = Field(default=5, ge=1, le=12)
    max_lines: int = Field(default=2, ge=1, le=3)
    background: bool = False
    background_opacity: int = Field(default=65, ge=0, le=100)
    alignment: Literal["left", "center", "right"] = DEFAULT_CAPTION_ALIGNMENT
    highlight: Literal["word", "none"] = "word"
    animation: Literal["none", "fade", "pop"] = "none"
    max_chars_per_line: int = Field(default=0, ge=0, le=80)
    reading_speed_cps: int = Field(default=0, ge=0, le=60)
    max_line_width_pct: float = Field(default=0.90, ge=0.40, le=1.0)
    min_font_scale: float = Field(default=0.72, ge=0.40, le=1.0)
    avoid_visual_overlap: bool = True

    @field_validator("primary_color", "highlight_color", "outline_color")
    @classmethod
    def _colour(cls, value: str) -> str:
        if not HEX_COLOR.match(value or ""):
            raise ValueError("Colours must be #RRGGBB")
        return value.upper()

    @field_validator("font_family")
    @classmethod
    def _font(cls, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", value or "").strip()
        return cleaned or "Arial"

    @model_validator(mode="after")
    def _weight(self) -> "CaptionSettings":
        if self.weight != "auto":
            self.bold = self.weight in {"bold", "black"}
        if self.enabled and not self.burn_in and not self.export_sidecar:
            raise ValueError("Captions are enabled but neither burned in nor exported as a sidecar")
        return self


class VideoSettings(BaseModel):
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal[720, 1080] = 1080
    # ``framing`` is the legacy global switch. ``composition`` may be "auto"
    # (scene-aware), or a manual mode. When both are present, composition wins.
    framing: Literal["crop", "blur"] = "crop"
    composition: Literal["auto", "crop", "fit", "pip", "split", "manual"] | None = None
    crop_x_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    crop_y_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    fps: int | None = Field(default=None, ge=15, le=60)

    @property
    def dimensions(self) -> tuple[int, int]:
        return ASPECT_DIMENSIONS[self.aspect][int(self.resolution)]

    @property
    def target_aspect(self) -> float:
        width, height = self.dimensions
        return width / height


class AudioSettings(BaseModel):
    mode: Literal["original", "original_plus_music", "replace"] = "original"
    normalize: bool = False
    denoise: bool = False
    dialogue_enhance: bool = False
    fade_in_ms: int = Field(default=0, ge=0, le=5_000)
    fade_out_ms: int = Field(default=0, ge=0, le=5_000)
    music_asset_id: str | None = None
    music_name: str | None = None
    music_gain_db: float = Field(default=-18.0, ge=-40, le=0)
    duck_music: bool = True

    @model_validator(mode="after")
    def _music(self) -> "AudioSettings":
        if self.mode != "original" and not self.music_asset_id:
            raise ValueError("This audio mode needs an approved music asset")
        return self


class LogoOverlay(BaseModel):
    enabled: bool = False
    required: bool = False
    asset_id: str | None = None
    asset_name: str | None = None
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right", "custom"] = "top-right"
    width_pct: float = Field(default=18.0, ge=3, le=60)
    opacity: float = Field(default=1.0, ge=0.05, le=1.0)
    margin_px: int = Field(default=48, ge=0, le=400)
    custom_x_pct: float = Field(default=82.0, ge=0, le=100)
    custom_y_pct: float = Field(default=8.0, ge=0, le=100)
    from_ms: int | None = Field(default=None, ge=0)
    to_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _asset(self) -> "LogoOverlay":
        if self.enabled and not (self.asset_id and self.asset_name):
            raise ValueError("A logo overlay needs an uploaded asset")
        if self.from_ms is not None and self.to_ms is not None and self.to_ms <= self.from_ms:
            raise ValueError("Logo end must be after its start")
        return self


class Rect(BaseModel):
    """Normalised rectangle in output space (0-1, origin top-left)."""

    x_pct: float = Field(default=0.0, ge=0.0, le=1.0)
    y_pct: float = Field(default=0.0, ge=0.0, le=1.0)
    width_pct: float = Field(default=1.0, gt=0.0, le=1.0)
    height_pct: float = Field(default=1.0, gt=0.0, le=1.0)


class CaptionBand(BaseModel):
    """Vertical band captions may occupy, in output space (0-1)."""

    top_pct: float = Field(default=0.62, ge=0.0, le=1.0)
    bottom_pct: float = Field(default=0.90, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> "CaptionBand":
        if self.bottom_pct <= self.top_pct:
            raise ValueError("Caption band bottom must be below its top")
        return self


class CompositionDecision(BaseModel):
    """How one source scene is mapped into the output frame."""

    mode: Literal["crop", "fit", "pip", "split", "manual"] = "crop"
    background: Literal["blur", "solid", "none"] = "blur"
    background_color: str = "#000000"
    content_type: Literal[
        "talking_head", "slide", "screen_recording", "mixed", "unknown"
    ] = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    auto: bool = True
    crop_x_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    crop_y_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    fit_margin_pct: float = Field(default=0.0, ge=0.0, le=0.20)
    foreground: Rect | None = None
    # Picture-in-picture keeps the whole source visible over a blurred fill.
    pip_scale_pct: float = Field(default=72.0, ge=30.0, le=100.0)
    pip_position: Literal["center", "top", "bottom"] = "center"
    pip_margin_px: int = Field(default=48, ge=0, le=300)
    caption_band: CaptionBand = Field(default_factory=CaptionBand)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("background_color")
    @classmethod
    def _background_colour(cls, value: str) -> str:
        if not HEX_COLOR.match(value or ""):
            raise ValueError("Background colours must be #RRGGBB")
        return value.upper()


class SegmentSpan(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    role: NarrativeRole = "main"
    composition: CompositionDecision | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "SegmentSpan":
        if self.end_ms <= self.start_ms:
            raise ValueError("Segment end must be after its start")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class ProfileRef(BaseModel):
    id: str | None = None
    name: str | None = None
    version: int | None = None


class RenderSpec(BaseModel):
    version: Literal[1, 2, 3] = 3
    source_id: str
    segments: list[SegmentSpan] = Field(min_length=1, max_length=40)
    video: VideoSettings = Field(default_factory=VideoSettings)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    logo: LogoOverlay = Field(default_factory=LogoOverlay)
    title: str | None = None
    hook: dict | None = None
    editorial: dict = Field(default_factory=dict)
    duration: dict = Field(default_factory=dict)
    chronological: bool = False
    profile: ProfileRef = Field(default_factory=ProfileRef)
    profile_snapshot: dict = Field(default_factory=dict)
    edit_spec_id: str | None = None

    @property
    def total_duration_ms(self) -> int:
        return sum(segment.duration_ms for segment in self.segments)

    @property
    def hook_segments(self) -> list[SegmentSpan]:
        return [segment for segment in self.segments if segment.role == "hook"]

    def validate_against(self, source_duration_ms: int) -> None:
        if source_duration_ms <= 0:
            raise ValueError("The source duration is unknown")
        for segment in self.segments:
            if segment.start_ms >= source_duration_ms:
                raise ValueError("A segment starts after the end of the source video")
            if segment.end_ms > source_duration_ms + 250:
                raise ValueError("A segment ends past the end of the source video")
        total = self.total_duration_ms
        if total < 2_000:
            raise ValueError("The planned output is shorter than 2 seconds")
        if total > 15 * 60 * 1000:
            raise ValueError("The planned output is longer than 15 minutes")

    def to_edl(self) -> dict:
        """Versioned edit decision list with source and output coordinates separated."""
        cursor = 0
        segments = []
        for index, segment in enumerate(self.segments):
            composition = segment.composition
            segments.append(
                {
                    "index": index,
                    "role": segment.role,
                    "source_start_ms": segment.start_ms,
                    "source_end_ms": segment.end_ms,
                    "output_start_ms": cursor,
                    "output_end_ms": cursor + segment.duration_ms,
                    "composition": composition.model_dump() if composition else None,
                }
            )
            cursor += segment.duration_ms
        return {
            "version": self.version,
            "source_id": self.source_id,
            "editorial": self.editorial,
            "duration": self.duration,
            "target": {
                "aspect": self.video.aspect,
                "resolution": self.video.resolution,
                "width": self.video.dimensions[0],
                "height": self.video.dimensions[1],
                "fps": self.video.fps,
            },
            "segments": segments,
            "total_output_ms": cursor,
        }

    def summary(self) -> dict:
        modes: dict[str, int] = {}
        for segment in self.segments:
            if segment.composition:
                modes[segment.composition.mode] = modes.get(segment.composition.mode, 0) + 1
        return {
            "version": self.version,
            "aspect": self.video.aspect,
            "resolution": self.video.resolution,
            "dimensions": list(self.video.dimensions),
            "segments": len(self.segments),
            "composition": self.video.composition or self.video.framing,
            "composition_modes": modes,
            "hook": self.hook.get("text") if self.hook else None,
            "editorial": self.editorial.get("strategy") if self.editorial else None,
            "editorial_label": self.editorial.get("label") if self.editorial else None,
            "duration": self.duration or None,
            "captions": self.captions.enabled,
            "captions_burned_in": self.captions.burn_in,
            "captions_sidecar": self.captions.export_sidecar,
            "audio_mode": self.audio.mode,
            "logo": self.logo.enabled,
            "profile": self.profile.model_dump(),
            "total_ms": self.total_duration_ms,
            "estimated": True,
        }


def caption_defaults(preset: str) -> dict:
    return dict(CAPTION_PRESETS.get(preset, CAPTION_PRESETS["clean"]))


def build_spec(
    *,
    source_id: str,
    segments: list[dict],
    aspect: str = "9:16",
    resolution: int = 1080,
    framing: str = "crop",
    composition: str | None = None,
    crop_x_pct: float | None = None,
    crop_y_pct: float | None = None,
    caption_preset: str = DEFAULT_CAPTION_PRESET,
    captions_enabled: bool = True,
    caption_overrides: dict | None = None,
    captions_burn_in: bool = True,
    captions_sidecar: bool = False,
    audio: dict | None = None,
    logo: dict | None = None,
    fps: int | None = None,
    title: str | None = None,
    hook: dict | None = None,
    editorial: dict | None = None,
    duration: dict | None = None,
    chronological: bool = False,
    profile: dict | None = None,
    profile_snapshot: dict | None = None,
    edit_spec_id: str | None = None,
) -> RenderSpec:
    defaults = caption_defaults(caption_preset) if caption_preset != "custom" else {}
    payload = {
        "version": 3,
        "source_id": source_id,
        "segments": segments,
        "video": {
            "aspect": aspect,
            "resolution": resolution,
            "framing": framing,
            "composition": composition,
            "crop_x_pct": crop_x_pct,
            "crop_y_pct": crop_y_pct,
            "fps": fps,
        },
        "captions": {
            "enabled": captions_enabled,
            "burn_in": captions_burn_in,
            "export_sidecar": captions_sidecar,
            "preset": caption_preset,
            **{k: v for k, v in defaults.items() if k != "label"},
            **(caption_overrides or {}),
        },
        "audio": audio or {},
        "logo": logo or {},
        "title": title,
        "hook": hook,
        "editorial": editorial or {},
        "duration": duration or {},
        "chronological": chronological,
        "profile": profile or {},
        "profile_snapshot": profile_snapshot or {},
        "edit_spec_id": edit_spec_id,
    }
    return RenderSpec.model_validate(payload)
