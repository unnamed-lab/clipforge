"""Explainable feature extraction for candidate ranking.

Every component returns a 0..1 score *and* the evidence behind it, so the UI can show
why a moment surfaced instead of a bare number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clipforge.ranking.lexicon import (
    ANECDOTE_MARKERS,
    ANAPHORA_STARTS,
    CONTRASTIVE_STARTS,
    CURIOSITY_MARKERS,
    DEBATE_MARKERS,
    EMOTION_WORDS,
    FILLER_STARTS,
    INTENSIFIERS,
    QUESTION_WORDS,
    QUOTE_SHAPES,
    SECOND_PERSON,
    STOPWORDS,
    TUTORIAL_MARKERS,
    WEAK_ENDINGS,
)
from clipforge.ranking.generate import CandidateDraft
from clipforge.ranking.sentences import Sentence, TranscriptModel, tokenize

COMPONENT_LABELS = {
    "hook": "Hook strength",
    "coherence": "Standalone coherence",
    "completeness": "Story completeness",
    "novelty": "Novelty and specificity",
    "density": "Information density",
    "emotion": "Emotional or comedic impact",
    "quotability": "Quotability",
    "boundary": "Natural boundaries",
    "context": "Context independence",
    "risk": "Low misrepresentation risk",
}


@dataclass
class Component:
    score: float
    reason: str

    def to_dict(self) -> dict:
        return {"score": round(self.score * 100, 1), "reason": self.reason}


@dataclass
class FeatureSet:
    components: dict[str, Component]
    warnings: list[str] = field(default_factory=list)
    category: str = "insight"


def _sentences_in(draft: CandidateDraft, model: TranscriptModel) -> list[Sentence]:
    return model.sentences[draft.sentence_start : draft.sentence_end + 1]


def _content_tokens(text: str) -> list[str]:
    return [token for token in tokenize(text) if token not in STOPWORDS and len(token) > 2]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _first_words(text: str, count: int = 6) -> str:
    return " ".join(text.split()[:count]).lower().strip(" ,.")


def score_hook(draft: CandidateDraft, sentences: list[Sentence]) -> tuple[Component, list[str]]:
    first = sentences[0].text.strip()
    lowered = first.lower()
    score = 0.42
    reasons: list[str] = []
    warnings: list[str] = []

    if first.rstrip().endswith("?"):
        score += 0.18
        reasons.append("opens with a direct question")
    if any(marker in lowered for marker in CURIOSITY_MARKERS):
        score += 0.16
        reasons.append("opens with a curiosity or promise cue")
    if any(ch.isdigit() for ch in first):
        score += 0.08
        reasons.append("opens with a concrete number")
    words = tokenize(first)
    if words and words[0] in SECOND_PERSON:
        score += 0.10
        reasons.append("addresses the viewer directly")
    if 4 <= len(words) <= 14:
        score += 0.07
        reasons.append("opening sentence is short and clear")
    if any(lowered.startswith(filler) for filler in FILLER_STARTS):
        score -= 0.22
        warnings.append("Opens with filler; the first second is weak.")
    if len(words) < 4:
        score -= 0.15
        warnings.append("The opening line is very short and may lack context.")

    return Component(_clamp(score), reasons[0] if reasons else "Straightforward opening line"), warnings


def score_coherence(sentences: list[Sentence], draft: CandidateDraft) -> tuple[Component, list[str]]:
    if not sentences:
        return Component(0.3, "No transcript available for this span"), []
    terminated = sum(1 for s in sentences if s.ends_with_terminator) / len(sentences)
    starts_clean = 1.0 if draft.gap_before_ms >= 300 or sentences[0].index == 0 else 0.5
    starts_clean -= 0.2 if any(sentences[0].text.lower().startswith(m) for m in CONTRASTIVE_STARTS) else 0.0
    length_factor = 1.0 if 2 <= len(sentences) <= 14 else 0.7
    score = _clamp(0.5 * terminated + 0.3 * starts_clean + 0.2 * length_factor)
    reason = (
        "Starts on a clean thought boundary and holds together on its own"
        if score >= 0.7
        else "Partly self-contained; some sentences lean on what came before"
    )
    return Component(score, reason), []


def score_completeness(sentences: list[Sentence]) -> tuple[Component, list[str]]:
    if not sentences:
        return Component(0.3, "No transcript available for this span"), []
    last = sentences[-1]
    last_words = tokenize(last.text)
    score = 0.45
    reasons: list[str] = []
    warnings: list[str] = []
    if last.ends_with_terminator:
        score += 0.35
        reasons.append("ends on a completed thought")
    else:
        warnings.append("Ends mid-thought; extend the clip slightly to reach the conclusion.")
    if last_words and last_words[-1] in WEAK_ENDINGS:
        score -= 0.25
        warnings.append("Ends on a linking word, so the conclusion is outside the clip.")
    if len(sentences) >= 2:
        score += 0.15
        reasons.append("contains a full idea rather than a fragment")
    return Component(_clamp(score), reasons[0] if reasons else "Reasonable ending"), warnings


def score_novelty(model: TranscriptModel, text: str) -> tuple[Component, list[str]]:
    tokens = tokenize(text)
    if not tokens:
        return Component(0.3, "No transcript available for this span"), []
    content = [t for t in tokens if t not in STOPWORDS]
    unique_ratio = len(set(content)) / max(1, len(content))
    rare_ratio = model.rare_word_ratio(text)
    numbers = sum(1 for token in tokens if any(ch.isdigit() for ch in token))
    score = _clamp(0.35 * min(1.0, unique_ratio * 1.6) + 0.45 * min(1.0, rare_ratio * 5.0) + 0.20 * min(1.0, numbers / 3))
    if score >= 0.65:
        reason = "Uses specific language and details rather than generic filler"
    elif score >= 0.45:
        reason = "Reasonably specific, with some general framing"
    else:
        reason = "Fairly generic wording for this speaker"
    return Component(score, reason), []


def score_density(draft: CandidateDraft, text: str) -> tuple[Component, list[str]]:
    tokens = tokenize(text)
    duration_s = max(0.5, draft.duration_ms / 1000)
    speech_s = max(0.5, draft.speech_ms / 1000)
    content = [t for t in tokens if t not in STOPWORDS]
    per_speech_second = len(content) / speech_s
    silence_ratio = draft.silence_ratio
    score = _clamp(min(1.0, per_speech_second / 2.2) * 0.75 + (1.0 - min(1.0, silence_ratio * 2.0)) * 0.25)
    warnings: list[str] = []
    if silence_ratio > 0.4:
        warnings.append("Over 40% of this clip is silence or pauses.")
    reason = (
        "Dense with concrete content, little dead air"
        if score >= 0.65
        else "Some padding or silence inside the clip"
    )
    return Component(score, reason), warnings


def score_emotion(text: str, sentences: list[Sentence]) -> tuple[Component, list[str]]:
    lowered = text.lower()
    tokens = tokenize(lowered)
    if not tokens:
        return Component(0.3, "No transcript available for this span"), []
    hits = sum(1 for token in tokens if token in EMOTION_WORDS)
    intensifiers = sum(1 for token in tokens if token in INTENSIFIERS)
    exclamations = text.count("!")
    laughter = lowered.count("(laugh") + lowered.count("[laugh") + lowered.count("haha")
    anecdotes = sum(1 for marker in ANECDOTE_MARKERS if marker in lowered)
    raw = hits / len(tokens) * 6 + intensifiers / len(tokens) * 3 + exclamations * 0.12 + laughter * 0.3 + anecdotes * 0.2
    score = _clamp(0.25 + min(0.75, raw))
    if anecdotes:
        reason = "Carries a personal story, which holds attention"
    elif hits or laughter:
        reason = "Emotionally loaded language lifts this moment"
    else:
        reason = "Measured, informational tone"
    return Component(score, reason), []


def score_quotability(sentences: list[Sentence]) -> tuple[Component, list[str]]:
    score = 0.3
    reason = "No obviously quotable line"
    for sentence in sentences:
        words = tokenize(sentence.text)
        lowered = sentence.text.lower()
        if 4 <= len(words) <= 12 and not sentence.text.rstrip().endswith("?"):
            score += 0.4
            reason = "Contains a short, self-contained line that survives as a quote"
            break
    if any(marker in " ".join(s.text.lower() for s in sentences) for marker in QUOTE_SHAPES):
        score += 0.25
        reason = "Uses an assertive claim that reads well out of context"
    if sentences and tokenize(sentences[0].text)[:1] and tokenize(sentences[0].text)[0] in {"stop", "never", "always", "don't"}:
        score += 0.1
    return Component(_clamp(score), reason), []


def score_boundary(draft: CandidateDraft, sentences: list[Sentence]) -> tuple[Component, list[str]]:
    score = 0.4
    if draft.gap_before_ms >= 300:
        score += 0.25
    elif draft.gap_before_ms >= 120:
        score += 0.12
    if sentences and sentences[-1].ends_with_terminator:
        score += 0.2
    if draft.gap_after_ms >= 250:
        score += 0.15
    reason = (
        "Cut points sit on natural pauses"
        if score >= 0.7
        else "Cut points are usable but not perfectly clean"
    )
    return Component(_clamp(score), reason), []


def score_context(sentences: list[Sentence]) -> tuple[Component, list[str]]:
    if not sentences:
        return Component(0.5, "No transcript available for this span"), []
    first_words = _first_words(sentences[0].text, 4)
    score = 0.85
    warnings: list[str] = []
    if any(first_words.startswith(marker) for marker in ANAPHORA_STARTS):
        score -= 0.4
        warnings.append("The opening references something said earlier; viewers may need context.")
    if any(first_words.startswith(marker) for marker in CONTRASTIVE_STARTS):
        score -= 0.2
        warnings.append("Starts with a contrast, which implies a missing setup.")
    pronouns = sum(1 for s in sentences[:2] for t in tokenize(s.text) if t in {"it", "this", "that", "they", "them", "he", "she"})
    if pronouns >= 4:
        score -= 0.15
        warnings.append("Heavy pronoun use in the opening two sentences.")
    reason = (
        "Readable without earlier context"
        if score >= 0.7
        else "Needs a little context from earlier in the source"
    )
    return Component(_clamp(score), reason), warnings


def score_risk(draft: CandidateDraft, sentences: list[Sentence], text: str) -> tuple[Component, list[str]]:
    if not sentences:
        return Component(0.5, "No transcript available for this span"), []
    score = 0.9
    warnings: list[str] = []
    lowered = sentences[0].text.lower()
    if any(lowered.startswith(marker) for marker in CONTRASTIVE_STARTS):
        score -= 0.25
        warnings.append("Starts on a contrast: the sentence it argues against is not in the clip.")
    if re.search(r"\bif\b", lowered) and not re.search(r"\bthen\b|\bso\b", text.lower()):
        score -= 0.15
        warnings.append("Opens a conditional without showing the consequence.")
    if text.strip().endswith(","):
        score -= 0.2
    if len(tokenize(text)) < 8:
        score -= 0.1
    reason = (
        "Cannot easily be read as saying something the speaker did not say"
        if score >= 0.75
        else "The cut changes what the sentence was responding to"
    )
    return Component(_clamp(score), reason), warnings


def _category(draft: CandidateDraft, components: dict[str, Component], text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in DEBATE_MARKERS):
        return "debate"
    if any(marker in lowered for marker in TUTORIAL_MARKERS):
        return "tutorial"
    if components["emotion"].score >= 0.7 or "haha" in lowered:
        return "story" if any(marker in lowered for marker in ANECDOTE_MARKERS) else "emotional"
    if draft.first_sentence.strip().endswith("?") or any(marker in lowered for marker in CURIOSITY_MARKERS):
        return "insight"
    return "insight"


def extract_features(draft: CandidateDraft, model: TranscriptModel) -> FeatureSet:
    sentences = _sentences_in(draft, model)
    hook, hook_warnings = score_hook(draft, sentences)
    coherence, coherence_warnings = score_coherence(sentences, draft)
    completeness, completeness_warnings = score_completeness(sentences)
    novelty, _ = score_novelty(model, draft.text)
    density, density_warnings = score_density(draft, draft.text)
    emotion, _ = score_emotion(draft.text, sentences)
    quotability, _ = score_quotability(sentences)
    boundary, _ = score_boundary(draft, sentences)
    context, context_warnings = score_context(sentences)
    risk, risk_warnings = score_risk(draft, sentences, draft.text)

    components = {
        "hook": hook,
        "coherence": coherence,
        "completeness": completeness,
        "novelty": novelty,
        "density": density,
        "emotion": emotion,
        "quotability": quotability,
        "boundary": boundary,
        "context": context,
        "risk": risk,
    }
    warnings: list[str] = []
    for bucket in (
        hook_warnings,
        coherence_warnings,
        completeness_warnings,
        density_warnings,
        context_warnings,
        risk_warnings,
    ):
        for warning in bucket:
            if warning not in warnings:
                warnings.append(warning)

    return FeatureSet(
        components=components,
        warnings=warnings,
        category=_category(draft, components, draft.text),
    )
