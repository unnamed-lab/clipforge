"""faster-whisper transcription provider (CPU int8 by default)."""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

from clipforge.errors import JobFailed, ProviderUnavailable
from clipforge.providers.transcription.base import (
    Segment,
    TranscriptResult,
    TranscriptionOptions,
    Word,
)

logger = logging.getLogger(__name__)

MODEL_SIZES_MB = {
    "tiny": 75,
    "tiny.en": 75,
    "base": 145,
    "base.en": 145,
    "small": 480,
    "small.en": 480,
    "medium": 1500,
    "medium.en": 1500,
    "large-v3": 3100,
}
SUPPORTED_MODELS = ("tiny", "base", "small", "medium")

_MODEL_CACHE: dict[tuple[str, str, str], object] = {}
_MODEL_LOCK = threading.Lock()

AUDIO_SAMPLE_RATE = 16_000


def load_pcm_16k(audio: Path) -> "np.ndarray":
    """Read the extracted 16 kHz mono WAV as float32 samples.

    faster-whisper is handed samples rather than a path on purpose: its own
    decoder goes through PyAV, whose ``av.open`` signature has changed across
    releases. Our extractor already produces exactly the format Whisper wants,
    so decoding it ourselves removes that version coupling entirely.
    """
    import numpy as np

    try:
        with wave.open(str(audio), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, OSError) as exc:
        raise JobFailed(
            "audio_unreadable",
            "The extracted audio track could not be read. Re-import the video and try again.",
        ) from exc

    if channels != 1 or width != 2 or rate != AUDIO_SAMPLE_RATE:
        raise JobFailed(
            "audio_format_unexpected",
            "The extracted audio track was not 16 kHz mono PCM. Re-import the video and try again.",
        )

    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


class FasterWhisperTranscription:
    name = "faster-whisper"

    def available(self) -> tuple[bool, str | None]:
        try:
            import faster_whisper  # noqa: F401
        except Exception as exc:  # pragma: no cover - import failure path
            return False, f"faster-whisper could not be imported: {exc.__class__.__name__}"
        return True, None

    def _load_model(self, options: TranscriptionOptions):
        key = (options.model, options.device, options.compute_type, options.model_dir or "")
        with _MODEL_LOCK:
            if key in _MODEL_CACHE:
                return _MODEL_CACHE[key]
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise ProviderUnavailable(
                "The transcription engine is not installed in the worker environment.",
                code="transcription_unavailable",
            ) from exc

        try:
            model = WhisperModel(
                options.model,
                device=options.device,
                compute_type=options.compute_type,
                download_root=options.model_dir,
                cpu_threads=0,
            )
        except Exception as exc:
            logger.exception("Failed to load whisper model %s", options.model)
            raise JobFailed(
                "model_download_failed",
                f"The transcription model '{options.model}' could not be loaded. "
                "Check your internet connection for the first download, or retry with a smaller model.",
            ) from exc

        with _MODEL_LOCK:
            _MODEL_CACHE[key] = model
        return model

    def transcribe(
        self,
        audio: Path,
        options: TranscriptionOptions,
        *,
        on_progress: Callable[[float], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TranscriptResult:
        model = self._load_model(options)
        samples = load_pcm_16k(audio)
        total_ms = max(1, int(round(len(samples) / AUDIO_SAMPLE_RATE * 1000)))
        try:
            segments_iter, info = model.transcribe(  # type: ignore[attr-defined]
                samples,
                language=options.language,
                beam_size=options.beam_size,
                word_timestamps=True,
                vad_filter=options.vad,
                vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
                condition_on_previous_text=False,
                temperature=0,
            )
        except Exception as exc:
            logger.exception("Transcription failed")
            raise JobFailed("transcription_failed", "Speech recognition failed on this file.") from exc

        segments: list[Segment] = []
        for index, segment in enumerate(segments_iter):
            if should_cancel is not None and should_cancel():
                raise JobFailed("cancelled", "Transcription cancelled")
            words = [
                Word(
                    start_ms=int(round(word.start * 1000)),
                    end_ms=int(round(word.end * 1000)),
                    word=word.word,
                    probability=round(float(word.probability), 4) if word.probability is not None else None,
                )
                for word in (segment.words or [])
            ]
            segments.append(
                Segment(
                    id=index,
                    start_ms=int(round(segment.start * 1000)),
                    end_ms=int(round(segment.end * 1000)),
                    text=segment.text,
                    words=words,
                    avg_logprob=round(float(segment.avg_logprob), 4) if segment.avg_logprob is not None else None,
                    no_speech_prob=round(float(segment.no_speech_prob), 4)
                    if segment.no_speech_prob is not None
                    else None,
                )
            )
            if on_progress is not None:
                on_progress(min(99.0, segment.end * 1000 / total_ms * 100))

        if not segments:
            raise JobFailed(
                "no_speech_detected",
                "No speech was detected in this video. Check that the audio track contains dialogue.",
            )

        return TranscriptResult(
            language=getattr(info, "language", None),
            language_probability=round(float(info.language_probability), 4)
            if getattr(info, "language_probability", None) is not None
            else None,
            segments=segments,
            model=options.model,
            duration_ms=total_ms,
        )
