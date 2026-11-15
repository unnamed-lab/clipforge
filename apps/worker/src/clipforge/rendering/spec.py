"""Render specification: a validated, versioned document.

The same document is stored on a render, shown as a preview summary in the UI, and
turned into FFmpeg arguments by ``ffmpeg.py``. There is exactly one renderer.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

ASPECT_DIMENSIONS: dict[str, dict[int, tuple[int, int]]] = {
    "9:16": {1080: (1080, 1920), 720: (720, 1280)},
    "16:9": {1080: (1920, 1080), 720: (1280, 720)},
    "1:1": {1080: (1080, 1080), 720: (720, 720)},
}

CAPTION_PRESETS: dict[str, dict] = {
    "clean": {
        "label": "Clean podcast",
        "font_family": "Arial",
        "font_size": 54,
        "bold": True,
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
        "font_family": "Arial",
        "font_size": 76,
        "bold": True,
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
        "font_family": "Arial",
        "font_size": 40,
        "bold": False,
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
        "font_family": "Arial",
        "font_size": 52,
        "bold": True,
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
    preset: Literal["clean", "large", "minimal", "branded", "custom"] = "clean"
    font_family: str = Field(default="Arial", max_length=60)
    font_size: int = Field(default=54, ge=18, le=140)
    bold: bool = True
    position: Literal["bottom", "center", "top"] = "bottom"
    margin_v: int = Field(default=150, ge=0, le=900)
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD166"
    outline_color: str = "#000000"
    outline_width: float = Field(default=2.0, ge=0, le=8)
    uppercase: bool = False
    max_words_per_line: int = Field(default=5, ge=1, le=12)
    max_lines: int = Field(default=2, ge=1, le=3)
    background: bool = False

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


class VideoSettings(BaseModel):
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal[720, 1080] = 1080
    framing: Literal["crop", "blur"] = "crop"
    fps: int | None = Field(default=None, ge=15, le=60)

    @property
    def dimensions(self) -> tuple[int, int]:
        return ASPECT_DIMENSIONS[self.aspect][int(self.resolution)]


class AudioSettings(BaseModel):
    normalize: bool = False


class SegmentSpan(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "SegmentSpan":
        if self.end_ms <= self.start_ms:
            raise ValueError("Segment end must be after its start")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class RenderSpec(BaseModel):
    version: Literal[1] = 1
    source_id: str
    segments: list[SegmentSpan] = Field(min_length=1, max_length=40)
    video: VideoSettings = Field(default_factory=VideoSettings)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    title: str | None = None

    @property
    def total_duration_ms(self) -> int:
        return sum(segment.duration_ms for segment in self.segments)

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


def caption_defaults(preset: str) -> dict:
    return dict(CAPTION_PRESETS.get(preset, CAPTION_PRESETS["clean"]))


def build_spec(
    *,
    source_id: str,
    segments: list[dict],
    aspect: str = "9:16",
    resolution: int = 1080,
    framing: str = "crop",
    caption_preset: str = "clean",
    captions_enabled: bool = True,
    caption_overrides: dict | None = None,
    fps: int | None = None,
    title: str | None = None,
) -> RenderSpec:
    defaults = caption_defaults(caption_preset) if caption_preset != "custom" else {}
    payload = {
        "source_id": source_id,
        "segments": segments,
        "video": {"aspect": aspect, "resolution": resolution, "framing": framing, "fps": fps},
        "captions": {
            "enabled": captions_enabled,
            "preset": caption_preset,
            **{k: v for k, v in defaults.items() if k != "label"},
            **(caption_overrides or {}),
        },
        "title": title,
    }
    return RenderSpec.model_validate(payload)
