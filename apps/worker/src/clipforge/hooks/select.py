"""Hook selection: find a real 1-3 second opening for a moment.

A hook is never invented. It is either the moment's own opening or a genuine
sentence-span elsewhere in the same source whose wording actually connects to the
moment. Anything weaker is reported as missing so the user can decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from clipforge.briefs.spec import HookPolicy
from clipforge.ranking.lexicon import (
    ANECDOTE_MARKERS,
    CONTRASTIVE_STARTS,
    CURIOSITY_MARKERS,
    EMOTION_WORDS,
    FILLER_STARTS,
    QUESTION_WORDS,
    STOPWORDS,
)
from clipforge.ranking.sentences import Sentence, TranscriptModel, tokenize

REACTION_STARTS = ("oh", "wow", "whoa", "no way", "wait", "look", "here's", "heres", "this is")
SUPERLATIVES = ("biggest", "worst", "best", "hardest", "most", "least", "never", "always", "everyone", "nobody")
OUTCOME_MARKERS = (
    "the result", "what happened", "ended up", "turns out", "the lesson", "i learned",
    "it worked", "it failed", "the answer", "that's when", "which means",
)
HOOK_THRESHOLD = 52.0
MIN_REPRESENTATIVE_OVERLAP = 0.08


@dataclass
class Hook:
    text: str
    start_ms: int
    end_ms: int
    source: Literal["candidate", "external", "none"]
    approved: bool
    quality: float
    reason: str
    placement: Literal["before", "none"] = "none"
    warnings: list[str] = field(default_factory=list)
    strategy: str = "chronological"
    grounded: bool = True
    direction: Literal["opening", "before", "after", "none"] = "none"
    needs_context: bool = False
    review_required: bool = False
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    @property
    def usable(self) -> bool:
        return self.source != "none" and self.approved and self.duration_ms > 0

    @property
    def assembled(self) -> bool:
        """True only when the hook becomes an extra segment before the moment."""
        return self.usable and self.placement == "before"

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "source": self.source,
            "placement": self.placement,
            "direction": self.direction,
            "approved": self.approved,
            "grounded": self.grounded,
            "quality": round(self.quality, 1),
            "strategy": self.strategy,
            "reason": self.reason,
            "needs_context": self.needs_context,
            "review_required": self.review_required,
            "scores": {key: round(value, 1) for key, value in self.scores.items()},
            "warnings": self.warnings,
            "estimated": True,
        }

    @classmethod
    def none(cls, reason: str) -> "Hook":
        return cls(
            text="",
            start_ms=0,
            end_ms=0,
            source="none",
            approved=False,
            quality=0.0,
            reason=reason,
            warnings=[reason],
            strategy="manual",
            grounded=True,
            direction="none",
        )


def _strategy_for(text: str) -> str:
    """Classify the editorial job the line can do, not its topic."""
    lowered = " ".join(text.lower().split())
    words = lowered.split()
    if lowered.endswith("?") or (words and words[0] in QUESTION_WORDS):
        return "question"
    if any(marker in lowered for marker in ANECDOTE_MARKERS):
        return "story"
    if any(word in EMOTION_WORDS for word in words):
        return "emotional"
    if any(marker in lowered for marker in CURIOSITY_MARKERS):
        return "curiosity"
    if any(marker in lowered for marker in OUTCOME_MARKERS):
        return "outcome"
    if any(lowered.startswith(start) for start in CONTRASTIVE_STARTS):
        return "contrarian"
    if any(lowered.startswith(start) for start in REACTION_STARTS):
        return "humour" if any(word in lowered for word in ("funny", "laugh", "joke")) else "emotional"
    return "story"


def _strategy_bonus(strategy: str) -> float:
    return {
        "question": 10.0,
        "outcome": 9.0,
        "curiosity": 9.0,
        "story": 4.0,
        "humour": 7.0,
        "emotional": 7.0,
        "contrarian": 8.0,
        "chronological": 0.0,
        "manual": 0.0,
    }.get(strategy, 0.0)


def _content_tokens(text: str) -> set[str]:
    return {word for word in tokenize(text) if word not in STOPWORDS and len(word) > 2}


def _overlap(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _opening_score(text: str) -> tuple[float, list[str]]:
    lowered = " ".join(text.lower().split())
    words = lowered.split()
    first = words[0] if words else ""
    score = 25.0
    reasons: list[str] = []

    if lowered.endswith("?") or (words and words[0] in QUESTION_WORDS):
        score += 26
        reasons.append("opens with a question")
    if any(lowered.startswith(start) for start in CONTRASTIVE_STARTS):
        score += 18
        reasons.append("opens with a contrast")
    if any(lowered.startswith(start) for start in REACTION_STARTS):
        score += 16
        reasons.append("opens with a reaction")
    if any(marker in lowered for marker in CURIOSITY_MARKERS):
        score += 14
        reasons.append("uses a curiosity phrase")
    if first in SUPERLATIVES or any(word in lowered for word in SUPERLATIVES):
        score += 10
        reasons.append("uses a strong claim")
    if any(character.isdigit() for character in lowered):
        score += 8
        reasons.append("contains a concrete number")
    if text.strip().endswith(("!", "…")):
        score += 6
    if words and first in FILLER_STARTS:
        score -= 22
        reasons.append("starts with filler")
    if len(words) <= 2:
        score -= 8
    return max(0.0, min(100.0, score)), reasons


def _window_from_sentence(sentence: Sentence, max_words: int) -> list[tuple[int, int, str]]:
    """Candidate word windows inside one sentence, ending on a word boundary."""
    windows: list[tuple[int, int, str]] = []
    words = sentence.words
    for count in range(1, min(max_words, len(words)) + 1):
        chunk = words[:count]
        text = " ".join(word.clean for word in chunk if word.clean).strip()
        if not text:
            continue
        windows.append((chunk[0].start_ms, chunk[-1].end_ms, text))
        if text.endswith((",", ".", "?", "!", "…")):
            break
    return windows


def build_hook_candidates(
    model: TranscriptModel,
    *,
    start_ms: int,
    end_ms: int,
    policy: HookPolicy,
    main_text: str,
    max_words: int = 14,
    preference: str = "auto",
) -> list[Hook]:
    """Return ranked, grounded hook options with an explicit source direction.

    ``direction="after"`` is the only option that can become a cold open: the
    line comes later in the source and is played before the moment. The editor
    still has to check that the main span resolves the promise; the strategy
    planner does that and falls back to chronological when it cannot.
    """
    if not policy.enabled or "manual" in policy.strategies and len(policy.strategies) == 1:
        return []
    if not policy.auto_select:
        return []

    main_tokens = _content_tokens(main_text)
    candidates: list[Hook] = []
    seen: set[tuple[int, int]] = set()

    for sentence in model.sentences:
        external = not (start_ms - 250 <= sentence.start_ms and sentence.end_ms <= end_ms)
        inside_opening = start_ms - 250 <= sentence.start_ms <= start_ms + 1_500
        distance = 0
        direction: Literal["opening", "before", "after"] = "opening"
        if sentence.end_ms < start_ms:
            distance = start_ms - sentence.end_ms
            direction = "before"
        elif sentence.start_ms > end_ms:
            distance = sentence.start_ms - end_ms
            direction = "after"

        if external:
            if not policy.allow_external:
                continue
            if policy.strict_chronological and direction == "after":
                continue
            if distance > policy.max_distance_ms:
                continue
        elif not inside_opening:
            # A line from the middle of the moment would repeat footage when assembled.
            continue

        for window_start, window_end, text in _window_from_sentence(sentence, max_words):
            if not external and window_start > sentence.start_ms:
                continue
            duration = window_end - window_start
            if duration < policy.min_ms or duration > policy.max_ms:
                continue
            tokens = _content_tokens(text)
            overlap = _overlap(tokens, main_tokens)
            if external and overlap < MIN_REPRESENTATIVE_OVERLAP:
                continue
            # A hook that simply repeats the first few words of the moment adds nothing.
            if tokens and tokens == set(list(main_tokens)[: len(tokens)]):
                continue
            key = (window_start, window_end)
            if key in seen:
                continue
            seen.add(key)

            base, reasons = _opening_score(text)
            strategy = _strategy_for(text)
            if preference not in ("auto", "", "manual") and strategy != preference:
                # Keep chronological wording available as a fallback, but do not
                # let an unrelated strategy win when the user asked for a type.
                if not (preference == "chronological" and direction == "opening"):
                    continue
            if strategy not in policy.strategies and strategy != "chronological":
                continue

            relevance = min(100.0, overlap * 170)
            score = base * 0.62 + relevance * 0.28 + _strategy_bonus(strategy)
            if direction == "opening":
                score += 8
            elif direction == "after":
                score -= 2
            if duration > policy.max_ms * 0.9:
                score -= 4
            if score < max(policy.min_score, HOOK_THRESHOLD):
                continue

            reason_parts = reasons or ["works as a short opening line"]
            reason = "Selected because it " + ", ".join(reason_parts[:3])
            warnings: list[str] = []
            needs_context = direction == "after"
            review_required = False
            if external:
                reason += " and shares subject matter with the moment"
                if direction == "after":
                    reason += "; it is a later line moved earlier as a cold open"
                elif direction == "before":
                    reason += "; it is an earlier line moved forward as context"
                if overlap < MIN_REPRESENTATIVE_OVERLAP * 2:
                    warnings.append(
                        "This hook shares little wording with the moment; confirm it still represents it."
                    )
                    review_required = policy.review_when_weak
            else:
                reason = (
                    "The moment already opens with this line; it is flagged as the hook "
                    "rather than repeated."
                )

            candidates.append(
                Hook(
                    text=text,
                    start_ms=window_start,
                    end_ms=window_end,
                    source="external" if external else "candidate",
                    approved=True,
                    quality=score,
                    reason=reason,
                    placement="before" if external else "none",
                    warnings=warnings,
                    strategy=strategy,
                    grounded=True,
                    direction=direction,
                    needs_context=needs_context,
                    review_required=review_required,
                    scores={
                        "base": base,
                        "relevance": relevance,
                        "strategy": _strategy_bonus(strategy),
                    },
                )
            )

    candidates.sort(key=lambda item: item.quality, reverse=True)
    # Prefer opening hooks at similar quality: a cold open needs a real reason,
    # not a marginal score difference.
    if candidates:
        best = candidates[0]
        opening = next((item for item in candidates if item.direction == "opening"), None)
        if opening and best.direction != "opening" and best.quality - opening.quality < 4:
            candidates.remove(opening)
            candidates.insert(0, opening)
    return candidates[: policy.max_candidates]


def build_hook(
    model: TranscriptModel,
    *,
    start_ms: int,
    end_ms: int,
    policy: HookPolicy,
    main_text: str,
    max_words: int = 14,
    preference: str = "auto",
) -> Hook:
    candidates = build_hook_candidates(
        model,
        start_ms=start_ms,
        end_ms=end_ms,
        policy=policy,
        main_text=main_text,
        max_words=max_words,
        preference=preference,
    )
    if candidates:
        return candidates[0]
    return Hook.none(
        "No suitable 1–3 second hook was found in the transcript. "
        "The clip opens on the moment itself rather than inventing a line."
    )
