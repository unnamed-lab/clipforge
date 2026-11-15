from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class TranscriptionOptions:
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = None
    vad: bool = True
    beam_size: int = 5
    model_dir: str | None = None


@dataclass
class Word:
    start_ms: int
    end_ms: int
    word: str
    probability: float | None = None


@dataclass
class Segment:
    id: int
    start_ms: int
    end_ms: int
    text: str
    words: list[Word] = field(default_factory=list)
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    speaker: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text.strip(),
            "words": [
                {"start_ms": w.start_ms, "end_ms": w.end_ms, "word": w.word, "probability": w.probability}
                for w in self.words
            ],
            "avg_logprob": self.avg_logprob,
            "no_speech_prob": self.no_speech_prob,
            "speaker": self.speaker,
        }


@dataclass
class TranscriptResult:
    language: str | None
    language_probability: float | None
    segments: list[Segment]
    model: str
    duration_ms: int

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "language_probability": self.language_probability,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "segments": [segment.to_dict() for segment in self.segments],
        }


ProgressCallback = Callable[[float], None]


class TranscriptionProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str | None]: ...

    def transcribe(self, audio: Path, options: TranscriptionOptions, *,
                   on_progress: ProgressCallback | None = None,
                   should_cancel: Callable[[], bool] | None = None) -> TranscriptResult: ...


@dataclass(frozen=True)
class TranscriptionCapabilities:
    provider: str
    available: bool
    detail: str | None
    device: str
    compute_type: str
    default_model: str
    models: tuple[str, ...]
