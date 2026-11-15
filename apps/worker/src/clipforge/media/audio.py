from __future__ import annotations

import threading
from pathlib import Path

from clipforge.errors import AppError
from clipforge.media.process import ProcessCancelled, ProcessFailed, ProcessTimeout, require, run

SAMPLE_RATE = 16_000
CHANNELS = 1


def extract_audio(source: Path, destination: Path, *, cancel: threading.Event | None = None,
                  duration_ms: int | None = None,
                  on_progress=None) -> Path:
    """Decode any source to 16 kHz mono PCM WAV for speech recognition."""
    ffmpeg = require("ffmpeg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    args = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-vn", "-sn", "-dn",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
        "-y", str(destination),
    ]

    expected = (duration_ms or 0) / 1000.0
    state: dict[str, float] = {}

    def handle_progress(line: str) -> None:
        if "out_time_us=" in line and on_progress and expected > 0:
            raw = line.split("=", 1)[1].strip()
            try:
                seconds = int(raw) / 1_000_000
            except ValueError:
                return
            percent = max(0.0, min(100.0, seconds / expected * 100))
            if percent - state.get("last", -10) >= 5:
                state["last"] = percent
                on_progress(percent)

    progress_args = args[:-2] + ["-progress", "pipe:1", "-nostats"] + args[-2:] if on_progress else args
    try:
        result = run(progress_args, timeout_seconds=1800, cancel=cancel, on_stdout_line=handle_progress)
    except ProcessCancelled as exc:
        raise
    except ProcessTimeout as exc:
        raise AppError("Audio extraction timed out", code="audio_timeout") from exc
    except ProcessFailed as exc:
        raise AppError("Audio could not be extracted from that video", code="audio_extract_failed") from exc

    if result.returncode != 0 or not destination.exists() or destination.stat().st_size < 1024:
        raise AppError(
            "Audio could not be extracted from that video. It may have no usable audio track.",
            code="audio_extract_failed",
        )
    return destination
