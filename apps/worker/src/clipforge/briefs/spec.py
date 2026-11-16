"""Clipping brief: the structured definition of what the user actually wants.

Every download is not a quest for viral comedy. The brief is the contract between
the user's intent and candidate generation, scoring and hook selection, and it is
snapshotted onto each analysis run so an old result explains itself.
"""

from __future__ import annotations


from pydantic import BaseModel, Field, field_validator, model_validator

MOMENT_TYPES: dict[str, str] = {
    "funny": "Funny moments",
    "joke": "Jokes and punchlines",
    "reaction": "Reactions",
    "best_advice": "Best advice",
    "practical_tip": "Practical tips",
    "controversial": "Controversial opinions",
    "emotional": "Emotional moments",
    "storytelling": "Storytelling",
    "strong_insight": "Strong insights",
    "memorable_quote": "Memorable quotes",
    "audience_question": "Audience questions",
    "custom": "Custom category",
}

PLATFORMS = (
    "tiktok", "instagram_reels", "youtube_shorts", "youtube", "linkedin",
    "x", "podcast", "internal", "other",
)

DEFAULT_INCLUDE_TYPES = ("best_advice", "strong_insight", "storytelling")


def _dedupe(values: list[str], limit: int = 40) -> list[str]:
    seen: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split()).strip()
        if cleaned and cleaned.lower() not in {existing.lower() for existing in seen}:
            seen.append(cleaned[:80])
    return seen[:limit]


class HookPolicy(BaseModel):
    """The 1-3 second opening. Default on, because a cold open is the point."""

    enabled: bool = True
    min_ms: int = Field(default=1_000, ge=500, le=10_000)
    max_ms: int = Field(default=3_000, ge=1_000, le=15_000)
    allow_external: bool = True
    strict_chronological: bool = False
    required: bool = False
    max_distance_ms: int = Field(default=180_000, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "HookPolicy":
        if self.max_ms < self.min_ms:
            raise ValueError("Hook maximum must be at least the minimum")
        return self


class ContextRules(BaseModel):
    require_standalone: bool = True
    avoid_mid_sentence: bool = True
    max_setup_ms: int = Field(default=2_500, ge=0, le=20_000)


class SafetyRules(BaseModel):
    avoid_profanity: bool = False
    avoid_medical_claims: bool = False
    avoid_financial_advice: bool = False
    notes: str = Field(default="", max_length=600)


class ClippingBrief(BaseModel):
    moment_types: list[str] = Field(default_factory=lambda: list(DEFAULT_INCLUDE_TYPES))
    custom_types: list[str] = Field(default_factory=list)
    instructions: str = Field(default="", max_length=4_000)
    include_topics: list[str] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)
    include_speakers: list[str] = Field(default_factory=list)
    exclude_topics: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    exclude_speakers: list[str] = Field(default_factory=list)
    clip_count: int = Field(default=8, ge=1, le=20)
    min_duration_ms: int = Field(default=15_000, ge=2_000, le=600_000)
    max_duration_ms: int = Field(default=90_000, ge=2_000, le=900_000)
    target_duration_ms: int | None = Field(default=None, ge=2_000, le=900_000)
    min_quality: float = Field(default=45.0, ge=0, le=100)
    platform: str = "tiktok"
    audience: str = Field(default="", max_length=200)
    hook: HookPolicy = Field(default_factory=HookPolicy)
    context: ContextRules = Field(default_factory=ContextRules)
    safety: SafetyRules = Field(default_factory=SafetyRules)

    @field_validator("moment_types")
    @classmethod
    def _known_types(cls, values: list[str]) -> list[str]:
        known = [value for value in _dedupe(values) if value in MOMENT_TYPES]
        if not known:
            raise ValueError("Choose at least one moment type")
        return known

    @field_validator(
        "include_topics", "include_keywords", "include_speakers",
        "exclude_topics", "exclude_keywords", "exclude_speakers", "custom_types",
    )
    @classmethod
    def _clean_lists(cls, values: list[str]) -> list[str]:
        return _dedupe(values)

    @field_validator("platform")
    @classmethod
    def _known_platform(cls, value: str) -> str:
        return value if value in PLATFORMS else "other"

    @model_validator(mode="after")
    def _durations(self) -> "ClippingBrief":
        if self.max_duration_ms <= self.min_duration_ms:
            raise ValueError("Maximum duration must be greater than the minimum")
        if self.target_duration_ms is not None:
            self.target_duration_ms = max(self.min_duration_ms, min(self.target_duration_ms, self.max_duration_ms))
        return self

    def duration_bands(self) -> tuple[tuple[int, int, str], ...]:
        """Translate the brief's range into candidate-generation bands.

        A band is only included when it overlaps the requested range; a custom
        range becomes a single band whose edges are clamped to the brief.
        """
        from clipforge.ranking.generate import DURATION_BANDS

        chosen = tuple(
            (max(minimum, self.min_duration_ms), min(maximum, self.max_duration_ms), name)
            for minimum, maximum, name in DURATION_BANDS
            if maximum > self.min_duration_ms and minimum < self.max_duration_ms
        )
        if chosen:
            return chosen
        return ((self.min_duration_ms, self.max_duration_ms, "custom"),)

    def describe(self) -> str:
        labels = [MOMENT_TYPES.get(value, value) for value in self.moment_types]
        labels += [f"custom: {value}" for value in self.custom_types]
        return ", ".join(labels)


def brief_from_legacy(
    goal: str,
    clip_count: int,
    duration_band: str,
    *,
    instructions: str | None = None,
) -> ClippingBrief:
    """Map the original analysis payload onto a brief without losing meaning."""
    by_goal = {
        "funny": (["funny", "joke"], 10_000, 45_000),
        "emotional_stories": (["emotional", "storytelling"], 20_000, 90_000),
        "educational": (["practical_tip", "strong_insight"], 20_000, 75_000),
        "tutorials": (["practical_tip"], 20_000, 90_000),
        "debate": (["controversial", "strong_insight"], 15_000, 60_000),
        "highlights": (["strong_insight", "memorable_quote"], 10_000, 45_000),
        "condensed": (["strong_insight", "storytelling"], 45_000, 120_000),
        "social_clips": (["memorable_quote", "best_advice"], 10_000, 45_000),
    }
    moment_types, minimum, maximum = by_goal.get(goal, (list(DEFAULT_INCLUDE_TYPES), 15_000, 90_000))
    band_to_range = {"short": (15_000, 30_000), "medium": (30_000, 60_000), "long": (60_000, 90_000)}
    if duration_band in band_to_range:
        minimum, maximum = band_to_range[duration_band]
    return ClippingBrief(
        moment_types=moment_types,
        instructions=(instructions or "").strip(),
        clip_count=max(1, min(20, clip_count)),
        min_duration_ms=minimum,
        max_duration_ms=maximum,
    )
