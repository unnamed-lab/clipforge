"""Boundary-aware candidate generation.

Windows always start and end on sentence boundaries. Nothing is cut mid-thought by
construction, which is the whole point of not slicing the video into fixed buckets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from clipforge.ranking.lexicon import FILLER_STARTS
from clipforge.ranking.sentences import Sentence, TranscriptModel

DURATION_BANDS: tuple[tuple[int, int, str], ...] = (
    (15_000, 30_000, "short"),
    (30_000, 60_000, "medium"),
    (60_000, 90_000, "long"),
)

DISCOURSE_OPENERS = ("so", "now", "here's", "here is", "let me", "let's", "the thing is", "i want to", "look")

MAX_CANDIDATES = 1200
MAX_PER_BAND = 500
LEAD_IN_MAX_MS = 400
TAIL_MAX_MS = 600


@dataclass
class CandidateDraft:
    key: str
    start_ms: int
    end_ms: int
    text: str
    band: str
    sentence_start: int
    sentence_end: int
    gap_before_ms: int
    gap_after_ms: int
    first_sentence: str
    last_sentence: str
    speech_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def silence_ratio(self) -> float:
        if self.duration_ms <= 0:
            return 0.0
        return max(0.0, 1.0 - (self.speech_ms / self.duration_ms))


def _is_legal_start(sentence: Sentence, previous: Sentence | None) -> bool:
    if previous is None:
        return True
    if sentence.gap_before_ms >= 300:
        return True
    lowered = sentence.text.strip().lower()
    if any(lowered.startswith(opener) for opener in DISCOURSE_OPENERS):
        return True
    return False


def _starts_with_filler(text: str) -> bool:
    lowered = text.strip().lower()
    return any(lowered.startswith(filler) for filler in FILLER_STARTS)


def _refine_start(sentence: Sentence, previous: Sentence | None) -> int:
    start = sentence.start_ms
    if previous is not None:
        start = max(previous.end_ms, start - LEAD_IN_MAX_MS)
    else:
        start = max(0, start - LEAD_IN_MAX_MS)
    return max(0, start)


def _refine_end(sentence: Sentence, following: Sentence | None, duration_ms: int) -> int:
    end = sentence.end_ms
    if following is not None and sentence.ends_with_terminator:
        end = min(duration_ms, end + min(TAIL_MAX_MS, max(0, following.start_ms - end)))
    return max(end, sentence.start_ms + 400)


def generate_candidates(
    model: TranscriptModel,
    *,
    bands: tuple[tuple[int, int, str], ...] = DURATION_BANDS,
    max_candidates: int = MAX_CANDIDATES,
) -> list[CandidateDraft]:
    sentences = model.sentences
    if not sentences:
        return []

    drafts: dict[str, CandidateDraft] = {}
    for min_duration, max_duration, band in bands:
        band_drafts: list[CandidateDraft] = []
        for start_index in range(len(sentences)):
            first = sentences[start_index]
            previous = sentences[start_index - 1] if start_index > 0 else None
            if not _is_legal_start(first, previous):
                continue

            tightest: CandidateDraft | None = None
            longest: CandidateDraft | None = None
            end_index = start_index
            while end_index < len(sentences):
                last = sentences[end_index]
                span = last.end_ms - first.start_ms
                if span > max_duration:
                    break
                following = sentences[end_index + 1] if end_index + 1 < len(sentences) else None
                start_ms = _refine_start(first, previous)
                end_ms = _refine_end(last, following, model.duration_ms)
                duration = end_ms - start_ms
                if min_duration <= duration <= max_duration:
                    text = " ".join(s.text for s in sentences[start_index : end_index + 1])
                    speech_ms = sum(max(0, s.duration_ms) for s in sentences[start_index : end_index + 1])
                    draft = CandidateDraft(
                        key=f"{start_index}:{end_index}",
                        start_ms=start_ms,
                        end_ms=end_ms,
                        text=text,
                        band=band,
                        sentence_start=start_index,
                        sentence_end=end_index,
                        gap_before_ms=first.gap_before_ms,
                        gap_after_ms=last.gap_after_ms,
                        first_sentence=first.text,
                        last_sentence=last.text,
                        speech_ms=speech_ms,
                    )
                    if tightest is None:
                        tightest = draft
                    if last.ends_with_terminator:
                        longest = draft
                end_index += 1

            for draft in (tightest, longest):
                if draft is not None:
                    band_drafts.append(draft)

        # Even sampling keeps a long video's tail from being starved by the cap.
        if len(band_drafts) > MAX_PER_BAND:
            step = len(band_drafts) / MAX_PER_BAND
            band_drafts = [band_drafts[min(int(i * step), len(band_drafts) - 1)] for i in range(MAX_PER_BAND)]

        for draft in band_drafts:
            drafts.setdefault(draft.key, draft)

    ordered = sorted(drafts.values(), key=lambda d: (d.start_ms, d.duration_ms))
    if len(ordered) > max_candidates:
        step = len(ordered) / max_candidates
        ordered = [ordered[min(int(i * step), len(ordered) - 1)] for i in range(max_candidates)]
    return ordered


def timeline_spread_limit(total_kept: int, duration_ms: int) -> int:
    """Maximum candidates allowed inside any 10% slice of the timeline."""
    buckets = max(1, min(10, math.ceil(duration_ms / 60_000)))
    return max(1, math.ceil(total_kept / buckets))
