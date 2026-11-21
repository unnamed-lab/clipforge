"""Deterministic brief matching.

Free-text instructions and moment types are interpreted with explainable lexical
signals. When an LLM is configured the same brief is also sent to it; the baseline
below is what makes the feature work with no model, no key and no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clipforge.briefs.spec import ClippingBrief
from clipforge.ranking.lexicon import (
    ANECDOTE_MARKERS,
    DEBATE_MARKERS,
    QUESTION_WORDS,
    STOPWORDS,
    TUTORIAL_MARKERS,
)
from clipforge.ranking.sentences import tokenize

PROFANITY = {
    "fuck", "fucking", "shit", "bullshit", "bitch", "asshole", "bastard", "damn", "crap",
}
MEDICAL_CLAIMS = ("cure", "cures", "cured", "treats", "diagnose", "diagnosis", "medical advice", "miracle")
FINANCIAL_CLAIMS = (
    "guaranteed return", "guaranteed profit", "risk free", "risk-free", "get rich",
    "financial advice", "buy this stock", "sure thing",
)
REACTION_STARTS = ("oh", "wow", "whoa", "no way", "yes", "what", "wait", "look")
ADVICE_MARKERS = ("you should", "you need to", "never ", "always ", "the best way", "my advice", "here's how")
SURPRISE_MARKERS = (
    "turns out", "it turns out", "surprisingly", "plot twist", "i couldn't believe",
    "you won't believe", "no way", "unexpected", "out of nowhere", "the weird part",
)
HOOK_MARKERS = (
    "nobody tells you", "here's why", "here's how", "the reason", "most people",
    "everyone thinks", "what nobody", "the truth about", "i used to think",
)
FAIL_MARKERS = ("almost", "close call", "nearly", "failed", "failure", "messed up", "mistake")
GAMEPLAY_MARKERS = ("boss", "level", "respawn", "speedrun", "combo", "loot", "quest")
INSTRUCTION_STOPWORDS = STOPWORDS | {
    "find", "clips", "clip", "want", "wants", "please", "make", "only", "avoid", "prioritise",
    "prioritize", "judge", "quality", "moments", "moment", "should", "must", "good", "great",
}


def _contains_phrase(text: str, phrase: str) -> bool:
    needle = " ".join(phrase.lower().split())
    return bool(needle) and needle in text


def _term_coverage(text: str, terms: list[str]) -> tuple[float, list[str], list[str]]:
    if not terms:
        return 1.0, [], []
    matched = [term for term in terms if _contains_phrase(text, term)]
    missing = [term for term in terms if term not in matched]
    return len(matched) / len(terms), matched, missing


def instruction_terms(instructions: str) -> list[str]:
    """Pull the concrete terms out of free text so baseline ranking can use them."""
    words = [word for word in tokenize(instructions) if len(word) >= 4 and word not in INSTRUCTION_STOPWORDS]
    counted: dict[str, int] = {}
    for word in words:
        counted[word] = counted.get(word, 0) + 1
    ordered = sorted(counted.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ordered[:12]]


def moment_type_matches(moment_type: str, text: str, category: str, components: dict[str, float]) -> bool:
    lowered = text.lower()
    first = lowered.strip().split(".")[0]
    if moment_type in {"funny", "joke"}:
        return components.get("emotion", 0) >= 0.55 or any(marker in lowered for marker in ("haha", "laugh", "joke"))
    if moment_type == "reaction":
        return components.get("emotion", 0) >= 0.45 and any(first.startswith(word) for word in REACTION_STARTS)
    if moment_type == "best_advice":
        return any(marker in lowered for marker in ADVICE_MARKERS) or components.get("quotability", 0) >= 0.65
    if moment_type == "practical_tip":
        return category == "tutorial" or any(marker in lowered for marker in TUTORIAL_MARKERS)
    if moment_type == "tutorial":
        return category == "tutorial" or any(marker in lowered for marker in TUTORIAL_MARKERS)
    if moment_type == "controversial":
        return category == "debate" or any(marker in lowered for marker in DEBATE_MARKERS)
    if moment_type == "emotional":
        return category in {"emotional", "story"} and components.get("emotion", 0) >= 0.5
    if moment_type == "storytelling":
        return category in {"story", "emotional"} or any(marker in lowered for marker in ANECDOTE_MARKERS)
    if moment_type == "strong_insight":
        return components.get("novelty", 0) >= 0.5 or components.get("density", 0) >= 0.5
    if moment_type == "memorable_quote":
        return components.get("quotability", 0) >= 0.55
    if moment_type == "audience_question":
        return "?" in text or any(word in lowered.split()[:3] for word in QUESTION_WORDS)
    if moment_type == "surprising":
        return any(marker in lowered for marker in SURPRISE_MARKERS) or components.get("novelty", 0) >= 0.65
    if moment_type == "question_answer":
        # A question alone is a prompt, not an answer. Require enough speech for
        # the response and a question mark or question word in the opening.
        tokens = tokenize(text)
        asks = "?" in text or any(word in lowered.split()[:4] for word in QUESTION_WORDS)
        return asks and len(tokens) >= 18
    if moment_type == "strong_hook":
        opening = " ".join(lowered.split()[:18])
        return any(marker in opening for marker in HOOK_MARKERS) or opening.rstrip().endswith("?")
    if moment_type in {"gameplay_highlight", "fail"}:
        # These require frame-level game state or an incident label. Transcript
        # words such as "boss" or "failed" are not enough evidence to claim one.
        return False
    if moment_type == "custom":
        return False
    return False


@dataclass
class BriefMatch:
    relevance: float
    matched_types: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    missing_terms: list[str] = field(default_factory=list)
    blocked: bool = False
    blocked_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "relevance": round(self.relevance, 1),
            "matched_types": self.matched_types,
            "matched_terms": self.matched_terms,
            "missing_terms": self.missing_terms,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "warnings": self.warnings,
            "reasons": self.reasons,
        }


def evaluate_brief(
    brief: ClippingBrief,
    *,
    text: str,
    category: str,
    components: dict[str, float],
) -> BriefMatch:
    lowered = " ".join(text.lower().split())

    for term in brief.exclude_keywords + brief.exclude_topics:
        if _contains_phrase(lowered, term):
            return BriefMatch(
                relevance=0.0,
                blocked=True,
                blocked_reason=f'Excluded term "{term}" appears in this moment',
            )

    safety = brief.safety
    if safety.avoid_profanity:
        hit = next((word for word in PROFANITY if word in tokenize(lowered)), None)
        if hit is not None:
            return BriefMatch(relevance=0.0, blocked=True, blocked_reason=f"Profanity ({hit}) violates the brief")
    if safety.avoid_medical_claims and any(phrase in lowered for phrase in MEDICAL_CLAIMS):
        return BriefMatch(relevance=0.0, blocked=True, blocked_reason="Medical claim violates the brief")
    if safety.avoid_financial_advice and any(phrase in lowered for phrase in FINANCIAL_CLAIMS):
        return BriefMatch(relevance=0.0, blocked=True, blocked_reason="Financial advice violates the brief")

    matched_types = [
        moment_type for moment_type in brief.moment_types
        if moment_type_matches(moment_type, text, category, components)
    ]
    type_score = 1.0 if not brief.moment_types else min(1.0, len(matched_types) / min(2, len(brief.moment_types)))

    include_terms = brief.include_keywords + brief.include_topics
    include_coverage, matched_terms, missing_terms = _term_coverage(lowered, include_terms)

    hints = instruction_terms(brief.instructions)
    hint_coverage, hint_matched, _ = _term_coverage(lowered, hints)

    relevance = 42.0
    reasons: list[str] = []
    warnings: list[str] = []

    if brief.moment_types:
        if matched_types:
            from clipforge.briefs.spec import MOMENT_TYPES

            labels = ", ".join(MOMENT_TYPES.get(value, value).lower() for value in matched_types[:3])
            relevance += 22.0 * type_score
            reasons.append(f"Matches the requested moment type ({labels})")
        else:
            relevance -= 10.0
            warnings.append("No requested moment type was detected in this passage")

    if include_terms:
        relevance += 20.0 * include_coverage
        if matched_terms:
            reasons.append("Covers requested topics: " + ", ".join(matched_terms[:4]))
        if missing_terms:
            warnings.append("Missing requested topics: " + ", ".join(missing_terms[:4]))

    if hints:
        relevance += 12.0 * hint_coverage
        if hint_matched:
            reasons.append("Instruction keywords present: " + ", ".join(hint_matched[:5]))
        warnings.append(
            "Free-text instructions were applied as keyword hints by the offline ranker; "
            "configure a language model for semantic interpretation."
        )

    if brief.context.require_standalone and components.get("coherence", 0) < 0.45:
        relevance -= 8.0
        warnings.append("Passage may not stand alone without prior context")

    if brief.min_quality > 0 and components.get("risk", 0) < 0.4:
        warnings.append("Possible misrepresentation risk if trimmed tightly")

    return BriefMatch(
        relevance=max(0.0, min(100.0, relevance)),
        matched_types=matched_types,
        matched_terms=matched_terms,
        missing_terms=missing_terms,
        warnings=warnings,
        reasons=reasons,
    )


def blend_with_brief(base_score: float, relevance: float) -> float:
    """Keep editorial signals primary while making the brief materially count."""
    return round(max(0.0, min(100.0, base_score * 0.72 + relevance * 0.28)), 1)
