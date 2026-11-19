"""Versioned editorial documents shared by analysis, the API and the EDL."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

HookStrategy = Literal[
    "question",
    "outcome",
    "curiosity",
    "story",
    "humour",
    "emotional",
    "contrarian",
    "chronological",
    "manual",
]
NarrativeRole = Literal["hook", "context", "main", "development", "payoff", "loop"]
PacingProfile = Literal["calm", "conversational", "balanced", "energetic", "fast"]
EditorialStrategy = Literal[
    "chronological",
    "cold_open",
    "in_medias_res",
    "result_first",
    "question_answer",
    "story_escalation",
    "reaction_led",
    "montage",
    "loop",
    "open_loop",
    "manual",
]


class HookCandidateDocument(BaseModel):
    text: str = ""
    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(default=0, ge=0)
    source: Literal["candidate", "external", "none"] = "none"
    placement: Literal["before", "none"] = "none"
    direction: Literal["opening", "before", "after", "none"] = "none"
    strategy: HookStrategy = "story"
    approved: bool = False
    grounded: bool = True
    quality: float = Field(default=0.0, ge=0.0, le=100.0)
    reason: str = ""
    needs_context: bool = False
    review_required: bool = False
    scores: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


class NarrativeRelationship(BaseModel):
    source_role: NarrativeRole
    target_role: NarrativeRole
    relation: Literal["sets_up", "elaborates", "resolves", "calls_back", "contrasts"]


class RetentionSegment(BaseModel):
    role: NarrativeRole
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    sentence_start: int | None = None
    sentence_end: int | None = None
    key_point: str = ""
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    context_loss: float = Field(default=0.0, ge=0.0, le=1.0)
    transition: Literal["cut", "continuous", "fade", "flashback"] = "cut"
    review: bool = False
    pattern_interrupt: bool = False
    output_start_ms: int | None = Field(default=None, ge=0)
    output_end_ms: int | None = Field(default=None, ge=0)

    @property
    def duration_ms(self) -> int:
        return max(0, self.source_end_ms - self.source_start_ms)


class TimelineEntry(BaseModel):
    """One source span in output order. Source and output time are separate."""

    index: int = Field(ge=0)
    role: NarrativeRole = "main"
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    output_start_ms: int = Field(ge=0)
    output_end_ms: int = Field(ge=0)
    sentence_start: int | None = None
    sentence_end: int | None = None
    transition: Literal["cut", "continuous", "flashback"] = "cut"
    key_point: str = ""
    reason: str = ""

    @property
    def duration_ms(self) -> int:
        return max(0, self.output_end_ms - self.output_start_ms)


class TimelineReport(BaseModel):
    """Deterministic checks on a non-linear edit before it can be rendered."""

    version: int = 1
    entries: list[TimelineEntry] = Field(default_factory=list)
    total_output_ms: int = Field(default=0, ge=0)
    covered_ms: int = Field(default=0, ge=0)
    missing_ms: int = Field(default=0, ge=0)
    duplicated_ms: int = Field(default=0, ge=0)
    # Source time a deliberate loop replays. Kept apart from duplicated_ms so an
    # intentional repeat cannot be confused with an accidental overlap.
    loop_ms: int = Field(default=0, ge=0)
    gaps: list[dict] = Field(default_factory=list)
    overlaps: list[dict] = Field(default_factory=list)
    valid: bool = True
    warnings: list[str] = Field(default_factory=list)


class EditorialAlternative(BaseModel):
    """A strategy or pacing variant the user can compare, not a promise."""

    id: str
    label: str
    strategy: EditorialStrategy = "chronological"
    description: str = ""
    available: bool = True
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    estimated_duration_ms: int = Field(default=0, ge=0)
    pacing_profile: PacingProfile | None = None
    reason: str = ""
    requires_approval: bool = False


class EditorialDecision(BaseModel):
    version: int = 1
    strategy: EditorialStrategy = "chronological"
    label: str = "Chronological"
    style: str = "automatic"
    rationale: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    alternatives: list[EditorialAlternative] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    review_required: bool = False
    reordered: bool = False
    applied: bool = False
    fallback: str = ""
    pacing_profile: PacingProfile = "balanced"
    timeline: TimelineReport | None = None


class RetentionPlan(BaseModel):
    version: int = 1
    profile: PacingProfile = "balanced"
    segments: list[RetentionSegment] = Field(default_factory=list)
    relationships: list[NarrativeRelationship] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    context_loss: float = Field(default=0.0, ge=0.0, le=1.0)
    review_required: bool = False
    warnings: list[str] = Field(default_factory=list)
    hook: HookCandidateDocument | None = None
    candidates: list[HookCandidateDocument] = Field(default_factory=list)
    strategy: EditorialStrategy = "chronological"
    rationale: str = ""
    alternatives: list[EditorialAlternative] = Field(default_factory=list)
    timeline: TimelineReport | None = None
    reordered: bool = False
    editorial: EditorialDecision | None = None


class TimingSegment(BaseModel):
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    sentence_start: int | None = None
    sentence_end: int | None = None
    reason: str = "kept"


class TimingCut(BaseModel):
    kind: Literal["dead_air", "filler"]
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    reason: str = ""


class TimingPlan(BaseModel):
    version: int = 1
    profile: PacingProfile = "balanced"
    segments: list[TimingSegment] = Field(default_factory=list)
    cuts: list[TimingCut] = Field(default_factory=list)
    original_duration_ms: int = Field(ge=0)
    output_duration_ms: int = Field(ge=0)
    removed_ms: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    review_required: bool = False
