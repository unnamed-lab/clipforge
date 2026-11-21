"""What an additional discovery pass is allowed to look for.

Criteria are deliberately separate from the clipping brief. The brief describes
the project's editorial contract; criteria describe one targeted search inside
it. A discovery pass merges the two for ranking, stores both on the run, and
leaves the project brief untouched so the next pass starts from the same base.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from clipforge.briefs.spec import MOMENT_TYPES, ClippingBrief
from clipforge.durations import MAX_CUSTOM_DURATION_MS, MIN_CUSTOM_DURATION_MS
from clipforge.errors import ValidationFailed

# Evidence is part of the contract: a type is offered only when the pipeline can
# point at the signal it used. Gameplay highlights and fails need frame-level
# game state or incident labels, which this version does not produce.
DISCOVERY_MOMENT_TYPES: tuple[dict, ...] = (
    {"id": "funny", "label": "Funny", "evidence": "Laughter, jokes and emotional language in the transcript.", "available": True},
    {"id": "joke", "label": "Jokes & punchlines", "evidence": "Setup and punchline wording in the transcript.", "available": True},
    {"id": "reaction", "label": "Reactions", "evidence": "Reaction openers and emotional peaks in the transcript.", "available": True},
    {"id": "surprising", "label": "Surprising", "evidence": "Reversal and reveal markers such as \"turns out\".", "available": True},
    {"id": "strong_insight", "label": "Insights", "evidence": "Novel or dense explanatory language.", "available": True},
    {"id": "best_advice", "label": "Advice", "evidence": "Directive language such as \"you should\" or \"the best way\".", "available": True},
    {"id": "practical_tip", "label": "Practical tips", "evidence": "Step and how-to markers in the transcript.", "available": True},
    {"id": "tutorial", "label": "Tutorials", "evidence": "Step-by-step or instructional markers.", "available": True},
    {"id": "question_answer", "label": "Q&A", "evidence": "A question followed by a substantive answer.", "available": True},
    {"id": "audience_question", "label": "Audience questions", "evidence": "A question addressed to or from the audience.", "available": True},
    {"id": "emotional", "label": "Emotional stories", "evidence": "Emotional language inside a narrative.", "available": True},
    {"id": "storytelling", "label": "Stories", "evidence": "Anecdote markers and a narrative arc.", "available": True},
    {"id": "controversial", "label": "Controversial", "evidence": "Disagreement and contrarian markers.", "available": True},
    {"id": "memorable_quote", "label": "Quotes", "evidence": "Quotable sentence shapes.", "available": True},
    {"id": "strong_hook", "label": "Strong openings", "evidence": "Curiosity or question in the opening sentence.", "available": True},
    {"id": "gameplay_highlight", "label": "Gameplay highlights", "evidence": "Needs frame-level game state. Not available from this transcript-only pipeline.", "available": False},
    {"id": "fail", "label": "Fails & close calls", "evidence": "Needs visual incident detection. Transcript words alone are ambiguous.", "available": False},
)
_BY_ID = {entry["id"]: entry for entry in DISCOVERY_MOMENT_TYPES}
_AVAILABLE = {entry["id"] for entry in DISCOVERY_MOMENT_TYPES if entry["available"]}


def _dedupe(values: list[str], limit: int = 40) -> list[str]:
    seen: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split()).strip()
        if cleaned and cleaned.lower() not in {item.lower() for item in seen}:
            seen.append(cleaned[:80])
    return seen[:limit]


class DiscoveryCriteria(BaseModel):
    """One targeted search. ``count`` is how many *new* moments to add."""

    count: int = Field(default=10, ge=1)
    moment_types: list[str] = Field(default_factory=list)
    include_topics: list[str] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)
    include_speakers: list[str] = Field(default_factory=list)
    source_start_ms: int | None = Field(default=None, ge=0)
    source_end_ms: int | None = Field(default=None, ge=0)
    min_duration_ms: int | None = Field(default=None, ge=MIN_CUSTOM_DURATION_MS, le=MAX_CUSTOM_DURATION_MS)
    max_duration_ms: int | None = Field(default=None, ge=MIN_CUSTOM_DURATION_MS, le=MAX_CUSTOM_DURATION_MS)
    custom_request: str = Field(default="", max_length=600)
    min_quality: float | None = Field(default=None, ge=0, le=100)
    diversity: Literal["balanced", "chronological", "quality"] = "balanced"
    revisit_underexplored: bool = True
    exclude_existing: bool = True

    @field_validator(
        "moment_types", "include_topics", "include_keywords", "include_speakers",
    )
    @classmethod
    def _clean_lists(cls, values: list[str]) -> list[str]:
        return _dedupe(values)

    @field_validator("custom_request")
    @classmethod
    def _clean_request(cls, value: str) -> str:
        return " ".join(value.split())[:600]

    @model_validator(mode="after")
    def _ordered(self) -> "DiscoveryCriteria":
        if (
            self.source_start_ms is not None
            and self.source_end_ms is not None
            and self.source_end_ms <= self.source_start_ms
        ):
            raise ValueError("The source range must end after it starts")
        if (
            self.min_duration_ms is not None
            and self.max_duration_ms is not None
            and self.max_duration_ms <= self.min_duration_ms
        ):
            raise ValueError("The maximum duration must be greater than the minimum")
        return self

    def key(self) -> str:
        payload = self.model_dump()
        payload["moment_types"] = sorted(payload.get("moment_types") or [])
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def normalise_criteria(
    criteria: DiscoveryCriteria,
    *,
    source_duration_ms: int,
    max_count: int,
    speakers_available: bool,
) -> tuple[DiscoveryCriteria, list[str]]:
    """Clamp a request to what this source can actually answer."""
    warnings: list[str] = []
    values = criteria.model_dump()

    requested_count = int(values["count"])
    values["count"] = max(1, min(requested_count, max(1, max_count)))
    if values["count"] != requested_count:
        warnings.append(
            f"The app allows at most {max(1, max_count)} new moments per search, so the request was limited."
        )

    requested_types = list(values.get("moment_types") or [])
    supported = [value for value in requested_types if value in _AVAILABLE]
    unsupported = [value for value in requested_types if value not in _AVAILABLE]
    if unsupported:
        labels = ", ".join(_BY_ID.get(value, {}).get("label", value) for value in unsupported)
        warnings.append(
            f"{labels} cannot be targeted from this source: that evidence is not produced yet. "
            "The request will search the supported types instead."
        )
    values["moment_types"] = supported

    if values.get("include_speakers") and not speakers_available:
        raise ValidationFailed(
            "This transcript has no speaker labels, so it cannot target a speaker. "
            "Remove the speaker filter or run a source with diarisation.",
            code="speaker_labels_unavailable",
        )

    start = values.get("source_start_ms")
    end = values.get("source_end_ms")
    if start is not None:
        values["source_start_ms"] = max(0, min(int(start), max(0, source_duration_ms - 1_000)))
    if end is not None:
        values["source_end_ms"] = max(1_000, min(int(end), source_duration_ms))
    if (
        values.get("source_start_ms") is not None
        and values.get("source_end_ms") is not None
        and values["source_end_ms"] - values["source_start_ms"] < MIN_CUSTOM_DURATION_MS
    ):
        raise ValidationFailed(
            "Choose a source range of at least two seconds.",
            code="invalid_source_range",
        )

    minimum = values.get("min_duration_ms")
    maximum = values.get("max_duration_ms")
    if minimum is not None:
        values["min_duration_ms"] = max(MIN_CUSTOM_DURATION_MS, min(int(minimum), MAX_CUSTOM_DURATION_MS))
    if maximum is not None:
        values["max_duration_ms"] = max(MIN_CUSTOM_DURATION_MS, min(int(maximum), MAX_CUSTOM_DURATION_MS))
    if (
        values.get("min_duration_ms") is not None
        and values.get("max_duration_ms") is not None
        and values["max_duration_ms"] <= values["min_duration_ms"]
    ):
        raise ValidationFailed(
            "The maximum duration must be greater than the minimum.",
            code="invalid_duration_range",
        )

    return DiscoveryCriteria.model_validate(values), warnings


def build_discovery_brief(base: ClippingBrief, criteria: DiscoveryCriteria) -> ClippingBrief:
    """Merge one targeted search onto the project's editorial contract."""
    payload = base.model_dump()
    payload["clip_count"] = criteria.count
    if criteria.moment_types:
        payload["moment_types"] = criteria.moment_types
    payload["include_topics"] = _dedupe(base.include_topics + criteria.include_topics)
    payload["include_keywords"] = _dedupe(base.include_keywords + criteria.include_keywords)
    payload["include_speakers"] = _dedupe(base.include_speakers + criteria.include_speakers)
    if criteria.custom_request:
        combined = f"{base.instructions.strip()} {criteria.custom_request}".strip()
        payload["instructions"] = combined[:4_000]
    if criteria.min_quality is not None:
        payload["min_quality"] = criteria.min_quality

    if criteria.min_duration_ms is not None or criteria.max_duration_ms is not None:
        minimum = criteria.min_duration_ms or base.min_duration_ms
        maximum = criteria.max_duration_ms or base.max_duration_ms
        if maximum <= minimum:
            raise ValidationFailed(
                "The duration range does not overlap the project's current range.",
                code="invalid_duration_range",
            )
        payload["duration_preset"] = "custom"
        payload["min_duration_ms"] = minimum
        payload["max_duration_ms"] = maximum
        payload["target_duration_ms"] = None

    return ClippingBrief.model_validate(payload)


def moment_type_label(moment_type: str) -> str:
    return _BY_ID.get(moment_type, {}).get("label") or MOMENT_TYPES.get(moment_type, moment_type)
