"""Transcript structures: words, sentences and transcript-level statistics."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

TERMINATORS = ".!?…"
PAUSE_SPLIT_MS = 700
SEGMENT_GAP_MS = 400
MAX_SENTENCE_MS = 45_000
MIN_SENTENCE_MS = 1_200

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class Word:
    start_ms: int
    end_ms: int
    text: str
    probability: float | None = None
    segment_id: int = 0
    starts_segment: bool = False

    @property
    def clean(self) -> str:
        return self.text.strip()


@dataclass
class Sentence:
    index: int
    start_ms: int
    end_ms: int
    text: str
    words: list[Word] = field(default_factory=list)
    ends_with_terminator: bool = False
    gap_before_ms: int = 0
    gap_after_ms: int = 0

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def token_count(self) -> int:
        return len(tokenize(self.text))

    @property
    def leading_text(self) -> str:
        return " ".join(self.text.split()[:8])


@dataclass
class TranscriptModel:
    sentences: list[Sentence]
    document_frequency: Counter
    total_tokens: int
    duration_ms: int
    language: str | None = None

    def rare_word_ratio(self, text: str) -> float:
        tokens = [t for t in tokenize(text)]
        if not tokens:
            return 0.0
        rare = sum(1 for token in tokens if self.document_frequency.get(token, 0) <= 1)
        return rare / len(tokens)


def _words_from_segments(segments: list[dict]) -> list[Word]:
    words: list[Word] = []
    for segment in segments:
        segment_id = int(segment.get("id", 0))
        raw_words = segment.get("words") or []
        segment_start = int(segment.get("start_ms", 0))
        segment_end = int(segment.get("end_ms", segment_start))
        if raw_words:
            for position, raw in enumerate(raw_words):
                words.append(
                    Word(
                        start_ms=int(raw.get("start_ms", segment_start)),
                        end_ms=int(raw.get("end_ms", segment_start)),
                        text=str(raw.get("word", "")),
                        probability=raw.get("probability"),
                        segment_id=segment_id,
                        starts_segment=position == 0,
                    )
                )
        else:
            # No word timings: distribute the segment text evenly across its span.
            tokens = str(segment.get("text", "")).split()
            if not tokens:
                continue
            span = max(1, segment_end - segment_start)
            step = span / len(tokens)
            for position, token in enumerate(tokens):
                words.append(
                    Word(
                        start_ms=segment_start + int(position * step),
                        end_ms=segment_start + int((position + 1) * step),
                        text=token,
                        segment_id=segment_id,
                        starts_segment=position == 0,
                    )
                )
    words.sort(key=lambda word: word.start_ms)
    return words


def build_transcript_model(segments: list[dict], duration_ms: int, language: str | None = None) -> TranscriptModel:
    words = _words_from_segments(segments)
    sentences: list[Sentence] = []
    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(word.clean for word in current).strip()
        if not text:
            current.clear()
            return
        start = current[0].start_ms
        end = max(current[-1].end_ms, start + 400)
        sentences.append(
            Sentence(
                index=len(sentences),
                start_ms=start,
                end_ms=end,
                text=text,
                words=list(current),
                ends_with_terminator=text[-1:] in TERMINATORS,
            )
        )
        current.clear()

    for position, word in enumerate(words):
        if not word.clean:
            continue
        if not current:
            current.append(word)
        else:
            previous = current[-1]
            gap = word.start_ms - previous.end_ms
            if gap >= PAUSE_SPLIT_MS:
                flush()
            elif word.starts_segment and gap >= SEGMENT_GAP_MS:
                flush()
            current.append(word)

        combined = " ".join(w.clean for w in current)
        if combined and combined[-1] in TERMINATORS:
            flush()
        elif current and current[-1].end_ms - current[0].start_ms >= MAX_SENTENCE_MS:
            flush()

    flush()

    # Merge very short fragments into the following sentence unless they carry a terminator.
    merged: list[Sentence] = []
    for sentence in sentences:
        if (
            merged
            and sentence.duration_ms < MIN_SENTENCE_MS
            and not merged[-1].ends_with_terminator
            and sentence.start_ms - merged[-1].end_ms < PAUSE_SPLIT_MS
        ):
            previous = merged[-1]
            previous.text = f"{previous.text} {sentence.text}".strip()
            previous.end_ms = sentence.end_ms
            previous.words.extend(sentence.words)
            previous.ends_with_terminator = sentence.ends_with_terminator
            continue
        merged.append(sentence)

    for index, sentence in enumerate(merged):
        sentence.index = index
        if index + 1 < len(merged):
            sentence.gap_after_ms = max(0, merged[index + 1].start_ms - sentence.end_ms)
        else:
            sentence.gap_after_ms = max(0, duration_ms - sentence.end_ms)
        if index > 0:
            sentence.gap_before_ms = max(0, sentence.start_ms - merged[index - 1].end_ms)
        else:
            sentence.gap_before_ms = sentence.start_ms

    corpus_tokens: list[str] = []
    document_frequency: Counter = Counter()
    for sentence in merged:
        tokens = tokenize(sentence.text)
        corpus_tokens.extend(tokens)
        for token in set(tokens):
            document_frequency[token] += 1

    return TranscriptModel(
        sentences=merged,
        document_frequency=document_frequency,
        total_tokens=len(corpus_tokens),
        duration_ms=duration_ms,
        language=language,
    )
