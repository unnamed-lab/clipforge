"""Models for AI clip packaging and trend research.

Values that a platform can change (length limits, hashtag display) live in
``clipforge.platforms``. This module only holds the document shape that is
stored on a candidate and returned to the UI.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PACKAGING_VERSION = 2
BRIEF_VERSION = "content-brief/1.0"

TrendStatus = Literal["strong_match", "relevant_adjacent", "broad", "not_recommended"]
TrendDirection = Literal["rising", "flat", "falling", "unknown"]
TrendFreshness = Literal["fresh", "recent", "stale", "unknown"]
TrendMatchState = Literal[
    "live",
    "cached",
    "stale",
    "unavailable",
    "quota_exhausted",
    "auth_missing",
    "rate_limited",
    "manual_required",
    "no_results",
    "disabled",
]
HashtagCategory = Literal["topic", "niche", "broad", "trending"]
HashtagProvenance = Literal["transcript", "profile", "genre", "trend", "platform"]


TitleAngle = Literal[
    "descriptive",
    "insight",
    "problem_solution",
    "question",
    "benefit",
    "contrarian",
    "story",
    "search",
    "quote",
    "curiosity",
    "result",
    "how_to",
    "direct",
]


class TitleScores(BaseModel):
    """Transparent editorial fit scores. Never a prediction of views or CTR."""

    accuracy: float = Field(default=0.0, ge=0.0, le=1.0)
    specificity: float = Field(default=0.0, ge=0.0, le=1.0)
    audience_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    curiosity: float = Field(default=0.0, ge=0.0, le=1.0)
    search_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    readability: float = Field(default=0.0, ge=0.0, le=1.0)
    weighted: float = Field(default=0.0, ge=0.0, le=1.0)


class TitleVariant(BaseModel):
    text: str
    style: Literal["direct", "curiosity", "question", "result", "how_to", "quote"] = "direct"
    reason: str = ""
    chars: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    within_limit: bool = True
    basis: Literal["transcript", "hook", "topic", "profile"] = "transcript"
    angle: TitleAngle = "direct"
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    scores: TitleScores | None = None
    accepted: bool = True
    rejection_reason: str = ""


class DescriptionVariants(BaseModel):
    short: str = ""
    long: str = ""
    short_limit: int = Field(default=0, ge=0)
    long_limit: int = Field(default=0, ge=0)
    within_limit: bool = True


class PlatformCaption(BaseModel):
    platform: str
    label: str = ""
    text: str = ""
    char_count: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    within_limit: bool = True
    notes: list[str] = Field(default_factory=list)


class HashtagSuggestion(BaseModel):
    tag: str
    category: HashtagCategory = "topic"
    reason: str = ""
    provenance: HashtagProvenance = "transcript"
    relevance: float = Field(default=0.5, ge=0.0, le=1.0)


class SearchKeyword(BaseModel):
    phrase: str
    reason: str = ""
    source: Literal["ai_generated", "platform_verified"] = "ai_generated"
    verified: bool = False


class ThumbnailText(BaseModel):
    text: str = ""
    limit: int = Field(default=0, ge=0)
    within_limit: bool = True
    reason: str = ""


class MetadataValidation(BaseModel):
    ok: bool = True
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    platform: str = "cross_platform"


class PackagingProvenance(BaseModel):
    generated_at: str = ""
    method: Literal["deterministic", "llm", "manual", "hybrid"] = "deterministic"
    provider: str | None = None
    model: str | None = None
    transcript_id: str | None = None
    source_start_ms: int = Field(default=0, ge=0)
    source_end_ms: int = Field(default=0, ge=0)
    timeline_applied: bool = False
    sentence_ids: list[int] = Field(default_factory=list)
    grounding: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BriefEvidence(BaseModel):
    """A transcript-backed reference used to justify a packaging claim."""

    id: str
    text: str
    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(default=0, ge=0)
    kind: Literal["claim", "example", "caveat", "hook", "context", "section"] = "claim"
    section_id: str | None = None
    sentence_id: int | None = None
    in_clip: bool = True


class SectionSummary(BaseModel):
    id: str
    label: str = ""
    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(default=0, ge=0)
    summary: str = ""
    key_terms: list[str] = Field(default_factory=list)
    contains_clip: bool = False
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)


ClipType = Literal[
    "standalone_insight",
    "excerpt_needs_context",
    "practical_tutorial",
    "personal_story",
    "concept_explanation",
    "debate",
    "reaction_opinion",
    "highlight",
    "unknown",
]


class ContentBrief(BaseModel):
    """Source-grounded understanding of the video and the selected clip.

    Video-level fields describe the whole source. Clip-level fields describe only
    what a viewer of the selected clip can actually see and hear. ``wider_context``
    is explicitly material from elsewhere in the video and must not be presented
    as if it were inside the clip.
    """

    version: str = BRIEF_VERSION
    method: Literal["deterministic", "llm"] = "deterministic"
    brief_key: str = ""
    clip_type: ClipType = "unknown"
    subject: str = ""
    video_topic: str = ""
    clip_topic: str = ""
    central_claim: str = ""
    key_insight: str = ""
    audience_problem: str = ""
    practical_takeaway: str = ""
    supporting_points: list[str] = Field(default_factory=list)
    example_evidence: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    stronger_hook: str = ""
    hook_evidence_id: str | None = None
    entities: list[str] = Field(default_factory=list)
    search_concepts: list[str] = Field(default_factory=list)
    minor_topics: list[str] = Field(default_factory=list)
    wider_context: list[str] = Field(default_factory=list)
    evidence: list[BriefEvidence] = Field(default_factory=list)
    sections: list[SectionSummary] = Field(default_factory=list)
    source_title: str | None = None
    source_description: str | None = None
    confidence: Literal["high", "medium", "low"] = "medium"
    ambiguity: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    context_chars: int = Field(default=0, ge=0)
    omitted_sections: int = Field(default=0, ge=0)
    truncated: bool = False


ResearchState = Literal[
    "live",
    "cached",
    "stale",
    "unavailable",
    "quota_exhausted",
    "auth_missing",
    "rate_limited",
    "manual_required",
    "no_results",
    "disabled",
]


class KeywordObservation(BaseModel):
    """One research observation with its provenance and limits.

    ``value`` is only populated when the provider supplied a number and the unit
    says what it measures. Relative indices are never converted into volumes.
    """

    id: str
    term: str
    provider: str
    kind: Literal["suggestion", "relative_interest", "public_video", "manual", "ai_hypothesis"] = "suggestion"
    value: float | None = None
    unit: str = ""
    region: str = ""
    language: str = ""
    retrieved_at: str = ""
    freshness: Literal["fresh", "recent", "stale", "unknown"] = "unknown"
    source_url: str | None = None
    evidence: str = ""
    limitations: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"
    manual: bool = False


class KeywordResearchProviderStatus(BaseModel):
    provider: str
    label: str = ""
    operational: bool = False
    requires_credentials: bool = False
    requires_approval: bool = False
    manual_research: bool = False
    detail: str = ""
    limitations: list[str] = Field(default_factory=list)


class KeywordResearchResult(BaseModel):
    mode: Literal["off", "free", "enhanced"] = "off"
    status: ResearchState = "disabled"
    retrieved_at: str = ""
    providers: list[KeywordResearchProviderStatus] = Field(default_factory=list)
    observations: list[KeywordObservation] = Field(default_factory=list)
    suggested_terms: list[str] = Field(default_factory=list)
    terms_used: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    cached: bool = False


class ClipMetadata(BaseModel):
    titles: list[TitleVariant] = Field(default_factory=list)
    descriptions: DescriptionVariants = Field(default_factory=DescriptionVariants)
    caption: PlatformCaption = Field(default_factory=lambda: PlatformCaption(platform="cross_platform"))
    platform_captions: list[PlatformCaption] = Field(default_factory=list)
    hashtags: list[HashtagSuggestion] = Field(default_factory=list)
    keywords: list[SearchKeyword] = Field(default_factory=list)
    cta: str | None = None
    thumbnail_text: ThumbnailText | None = None
    thumbnail_alternatives: list[str] = Field(default_factory=list)
    content_angle: str = ""
    key_takeaway: str = ""
    suggested_audience: str = ""
    source_hook: str = ""
    source_hook_start_ms: int = Field(default=0, ge=0)
    editorial_rationale: str = ""
    context_warnings: list[str] = Field(default_factory=list)
    validation: MetadataValidation = Field(default_factory=MetadataValidation)
    provenance: PackagingProvenance = Field(default_factory=PackagingProvenance)


class TrendScoreBreakdown(BaseModel):
    semantic_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    platform_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    regional_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    recency: float = Field(default=0.0, ge=0.0, le=1.0)
    rising_interest: float = Field(default=0.0, ge=0.0, le=1.0)
    content_suitability: float = Field(default=0.0, ge=0.0, le=1.0)
    misleading_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    rights_constraint: float = Field(default=0.0, ge=0.0, le=1.0)
    weighted: float = 0.0


class TrendRecommendation(BaseModel):
    id: str
    name: str
    platform: str = "unknown"
    provider: str = "manual"
    region: str = ""
    category: str = ""
    evidence: str = ""
    volume: int | None = Field(default=None, ge=0)
    ranking: int | None = Field(default=None, ge=1)
    growth: float | None = None
    observed_at: str | None = None
    updated_at: str | None = None
    published_at: str | None = None
    direction: TrendDirection = "unknown"
    source_url: str | None = None
    freshness: TrendFreshness = "unknown"
    confidence: Literal["low", "medium", "high"] = "low"
    limitations: list[str] = Field(default_factory=list)
    rights: str = ""
    status: TrendStatus = "not_recommended"
    status_reason: str = ""
    adaptation: str = ""
    scores: TrendScoreBreakdown = Field(default_factory=TrendScoreBreakdown)
    used: bool = False
    dismissed: bool = False
    manual: bool = False


class ManualTrend(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    platform: str = Field(default="unknown", max_length=40)
    region: str = Field(default="", max_length=40)
    category: str = Field(default="", max_length=80)
    evidence: str = Field(default="", max_length=600)
    source_url: str | None = Field(default=None, max_length=2048)
    observed_at: str | None = Field(default=None, max_length=60)
    notes: str = Field(default="", max_length=600)
    direction: Literal["rising", "flat", "falling", "unknown"] = "unknown"


class TrendCheckSettings(BaseModel):
    platform: str = "cross_platform"
    region: str = "US"
    country: str = ""
    language: str = "en"
    niche: str = ""
    audience: str = ""
    freshness_days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=8, ge=1, le=25)
    weights: dict[str, float] = Field(default_factory=dict)


class TrendCheckResult(BaseModel):
    status: TrendMatchState = "unavailable"
    provider: str = "none"
    platform: str = "cross_platform"
    checked_at: str = ""
    expires_at: str | None = None
    cached: bool = False
    stale: bool = False
    settings: TrendCheckSettings = Field(default_factory=TrendCheckSettings)
    results: list[TrendRecommendation] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    manual_references: list[TrendRecommendation] = Field(default_factory=list)


class PackagingDocument(BaseModel):
    version: int = PACKAGING_VERSION
    platform: str = "cross_platform"
    metadata: ClipMetadata | None = None
    brief: ContentBrief | None = None
    research: KeywordResearchResult | None = None
    trend: TrendCheckResult | None = None
    settings: dict = Field(default_factory=dict)
    edited_fields: list[str] = Field(default_factory=list)
    skill_version: str = ""
    brief_meta: dict = Field(default_factory=dict)
    dismissed_trend_ids: list[str] = Field(default_factory=list)
    saved_trends: list[TrendRecommendation] = Field(default_factory=list)
    manual_trends: list[TrendRecommendation] = Field(default_factory=list)
    updated_at: str = ""
    warnings: list[str] = Field(default_factory=list)
