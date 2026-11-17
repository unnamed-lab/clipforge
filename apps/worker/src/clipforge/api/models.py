from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from clipforge.rendering.spec import CaptionSettings


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goals: dict = Field(default_factory=dict)
    profile_id: str | None = None
    brief: dict | None = None


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
    clip_count: int = Field(default=8, ge=1, le=20)
    duration_band: Literal["auto", "short", "medium", "long"] = "auto"
    quality: Literal["fast", "balanced", "best"] = "balanced"
    model: str | None = None
    language: str | None = Field(default=None, max_length=12)
    vad: bool = True
    use_llm: bool = False
    llm_consent: bool = False
    weights: dict[str, float] | None = None
    instructions: str | None = Field(default=None, max_length=600)
    brief: dict | None = None


class HookPatch(BaseModel):
    text: str | None = Field(default=None, max_length=300)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    approved: bool | None = None
    placement: Literal["before", "none"] | None = None
    source: Literal["candidate", "external", "none"] | None = None


class CandidatePatch(BaseModel):
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    decision: Literal["proposed", "accepted", "rejected"] | None = None
    title: str | None = Field(default=None, max_length=120)
    summary: str | None = Field(default=None, max_length=600)
    hook: HookPatch | None = None
    clear_hook: bool = False
    reset_edits: bool = False


class BoundaryAction(BaseModel):
    action: Literal["accept", "reject", "reset"]
    end_ms: int | None = Field(default=None, ge=0)


class EditorialAction(BaseModel):
    """Re-evaluate a candidate under an explicit strategy or pacing profile."""

    strategy: Literal[
        "automatic", "chronological", "cold_open", "in_medias_res", "result_first",
        "question_answer", "story_escalation", "reaction_led", "manual",
    ] = "automatic"
    pacing_profile: Literal["calm", "conversational", "balanced", "energetic", "fast"] | None = None
    apply: bool = True


class RenderRequest(BaseModel):
    candidate_id: str | None = None
    segments: list[dict] | None = None
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal[720, 1080] = 1080
    framing: Literal["crop", "blur"] = "crop"
    composition: Literal["auto", "crop", "fit", "pip", "split", "manual"] = "auto"
    composition_overrides: dict[str, dict] | None = None
    crop_x_pct: float | None = Field(default=None, ge=0, le=100)
    crop_y_pct: float | None = Field(default=None, ge=0, le=100)
    caption_preset: Literal["clean", "large", "minimal", "branded", "custom"] = "clean"
    captions_enabled: bool = True
    captions: CaptionSettings | None = None
    captions_burn_in: bool = True
    captions_sidecar: bool = False
    audio: dict | None = None
    logo: dict | None = None
    include_hook: bool = True
    apply_editorial: bool = True
    approve_editorial: bool = False
    normalize_audio: bool = False
    override_policy: bool = False
    fps: int | None = Field(default=None, ge=15, le=60)
    title: str | None = Field(default=None, max_length=120)
    edit_spec_id: str | None = None

    @model_validator(mode="after")
    def _requires_source(self) -> "RenderRequest":
        if not self.candidate_id and not self.segments:
            raise ValueError("Provide a candidate or explicit segments to render")
        return self


class ProfileBody(BaseModel):
    document: dict


class ProjectSettingsBody(BaseModel):
    profile_id: str | None = None
    overrides: dict | None = None


class BriefBody(BaseModel):
    brief: dict


class EditSpecBody(BaseModel):
    spec: dict
    candidate_id: str | None = None


class CorrectionPatch(BaseModel):
    text: str | None = Field(default=None, max_length=200)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)


class CaptionPresetBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    config: dict


class EvaluationBody(BaseModel):
    candidate_id: str | None = None
    render_id: str | None = None
    decision: Literal["accepted", "rejected", "needs_revision"]
    ratings: dict = Field(default_factory=dict)
    rejection_reason: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=2000)


class DatasetBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    filters: dict = Field(default_factory=dict)
    train_ratio: float = Field(default=0.8, gt=0, lt=1)
    seed: int = 7


class LibrarySourceBody(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    platform: str = "other"
    title: str | None = Field(default=None, max_length=300)
    creator: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=1000)


class LibraryImportBody(BaseModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=120)
    profile_id: str | None = None
    brief: dict | None = None
    auto_analyse: bool = True


class LibrarySourcePatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    creator: str | None = Field(default=None, max_length=200)
    duration_ms: int | None = Field(default=None, ge=0)
    thumbnail_url: str | None = Field(default=None, max_length=2048)
    language: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=2000)
    published_at: str | None = Field(default=None, max_length=60)
    availability: Literal["unknown", "available", "unavailable", "expired", "revoked"] | None = None
    availability_note: str | None = Field(default=None, max_length=500)


class LibrarySearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    platform: Literal["youtube", "twitch"] = "youtube"
    max_results: int = Field(default=10, ge=1, le=25)


class RightsBody(BaseModel):
    reference: Literal["allowed", "denied", "unknown"] | None = None
    import_media: Literal["allowed", "denied", "unknown"] | None = None
    transcribe: Literal["allowed", "denied", "unknown"] | None = None
    edit_export: Literal["allowed", "denied", "unknown"] | None = None
    commercial_reuse: Literal["allowed", "denied", "unknown"] | None = None
    train_model: Literal["allowed", "denied", "unknown"] | None = None
    licence: str | None = Field(default=None, max_length=200)
    licence_url: str | None = Field(default=None, max_length=2048)
    evidence: str | None = Field(default=None, max_length=2000)
    attribution: str | None = Field(default=None, max_length=500)
    permission_source: str | None = Field(default=None, max_length=300)
    restrictions: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)
    review_status: Literal["needs_review", "partial", "reviewed", "revoked"] | None = None
