"""Clipping brief: the structured definition of what the user actually wants.

Every download is not a quest for viral comedy. The brief is the contract between
the user's intent and candidate generation, scoring and hook selection, and it is
snapshotted onto each analysis run so an old result explains itself.
"""

from __future__ import annotations


from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from clipforge.durations import (
    DEFAULT_TOLERANCE_MS,
    DURATION_PRESETS,
    MAX_CUSTOM_DURATION_MS,
    MIN_CUSTOM_DURATION_MS,
    infer_preset,
    preset_range,
)

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
    "surprising": "Surprising moments",
    "question_answer": "Questions and answers",
    "tutorial": "Tutorials and how-tos",
    "strong_hook": "Strong openings",
    "gameplay_highlight": "Gameplay highlights",
    "fail": "Fails and close calls",
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


HOOK_STRATEGIES = (
    "question",
    "outcome",
    "curiosity",
    "story",
    "humour",
    "emotional",
    "contrarian",
    "chronological",
    "manual",
)
PACING_PROFILES = ("calm", "conversational", "balanced", "energetic", "fast")
EDITORIAL_STYLES = ("automatic", "chronological", "hook_first", "manual")
EDITORIAL_STRATEGIES = (
    "automatic",
    "cold_open",
    "in_medias_res",
    "result_first",
    "chronological",
    "question_answer",
    "story_escalation",
    "reaction_led",
    "montage",
    "loop",
    "open_loop",
    "manual",
)
FLASHBACK_MODES = ("automatic", "prefer", "avoid", "manual")
LOOP_MODES = ("automatic", "enabled", "disabled", "manual")
REACTION_MODES = ("automatic", "prefer", "avoid", "manual")
GENRES = (
    "auto",
    "podcast_interview",
    "comedy",
    "storytime",
    "education",
    "expert_advice",
    "debate",
    "emotional_inspirational",
    "product_demo",
    "livestream_highlights",
    "slide_webinar",
)
GENRE_ALIASES = {
    "podcast": "podcast_interview",
    "commentary": "podcast_interview",
    "emotional": "emotional_inspirational",
    "product": "product_demo",
    "livestream": "livestream_highlights",
    "webinar": "slide_webinar",
}

GENRE_GUIDANCE: dict[str, dict] = {
    "podcast_interview": {
        "label": "Podcast / interview",
        "preferred": ("question_answer", "cold_open", "chronological"),
        "hook": "question",
        "pacing": "conversational",
    },
    "comedy": {
        "label": "Comedy",
        "preferred": ("cold_open", "result_first", "chronological"),
        "hook": "humour",
        "pacing": "energetic",
    },
    "storytime": {
        "label": "Storytime",
        "preferred": ("cold_open", "story_escalation", "chronological"),
        "hook": "story",
        "pacing": "balanced",
    },
    "education": {
        "label": "Education",
        "preferred": ("question_answer", "chronological", "cold_open"),
        "hook": "question",
        "pacing": "balanced",
    },
    "expert_advice": {
        "label": "Expert advice",
        "preferred": ("result_first", "question_answer", "chronological"),
        "hook": "outcome",
        "pacing": "balanced",
    },
    "debate": {
        "label": "Debate",
        "preferred": ("result_first", "cold_open", "chronological"),
        "hook": "contrarian",
        "pacing": "energetic",
    },
    "emotional_inspirational": {
        "label": "Emotional / inspirational",
        "preferred": ("cold_open", "story_escalation", "chronological"),
        "hook": "emotional",
        "pacing": "conversational",
    },
    "product_demo": {
        "label": "Product demo",
        "preferred": ("result_first", "question_answer", "chronological"),
        "hook": "outcome",
        "pacing": "energetic",
    },
    "livestream_highlights": {
        "label": "Livestream highlights",
        "preferred": ("reaction_led", "cold_open", "chronological"),
        "hook": "curiosity",
        "pacing": "fast",
    },
    "slide_webinar": {
        "label": "Slide-based / webinar",
        "preferred": ("question_answer", "chronological"),
        "hook": "question",
        "pacing": "conversational",
    },
}
DURATION_PRESET_NAMES = ("short", "standard", "extended", "custom")


class HookPolicy(BaseModel):
    """The 1-3 second opening. Default on, because a cold open is the point."""

    enabled: bool = True
    min_ms: int = Field(default=1_000, ge=500, le=10_000)
    max_ms: int = Field(default=3_000, ge=1_000, le=15_000)
    allow_external: bool = True
    strict_chronological: bool = False
    required: bool = False
    max_distance_ms: int = Field(default=180_000, ge=0)
    strategies: list[str] = Field(
        default_factory=lambda: [value for value in HOOK_STRATEGIES if value != "manual"]
    )
    auto_select: bool = True
    min_score: float = Field(default=52.0, ge=0, le=100)
    max_candidates: int = Field(default=5, ge=1, le=10)
    review_when_weak: bool = True

    @field_validator("strategies")
    @classmethod
    def _known_strategies(cls, values: list[str]) -> list[str]:
        known = [value for value in _dedupe(values) if value in HOOK_STRATEGIES]
        if not known:
            raise ValueError("Choose at least one hook strategy")
        return known

    @model_validator(mode="after")
    def _ordered(self) -> "HookPolicy":
        if self.max_ms < self.min_ms:
            raise ValueError("Hook maximum must be at least the minimum")
        return self


class PacingPolicy(BaseModel):
    """How aggressively silence and filler may be removed.

    A profile supplies defaults; every field is optional so an individual client
    can deviate without cloning the whole policy. ``None`` means "use the
    profile value", which keeps old saved briefs behaving predictably.
    """

    profile: str = "balanced"
    remove_dead_air: bool | None = None
    remove_fillers: bool | None = None
    max_pause_ms: int | None = Field(default=None, ge=150, le=5_000)
    keep_pause_ms: int | None = Field(default=None, ge=0, le=1_500)
    max_trim_ms: int | None = Field(default=None, ge=0, le=10_000)
    min_segment_ms: int | None = Field(default=None, ge=300, le=10_000)
    pattern_interrupts: bool | None = None

    @field_validator("profile")
    @classmethod
    def _known_profile(cls, value: str) -> str:
        return value if value in PACING_PROFILES else "balanced"

    @model_validator(mode="after")
    def _coherent(self) -> "PacingPolicy":
        if self.keep_pause_ms is not None and self.max_pause_ms is not None:
            if self.keep_pause_ms >= self.max_pause_ms:
                raise ValueError("Pacing must keep less silence than it removes")
        if self.min_segment_ms is not None and self.min_segment_ms < 300:
            raise ValueError("Pacing segments must be at least 300 ms")
        return self


class EditorialPolicy(BaseModel):
    """Editorial preferences that shape structure, not the renderer.

    ``automatic`` means Clipforge decides and explains its choice. An explicit
    strategy is honoured only when the source actually supports it; otherwise
    the plan records why a chronological version was used instead.
    """

    style: str = "automatic"
    preferred_strategy: str = "automatic"
    genre: str = "auto"
    hook_preference: str = "auto"
    flashback: str = "automatic"
    loop_ending: str = "automatic"
    reaction_shots: str = "automatic"
    pattern_interrupt_intensity: Literal["off", "subtle", "balanced", "strong"] = "balanced"

    @field_validator("style")
    @classmethod
    def _style(cls, value: str) -> str:
        return value if value in EDITORIAL_STYLES else "automatic"

    @field_validator("preferred_strategy")
    @classmethod
    def _strategy(cls, value: str) -> str:
        return value if value in EDITORIAL_STRATEGIES else "automatic"

    @field_validator("genre")
    @classmethod
    def _genre(cls, value: str) -> str:
        value = GENRE_ALIASES.get(value, value)
        return value if value in GENRES else "auto"

    @field_validator("hook_preference")
    @classmethod
    def _hook_preference(cls, value: str) -> str:
        return value if value in (*HOOK_STRATEGIES, "auto") else "auto"

    @field_validator("flashback")
    @classmethod
    def _flashback(cls, value: str) -> str:
        return value if value in FLASHBACK_MODES else "automatic"

    @field_validator("loop_ending")
    @classmethod
    def _loop(cls, value: str) -> str:
        return value if value in LOOP_MODES else "automatic"

    @field_validator("reaction_shots")
    @classmethod
    def _reaction(cls, value: str) -> str:
        return value if value in REACTION_MODES else "automatic"


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
    clip_count: int = Field(default=8, ge=1, le=30)
    duration_preset: Literal["short", "standard", "extended", "custom"] = "extended"
    min_duration_ms: int = Field(default=60_000, ge=MIN_CUSTOM_DURATION_MS, le=MAX_CUSTOM_DURATION_MS)
    max_duration_ms: int = Field(default=90_000, ge=MIN_CUSTOM_DURATION_MS, le=MAX_CUSTOM_DURATION_MS)
    target_duration_ms: int | None = Field(
        default=None, ge=MIN_CUSTOM_DURATION_MS, le=MAX_CUSTOM_DURATION_MS
    )
    duration_tolerance_ms: int = Field(default=DEFAULT_TOLERANCE_MS, ge=0, le=15_000)
    min_quality: float = Field(default=45.0, ge=0, le=100)
    platform: str = "tiktok"
    audience: str = Field(default="", max_length=200)
    hook: HookPolicy = Field(default_factory=HookPolicy)
    editorial: EditorialPolicy = Field(default_factory=EditorialPolicy)
    pacing: PacingPolicy = Field(default_factory=PacingPolicy)
    context: ContextRules = Field(default_factory=ContextRules)
    safety: SafetyRules = Field(default_factory=SafetyRules)

    @model_validator(mode="before")
    @classmethod
    def _migrate_duration(cls, data):
        """Preserve saved custom ranges and default new briefs to Extended.

        Old briefs serialised ``min_duration_ms`` and ``max_duration_ms`` but had
        no preset. Treating those as the new default would silently turn a saved
        15-90 second brief into 60-90. Infer the preset from the saved pair, and
        only use Extended when the document has no duration information at all.
        """
        if not isinstance(data, dict):
            return data
        values = dict(data)
        if "duration_preset" in values:
            return values
        if "min_duration_ms" in values or "max_duration_ms" in values:
            minimum = int(values.get("min_duration_ms", 60_000))
            maximum = int(values.get("max_duration_ms", 90_000))
            values["duration_preset"] = infer_preset(minimum, maximum)
        else:
            values["duration_preset"] = "extended"
        return values

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
    def _genre_defaults(self) -> "ClippingBrief":
        """Apply genre guidance only where the user left the field untouched."""
        guide = GENRE_GUIDANCE.get(self.editorial.genre)
        if not guide:
            return self
        if self.pacing.profile == "balanced" and guide.get("pacing"):
            self.pacing.profile = guide["pacing"]
        if self.editorial.hook_preference == "auto" and guide.get("hook"):
            self.editorial.hook_preference = guide["hook"]
        return self

    @model_validator(mode="after")
    def _durations(self) -> "ClippingBrief":
        """Make the preset authoritative, except for an explicit custom range."""
        if self.duration_preset in DURATION_PRESETS:
            self.min_duration_ms, self.max_duration_ms = DURATION_PRESETS[self.duration_preset]
        if self.max_duration_ms <= self.min_duration_ms:
            raise ValueError("Maximum duration must be greater than the minimum")
        if self.target_duration_ms is not None:
            self.target_duration_ms = max(self.min_duration_ms, min(self.target_duration_ms, self.max_duration_ms))
        return self

    def duration_range(self):
        return preset_range(
            self.duration_preset,
            min_duration_ms=self.min_duration_ms,
            max_duration_ms=self.max_duration_ms,
            target_duration_ms=self.target_duration_ms,
            tolerance_ms=self.duration_tolerance_ms,
        )

    def duration_bands(self) -> tuple[tuple[int, int, str], ...]:
        """The target band(s), clamped to the selected preset or custom range."""
        if self.duration_preset == "custom":
            return ((self.min_duration_ms, self.max_duration_ms, "custom"),)
        minimum, maximum = DURATION_PRESETS[self.duration_preset]
        return ((minimum, maximum, self.duration_preset),)

    def generation_bands(self) -> tuple[tuple[int, int, str], ...]:
        """Generate the target range plus coherent alternatives for no-fit review.

        The extra bands are not padding: they are separate sentence-aligned windows,
        scored lower for duration fit and labelled as alternatives. Nothing is
        truncated or stretched to make one of them match the preset.
        """
        from clipforge.durations import generation_bands

        return generation_bands(
            self.duration_preset,
            min_duration_ms=self.min_duration_ms,
            max_duration_ms=self.max_duration_ms,
        )

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
    moment_types, _minimum, _maximum = by_goal.get(goal, (list(DEFAULT_INCLUDE_TYPES), 60_000, 90_000))
    band_to_preset = {
        "short": "short",
        "medium": "standard",
        "long": "extended",
        "auto": "extended",
    }
    return ClippingBrief(
        moment_types=moment_types,
        instructions=(instructions or "").strip(),
        clip_count=max(1, min(30, clip_count)),
        duration_preset=band_to_preset.get(duration_band, "extended"),
    )
