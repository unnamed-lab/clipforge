from __future__ import annotations

from clipforge.ranking.dedupe import SelectionResult, select_candidates
from clipforge.ranking.features import COMPONENT_LABELS, Component, FeatureSet
from clipforge.ranking.generate import DURATION_BANDS, CandidateDraft, generate_candidates
from clipforge.ranking.score import (
    BASELINE_VERSION,
    GOALS,
    GOAL_WEIGHTS,
    ScoredCandidate,
    score_candidates,
    weights_for,
)
from clipforge.ranking.sentences import Sentence, TranscriptModel, build_transcript_model, tokenize

__all__ = [
    "BASELINE_VERSION",
    "COMPONENT_LABELS",
    "CandidateDraft",
    "Component",
    "DURATION_BANDS",
    "FeatureSet",
    "GOALS",
    "GOAL_WEIGHTS",
    "ScoredCandidate",
    "SelectionResult",
    "Sentence",
    "TranscriptModel",
    "build_transcript_model",
    "generate_candidates",
    "score_candidates",
    "select_candidates",
    "tokenize",
    "weights_for",
]
