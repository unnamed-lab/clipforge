"""Reusable client profiles and deterministic setting resolution.

A profile is a saved house style: brief defaults, export shape, captions, audio
policy, brand assets and rules. A project inherits a profile and may override
individual settings; resolution records where each effective value came from so the
UI can say "inherited" versus "overridden" without guessing.
"""

from __future__ import annotations

import copy
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from clipforge.briefs.spec import ClippingBrief
from clipforge.rendering.spec import CAPTION_PRESETS

LOGO_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "custom")
AUDIO_MODES = ("original", "original_plus_music", "replace")
SAFE_SECTIONS = ("export", "captions", "audio", "branding", "brief")


class Branding(BaseModel):
    logo_asset_id: str | None = None
    logo_required: bool = False
    logo_position: str = "top-right"
    logo_width_pct: float = Field(default=18.0, ge=3, le=60)
    logo_opacity: float = Field(default=1.0, ge=0.05, le=1.0)
    logo_margin_px: int = Field(default=48, ge=0, le=400)
    logo_custom_x_pct: float = Field(default=82.0, ge=0, le=100)
    logo_custom_y_pct: float = Field(default=8.0, ge=0, le=100)
    logo_from_ms: int | None = Field(default=None, ge=0)
    logo_to_ms: int | None = Field(default=None, ge=0)
    brand_colors: list[str] = Field(default_factory=lambda: ["#0F172A", "#F8FAFC", "#38BDF8"])
    font_family: str = "Arial"
    safe_area_pct: float = Field(default=6.0, ge=0, le=25)
    visual_treatment: Literal["clean", "bold", "documentary"] = "clean"
    framing: Literal["crop", "blur"] = "crop"

    @field_validator("logo_position")
    @classmethod
    def _position(cls, value: str) -> str:
        return value if value in LOGO_POSITIONS else "top-right"

    @field_validator("brand_colors")
    @classmethod
    def _colours(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            text = (value or "").upper()
            if len(text) == 7 and text.startswith("#") and all(c in "0123456789ABCDEF" for c in text[1:]):
                cleaned.append(text)
        return cleaned or ["#0F172A"]

    @model_validator(mode="after")
    def _window(self) -> "Branding":
        if self.logo_from_ms is not None and self.logo_to_ms is not None:
            if self.logo_to_ms <= self.logo_from_ms:
                raise ValueError("Logo end must be after its start")
        return self


class AudioPolicy(BaseModel):
    mode: Literal["original", "original_plus_music", "replace"] = "original"
    music_asset_id: str | None = None
    music_gain_db: float = Field(default=-18.0, ge=-40, le=0)
    duck_music: bool = True
    allow_replacement: bool = False
    normalize: bool = True
    denoise: bool = False
    dialogue_enhance: bool = False
    fade_in_ms: int = Field(default=0, ge=0, le=5_000)
    fade_out_ms: int = Field(default=0, ge=0, le=5_000)

    @model_validator(mode="after")
    def _music_mode(self) -> "AudioPolicy":
        if self.mode == "replace" and not self.allow_replacement:
            raise ValueError("Audio replacement needs explicit permission in the profile")
        return self


class CaptionPolicy(BaseModel):
    enabled: bool = True
    required: bool = False
    preset: str = "clean"
    highlight_words: bool = True
    max_chars_per_line: int = Field(default=0, ge=0, le=80)
    shadow: float = Field(default=0.0, ge=0, le=8)
    background_opacity: int = Field(default=0, ge=0, le=100)

    @field_validator("preset")
    @classmethod
    def _preset(cls, value: str) -> str:
        return value if value in CAPTION_PRESETS or value == "custom" else "clean"


class ExportDefaults(BaseModel):
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal[720, 1080] = 1080
    fps: int | None = Field(default=30, ge=15, le=60)
    format: Literal["mp4"] = "mp4"
    normalise_audio: bool = True


class ProfileRules(BaseModel):
    disclaimers: list[str] = Field(default_factory=list)
    terminology: dict[str, str] = Field(default_factory=dict)
    banned_terms: list[str] = Field(default_factory=list)
    brand_safety_notes: str = Field(default="", max_length=1_000)

    @field_validator("disclaimers", "banned_terms")
    @classmethod
    def _clean(cls, values: list[str]) -> list[str]:
        return [" ".join(value.split())[:280] for value in values if value and value.strip()][:20]

    @field_validator("terminology")
    @classmethod
    def _terminology(cls, values: dict[str, str]) -> dict[str, str]:
        return {
            " ".join(key.split())[:80]: " ".join(value.split())[:160]
            for key, value in list(values.items())[:50]
            if key and value
        }


class ClientProfileDocument(BaseModel):
    """The editable body of a profile. Identity and version live in the database."""

    name: str = Field(min_length=1, max_length=120)
    brief: ClippingBrief = Field(default_factory=ClippingBrief)
    branding: Branding = Field(default_factory=Branding)
    audio: AudioPolicy = Field(default_factory=AudioPolicy)
    captions: CaptionPolicy = Field(default_factory=CaptionPolicy)
    export: ExportDefaults = Field(default_factory=ExportDefaults)
    rules: ProfileRules = Field(default_factory=ProfileRules)


DEFAULT_PROFILE: dict = ClientProfileDocument(name="Default").model_dump()


def _merge_section(base: dict, override: dict) -> tuple[dict, list[str]]:
    merged = copy.deepcopy(base)
    overridden: list[str] = []
    for key, value in (override or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested, nested_keys = _merge_section(merged[key], value)
            merged[key] = nested
            overridden.extend(f"{key}.{nested}" for nested in nested_keys)
        else:
            merged[key] = value
            overridden.append(key)
    return merged, overridden


def resolve_settings(profile: dict | None, overrides: dict | None) -> dict:
    """Resolve effective settings and provenance.

    ``profile`` is a stored profile row (its ``document`` holds the editable body).
    ``overrides`` is ``{"export": {...}, "captions": {...}, ...}`` from the project.
    """
    document = copy.deepcopy((profile or {}).get("document") or DEFAULT_PROFILE)
    document.pop("name", None)
    effective = {section: copy.deepcopy(document.get(section, {})) for section in SAFE_SECTIONS}
    provenance: dict[str, str] = {}

    for section in SAFE_SECTIONS:
        for key in effective[section]:
            provenance[f"{section}.{key}"] = "profile" if profile else "default"

    for section, values in (overrides or {}).items():
        if section not in SAFE_SECTIONS or not isinstance(values, dict):
            continue
        merged, changed = _merge_section(effective[section], values)
        effective[section] = merged
        for key in changed:
            provenance[f"{section}.{key}"] = "project"

    effective["profile"] = {
        "id": (profile or {}).get("id"),
        "name": (profile or {}).get("name"),
        "version": (profile or {}).get("version"),
    }
    return {"settings": effective, "provenance": provenance}


def profile_snapshot(profile: dict | None) -> dict:
    """Immutable copy stored on a render so later profile edits cannot rewrite history."""
    if not profile:
        return {"id": None, "name": None, "version": None, "document": None}
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "version": profile.get("version"),
        "document": copy.deepcopy(profile.get("document")),
        "captured_at": profile.get("updated_at"),
    }
