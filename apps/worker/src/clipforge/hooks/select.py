"""Hook selection: find a real 1-3 second opening for a moment.

A hook is never invented. It is either the moment's own opening or a genuine
sentence-span elsewhere in the same source whose wording actually connects to the
moment. Anything weaker is reported as missing so the user can decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from clipforge.briefs.spec import HookPolicy
from clipforge.ranking.lexicon import CONTRASTIVE_STARTS, CURIOSITY_MARKERS, FILLER_STARTS, QUESTION_WORDS, STOPWORDS
from clipforge.ranking.sentences import Sentence, TranscriptModel, tokenize

REACTION_STARTS = ("oh", "wow", "whoa", "no way", "wait", "look", "here's", "heres", "this is")
SUPERLATIVES = ("biggest", "worst", "best", "hardest", "most", "least", "never", "always", "everyone", "nobody")
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
            "approved": self.approved,
            "quality": round(self.quality, 1),
            "reason": self.reason,
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
        )


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


def build_hook(
    model: TranscriptModel,
    *,
    start_ms: int,
    end_ms: int,
    policy: HookPolicy,
    main_text: str,
    max_words: int = 14,
) -> Hook:
    if not policy.enabled:
        return Hook.none("Hook is disabled for this brief, so the clip opens on the moment itself.")

    main_tokens = _content_tokens(main_text)
    best: tuple[float, Hook] | None = None

    for sentence in model.sentences:
        external = not (start_ms - 250 <= sentence.start_ms and sentence.end_ms <= end_ms)
        inside_opening = start_ms - 250 <= sentence.start_ms <= start_ms + 1_500
        distance = 0
        if sentence.end_ms < start_ms:
            distance = start_ms - sentence.end_ms
        elif sentence.start_ms > end_ms:
            distance = sentence.start_ms - end_ms

        if external:
            if not policy.allow_external:
                continue
            if policy.strict_chronological and sentence.start_ms > start_ms:
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

            base, reasons = _opening_score(text)
            relevance = min(100.0, overlap * 170)
            score = base * 0.7 + relevance * 0.3
            if not external:
                score += 8
            if duration > policy.max_ms * 0.9:
                score -= 4

            if score < HOOK_THRESHOLD:
                continue
            reason_parts = reasons or ["works as a short opening line"]
            reason = "Selected because it " + ", ".join(reason_parts[:3])
            if external:
                reason += " and shares subject matter with the moment"
                if sentence.start_ms > end_ms:
                    reason += "; it is moved earlier from later in the source"
            else:
                reason = "The moment already opens with this line; it is flagged as the hook rather than repeated."
            hook = Hook(
                text=text,
                start_ms=window_start,
                end_ms=window_end,
                source="external" if external else "candidate",
                approved=True,
                quality=score,
                reason=reason,
                placement="before" if external else "none",
                warnings=[] if overlap >= MIN_REPRESENTATIVE_OVERLAP else [
                    "This hook shares little wording with the moment; confirm it still represents it."
                ],
            )
            if best is None or hook.quality > best[0]:
                best = (hook.quality, hook)

    if best is None:
        return Hook.none(
            "No suitable 1–3 second hook was found in the transcript. "
            "The clip opens on the moment itself rather than inventing a line."
        )
    return best[1]
