from __future__ import annotations

from clipforge.providers.transcription.base import (
    Segment,
    TranscriptResult,
    TranscriptionCapabilities,
    TranscriptionOptions,
    TranscriptionProvider,
    Word,
)
from clipforge.providers.transcription.faster_whisper import (
    MODEL_SIZES_MB,
    SUPPORTED_MODELS,
    FasterWhisperTranscription,
)

__all__ = [
    "Segment",
    "TranscriptResult",
    "TranscriptionCapabilities",
    "TranscriptionOptions",
    "TranscriptionProvider",
    "Word",
    "FasterWhisperTranscription",
    "SUPPORTED_MODELS",
    "MODEL_SIZES_MB",
    "build_transcription_provider",
]


def build_transcription_provider() -> FasterWhisperTranscription:
    return FasterWhisperTranscription()
