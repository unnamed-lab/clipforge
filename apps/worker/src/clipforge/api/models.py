from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from clipforge.discovery.criteria import DiscoveryCriteria
from clipforge.rendering.spec import DEFAULT_CAPTION_PRESET, CaptionSettings


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
    # What to fetch, independent of what the project will later export.
    quality: Literal[
        "lowest", "144p", "240p", "360p", "480p", "720p",
        "1080p", "1440p", "2160p", "best", "audio",
    ] | None = None
    processing: Literal["fast", "balanced", "high", "custom"] = "balanced"
    proxy_height: int | None = Field(default=None, ge=144, le=1440)


class UrlInspectRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    quality: Literal[
        "lowest", "144p", "240p", "360p", "480p", "720p",
        "1080p", "1440p", "2160p", "best", "audio",
    ] | None = None
    processing: Literal["fast", "balanced", "high", "custom"] = "balanced"
    proxy_height: int | None = Field(default=None, ge=144, le=1440)


class AnalysisRequest(BaseModel):
    goal: Literal[
        "best_moments", "social_clips", "educational", "funny", "debate",
        "emotional_stories", "tutorials", "highlights", "condensed", "custom",
    ] = "best_moments"
    clip_count: int = Field(default=8, ge=1, le=30)
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


class DiscoveryRequest(DiscoveryCriteria):
    """One targeted search for additional moments.

    ``count`` is how many new moments to add, not the new total. Existing
    candidates are never regenerated or replaced by this request.
    """


class HookPatch(BaseModel):
    text: str | None = Field(default=None, max_length=300)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    approved: bool | None = None
    placement: Literal["before", "none"] | None = None
    direction: Literal["opening", "before", "after", "none"] | None = None
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
        "question_answer", "story_escalation", "reaction_led", "montage", "loop",
        "open_loop", "manual",
    ] = "automatic"
    pacing_profile: Literal["calm", "conversational", "balanced", "energetic", "fast"] | None = None
    apply: bool = True


class PackagingGenerateBody(BaseModel):
    platform: Literal["tiktok", "youtube_shorts", "instagram_reels", "cross_platform"] | None = None
    topic: str | None = Field(default=None, max_length=120)
    subtopics: list[str] = Field(default_factory=list, max_length=8)
    genre: str | None = Field(default=None, max_length=60)
    strategy: str | None = Field(default=None, max_length=60)
    use_llm: bool = True
    check_trend: bool = False
    force_trend: bool = False
    settings: dict | None = None
    fields: list[
        Literal["titles", "description", "caption", "hashtags", "keywords", "thumbnail", "cta", "all"]
    ] = Field(default_factory=list, max_length=8)
    title_angle: str | None = Field(default=None, max_length=40)
    more_titles: int = Field(default=0, ge=0, le=3)
    research_mode: Literal["off", "free", "enhanced"] = "off"
    manual_terms: list[str] = Field(default_factory=list, max_length=12)
    reuse_brief: bool = True
    edited_fields: list[str] = Field(default_factory=list, max_length=12)
    force_fields: list[str] = Field(default_factory=list, max_length=12)
    metadata: dict | None = None


class PackagingPatchBody(BaseModel):
    platform: Literal["tiktok", "youtube_shorts", "instagram_reels", "cross_platform"] | None = None
    metadata: dict | None = None
    settings: dict | None = None
    edited_fields: list[str] = Field(default_factory=list, max_length=12)
    trend_action: Literal["use", "dismiss", "restore", "save", "manual"] | None = None
    trend_id: str | None = Field(default=None, max_length=160)
    regenerate_with_trend: bool = False
    manual_trend: dict | None = None


class TrendCheckBody(BaseModel):
    platform: Literal["tiktok", "youtube_shorts", "instagram_reels", "cross_platform"] | None = None
    topic: str | None = Field(default=None, max_length=120)
    subtopics: list[str] = Field(default_factory=list, max_length=8)
    genre: str | None = Field(default=None, max_length=60)
    strategy: str | None = Field(default=None, max_length=60)
    region: str | None = Field(default=None, max_length=40)
    country: str | None = Field(default=None, max_length=40)
    language: str | None = Field(default=None, max_length=40)
    niche: str | None = Field(default=None, max_length=120)
    audience: str | None = Field(default=None, max_length=200)
    freshness_days: int | None = Field(default=None, ge=1, le=365)
    limit: int | None = Field(default=None, ge=1, le=25)
    weights: dict[str, float] | None = None
    force: bool = False
    settings: dict | None = None


class RenderRequest(BaseModel):
    candidate_id: str | None = None
    segments: list[dict] | None = None
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal[720, 1080] = 1080
    framing: Literal["crop", "blur"] = "crop"
    composition: Literal["auto", "crop", "fit", "pip", "split", "stack", "facecam_focus", "manual"] = "auto"
    composition_overrides: dict[str, dict] | None = None
    crop_x_pct: float | None = Field(default=None, ge=0, le=100)
    crop_y_pct: float | None = Field(default=None, ge=0, le=100)
    caption_preset: Literal["clean", "large", "minimal", "branded", "custom"] = DEFAULT_CAPTION_PRESET
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
    # Compression is a separate choice from resolution: {"preset": "smallest"} or
    # {"preset": "custom", "crf": 22, "audio_bitrate_kbps": 128, ...}.
    compression: dict | None = None

    @model_validator(mode="after")
    def _requires_source(self) -> "RenderRequest":
        if not self.candidate_id and not self.segments:
            raise ValueError("Provide a candidate or explicit segments to render")
        return self


class RenderEstimateRequest(BaseModel):
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal[720, 1080] = 1080
    fps: int | None = Field(default=None, ge=15, le=60)
    duration_ms: int = Field(gt=0, le=86_400_000)
    compression: dict | None = None


class SceneInspectRequest(BaseModel):
    """A user-requested second look at one scene, at a readable resolution."""

    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    height: int | None = Field(default=None, ge=144, le=2160)


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
