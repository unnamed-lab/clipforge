"""Deterministic baseline ranking.

Scores are weighted averages of the explainable components in ``features.py``.
They are estimates of content quality, not predictions of social performance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clipforge.ranking.features import COMPONENT_LABELS, FeatureSet, extract_features
from clipforge.ranking.generate import CandidateDraft
from clipforge.ranking.lexicon import FILLER_STARTS
from clipforge.ranking.sentences import TranscriptModel

BASELINE_VERSION = "baseline-1"

GOALS = (
    "best_moments",
    "social_clips",
    "educational",
    "funny",
    "debate",
    "emotional_stories",
    "tutorials",
    "highlights",
    "condensed",
    "custom",
)

_DEFAULT_WEIGHTS = {
    "hook": 0.16,
    "coherence": 0.12,
    "completeness": 0.12,
    "novelty": 0.09,
    "density": 0.10,
    "emotion": 0.10,
    "quotability": 0.10,
    "boundary": 0.08,
    "context": 0.07,
    "risk": 0.06,
}

GOAL_WEIGHTS: dict[str, dict[str, float]] = {
    "best_moments": _DEFAULT_WEIGHTS,
    "social_clips": {
        "hook": 0.24, "quotability": 0.18, "boundary": 0.12, "emotion": 0.12, "completeness": 0.10,
        "coherence": 0.08, "novelty": 0.06, "density": 0.04, "context": 0.04, "risk": 0.02,
    },
    "educational": {
        "density": 0.22, "completeness": 0.18, "coherence": 0.14, "novelty": 0.10, "hook": 0.10,
        "context": 0.10, "quotability": 0.06, "boundary": 0.05, "emotion": 0.03, "risk": 0.02,
    },
    "funny": {
        "emotion": 0.26, "quotability": 0.18, "hook": 0.16, "boundary": 0.10, "completeness": 0.10,
        "coherence": 0.06, "novelty": 0.05, "density": 0.04, "context": 0.03, "risk": 0.02,
    },
    "debate": {
        "novelty": 0.20, "hook": 0.18, "risk": 0.14, "emotion": 0.12, "quotability": 0.10,
        "completeness": 0.10, "coherence": 0.08, "density": 0.04, "boundary": 0.03, "context": 0.01,
    },
    "emotional_stories": {
        "emotion": 0.28, "completeness": 0.18, "coherence": 0.14, "hook": 0.12, "quotability": 0.08,
        "boundary": 0.07, "novelty": 0.06, "density": 0.03, "context": 0.02, "risk": 0.02,
    },
    "tutorials": {
        "density": 0.24, "completeness": 0.18, "context": 0.14, "coherence": 0.12, "hook": 0.10,
        "novelty": 0.08, "boundary": 0.06, "quotability": 0.04, "emotion": 0.02, "risk": 0.02,
    },
    "highlights": {
        "coherence": 0.18, "density": 0.16, "novelty": 0.14, "completeness": 0.12, "hook": 0.12,
        "emotion": 0.10, "quotability": 0.08, "boundary": 0.05, "context": 0.03, "risk": 0.02,
    },
    "condensed": {
        "coherence": 0.22, "density": 0.20, "completeness": 0.16, "novelty": 0.12, "hook": 0.10,
        "context": 0.08, "boundary": 0.06, "quotability": 0.03, "emotion": 0.02, "risk": 0.01,
    },
}


def weights_for(goal: str, overrides: dict[str, float] | None = None) -> dict[str, float]:
    weights = dict(GOAL_WEIGHTS.get(goal, _DEFAULT_WEIGHTS))
    if overrides:
        for key, value in overrides.items():
            if key in weights:
                try:
                    weights[key] = max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
    total = sum(weights.values()) or 1.0
    return {key: round(value / total, 4) for key, value in weights.items()}


@dataclass
class ScoredCandidate:
    draft: CandidateDraft
    features: FeatureSet
    overall: float
    title: str
    hook: str
    reason: str
    method: str = "baseline"
    method_version: str = BASELINE_VERSION
    duplicate_of: str | None = None
    weight_map: dict[str, float] = field(default_factory=dict)
    brief: dict = field(default_factory=dict)
    brief_relevance: float | None = None
    hook_detail: dict = field(default_factory=dict)
    summary: str = ""

    @property
    def start_ms(self) -> int:
        return self.draft.start_ms

    @property
    def end_ms(self) -> int:
        return self.draft.end_ms

    @property
    def duration_ms(self) -> int:
        return self.draft.duration_ms

    @property
    def excerpt(self) -> str:
        return self.draft.text

    @property
    def warnings(self) -> list[str]:
        return self.features.warnings

    @property
    def category(self) -> str:
        return self.features.category

    def scores_payload(self) -> dict:
        payload = {
            "overall": round(self.overall, 1),
            "components": {key: component.to_dict() for key, component in self.features.components.items()},
            "weights": self.weight_map,
            "method": self.method,
            "method_version": self.method_version,
            "estimated": True,
            "note": "Estimated editorial quality from transcript features, not a prediction of engagement.",
        }
        if self.brief:
            payload["brief"] = self.brief
        return payload


def _clean_title(text: str) -> str:
    candidate = " ".join(text.split()).strip(" \"'“”‘’")
    lowered = candidate.lower()
    for filler in sorted(FILLER_STARTS, key=len, reverse=True):
        if lowered.startswith(filler + " ") or lowered.startswith(filler + ","):
            candidate = candidate[len(filler):].lstrip(" ,")
            break
    candidate = candidate.strip(" ,;:-–—")
    if not candidate:
        return "Untitled moment"
    candidate = candidate[0].upper() + candidate[1:]
    if len(candidate) > 78:
        cut = candidate[:78].rsplit(" ", 1)[0]
        candidate = cut + "…"
    return candidate


def _build_reason(candidate: ScoredCandidate) -> str:
    weights = candidate.weight_map
    ranked = sorted(
        candidate.features.components.items(),
        key=lambda item: item[1].score * weights.get(item[0], 0),
        reverse=True,
    )
    positives = [component.reason for _, component in ranked[:2] if component.score >= 0.5]
    weakest_name, weakest = min(candidate.features.components.items(), key=lambda item: item[1].score)
    parts: list[str] = []
    if positives:
        parts.append(positives[0].rstrip(".") + ".")
    if len(positives) > 1:
        parts.append(positives[1].rstrip(".") + ".")
    if weakest.score < 0.55:
        parts.append(f"Watch out: {COMPONENT_LABELS[weakest_name].lower()} is the weak point — {weakest.reason.lower().rstrip('.')}.")
    else:
        parts.append("No major structural issues detected.")
    return " ".join(parts)


def score_candidates(
    drafts: list[CandidateDraft],
    model: TranscriptModel,
    *,
    goal: str = "best_moments",
    weight_overrides: dict[str, float] | None = None,
) -> list[ScoredCandidate]:
    weights = weights_for(goal, weight_overrides)
    scored: list[ScoredCandidate] = []
    for draft in drafts:
        features = extract_features(draft, model)
        total = sum(weights[key] * component.score for key, component in features.components.items())
        candidate = ScoredCandidate(
            draft=draft,
            features=features,
            overall=round(total * 100, 1),
            title=_clean_title(draft.first_sentence),
            hook=" ".join(draft.first_sentence.split()[:24]).strip(),
            reason="",
            weight_map=weights,
        )
        candidate.reason = _build_reason(candidate)
        scored.append(candidate)
    scored.sort(key=lambda item: item.overall, reverse=True)
    return scored
