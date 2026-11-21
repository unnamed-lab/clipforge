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
from clipforge.rendering.spec import (
    CAPTION_PRESETS,
    DEFAULT_CAPTION_ALIGNMENT,
    DEFAULT_CAPTION_MARGIN_V,
    DEFAULT_CAPTION_POSITION,
    DEFAULT_CAPTION_POSITION_PCT,
    DEFAULT_CAPTION_PRESET,
    FaceCamOverride,
    Rect,
)

LOGO_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "custom")
AUDIO_MODES = ("original", "original_plus_music", "replace")
SAFE_SECTIONS = ("export", "captions", "audio", "branding", "brief", "packaging")


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
    preset: str = DEFAULT_CAPTION_PRESET
    highlight_words: bool = True
    position: Literal["bottom", "center", "top", "custom"] = DEFAULT_CAPTION_POSITION
    position_pct: float = Field(default=DEFAULT_CAPTION_POSITION_PCT, ge=0, le=100)
    alignment: Literal["left", "center", "right"] = DEFAULT_CAPTION_ALIGNMENT
    margin_v: int = Field(default=DEFAULT_CAPTION_MARGIN_V, ge=0, le=900)
    max_chars_per_line: int = Field(default=0, ge=0, le=80)
    shadow: float = Field(default=0.0, ge=0, le=8)
    background_opacity: int = Field(default=0, ge=0, le=100)

    @field_validator("preset")
    @classmethod
    def _preset(cls, value: str) -> str:
        return value if value in CAPTION_PRESETS or value == "custom" else DEFAULT_CAPTION_PRESET


class ExportDefaults(BaseModel):
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal[720, 1080] = 1080
    fps: int | None = Field(default=30, ge=15, le=60)
    format: Literal["mp4"] = "mp4"
    normalise_audio: bool = True
    # Saved layout choice. Keeping these beside the other export defaults is what
    # makes a selected preset survive navigation and reload.
    composition: Literal[
        "auto", "crop", "fit", "pip", "split", "stack", "facecam_focus", "manual"
    ] = "auto"
    face_cam_position: Literal["top", "bottom"] = "bottom"
    face_cam_rect: Rect | None = None
    face_cam_overrides: list[FaceCamOverride] = Field(default_factory=list)
    stack_split_pct: float = Field(default=60.0, ge=35.0, le=80.0)
    adaptive_stack: bool = False
    crop_x_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    crop_y_pct: float | None = Field(default=None, ge=0.0, le=100.0)


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


class PackagingPreferences(BaseModel):
    """Default AI packaging voice and guardrails for a client.

    These values are snapshotted into a project so later profile edits cannot
    silently rewrite metadata the user already reviewed.
    """

    enabled: bool = True
    default_platform: Literal["tiktok", "youtube_shorts", "instagram_reels", "cross_platform"] = "cross_platform"
    tone: str = Field(default="", max_length=200)
    audience: str = Field(default="", max_length=200)
    language: str = Field(default="en", max_length=40)
    region: str = Field(default="US", max_length=40)
    niche: str = Field(default="", max_length=120)
    brand_voice: str = Field(default="", max_length=400)
    hashtag_style: Literal["minimal", "balanced", "descriptive"] = "balanced"
    hashtag_count: int | None = Field(default=None, ge=0, le=30)
    cta_preference: Literal["auto", "none", "follow", "comment", "link", "custom"] = "auto"
    cta_text: str = Field(default="", max_length=160)
    trend_check: Literal["automatic", "manual", "disabled"] = "manual"
    research_mode: Literal["off", "free", "enhanced"] = "off"
    title_style: str = Field(default="", max_length=120)
    description_style: Literal["auto", "short_form", "long_form"] = "auto"
    include_keywords: bool = True
    thumbnail_text: bool = True
    brand_terms: list[str] = Field(default_factory=list)
    restricted_words: list[str] = Field(default_factory=list)
    restricted_topics: list[str] = Field(default_factory=list)
    restricted_claims: list[str] = Field(default_factory=list)

    @field_validator("brand_terms", "restricted_words", "restricted_topics", "restricted_claims")
    @classmethod
    def _clean_lists(cls, values: list[str]) -> list[str]:
        return [" ".join(value.split())[:120] for value in values if value and value.strip()][:40]


class ClientProfileDocument(BaseModel):
    """The editable body of a profile. Identity and version live in the database."""

    name: str = Field(min_length=1, max_length=120)
    brief: ClippingBrief = Field(default_factory=ClippingBrief)
    branding: Branding = Field(default_factory=Branding)
    audio: AudioPolicy = Field(default_factory=AudioPolicy)
    captions: CaptionPolicy = Field(default_factory=CaptionPolicy)
    export: ExportDefaults = Field(default_factory=ExportDefaults)
    rules: ProfileRules = Field(default_factory=ProfileRules)
    packaging: PackagingPreferences = Field(default_factory=PackagingPreferences)


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
    effective = {}
    for section in SAFE_SECTIONS:
        # Profiles written before a section existed must still resolve to the
        # current document defaults rather than an empty object.
        base = copy.deepcopy(DEFAULT_PROFILE.get(section) or {})
        stored = document.get(section)
        if isinstance(stored, dict):
            base.update(copy.deepcopy(stored))
        effective[section] = base
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
