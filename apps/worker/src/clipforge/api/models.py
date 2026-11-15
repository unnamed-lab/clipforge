from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from clipforge.rendering.spec import CaptionSettings


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goals: dict = Field(default_factory=dict)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    goals: dict | None = None


class UrlImportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    rights_confirmed: bool = False


class AnalysisRequest(BaseModel):
    goal: Literal[
        "best_moments", "social_clips", "educational", "funny", "debate",
        "emotional_stories", "tutorials", "highlights", "condensed", "custom",
    ] = "best_moments"
    clip_count: int = Field(default=8, ge=3, le=20)
    duration_band: Literal["auto", "short", "medium", "long"] = "auto"
    quality: Literal["fast", "balanced", "best"] = "balanced"
    model: str | None = None
    language: str | None = Field(default=None, max_length=12)
    vad: bool = True
    use_llm: bool = False
    llm_consent: bool = False
    weights: dict[str, float] | None = None
    instructions: str | None = Field(default=None, max_length=600)


class CandidatePatch(BaseModel):
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    decision: Literal["proposed", "accepted", "rejected"] | None = None
    title: str | None = Field(default=None, max_length=120)
    reset_edits: bool = False


class RenderRequest(BaseModel):
    candidate_id: str | None = None
    segments: list[dict] | None = None
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal[720, 1080] = 1080
    framing: Literal["crop", "blur"] = "crop"
    caption_preset: Literal["clean", "large", "minimal", "branded", "custom"] = "clean"
    captions_enabled: bool = True
    captions: CaptionSettings | None = None
    normalize_audio: bool = False
    fps: int | None = Field(default=None, ge=15, le=60)
    title: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _requires_source(self) -> "RenderRequest":
        if not self.candidate_id and not self.segments:
            raise ValueError("Provide a candidate or explicit segments to render")
        return self
