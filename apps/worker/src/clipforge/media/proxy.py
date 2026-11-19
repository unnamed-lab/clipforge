"""Low-resolution analysis proxy and its measured settings.

The proxy exists so scene analysis does not decode a 4K source hundred of times.
It is deliberately video-only: audio is extracted separately for speech and the
waveform, so the proxy cannot be mistaken for a playable rendition of the source.
Timestamps are preserved exactly — only the scale filter changes, never the frame
rate or the duration — so a scene found at 12.4 s in the proxy is at 12.4 s in the
retained original.
"""

from __future__ import annotations

import threading
from pathlib import Path

from clipforge.errors import AppError
from clipforge.media.process import ProcessCancelled, ProcessFailed, ProcessTimeout, require, run

PROXY_CRF = 30
PROXY_PRESET = "veryfast"
PROXY_AUDIO_KBPS = 96
PROXY_MIN_HEIGHT = 144
PROXY_MAX_HEIGHT = 1440


def even(value: float) -> int:
    rounded = max(2, int(round(value)))
    return rounded + (rounded % 2)


def proxy_dimensions(width: int | None, height: int | None, limit: int) -> tuple[int, int] | None:
    """Fit the long edge inside ``limit``, keeping the source aspect ratio."""
    if not width or not height or limit <= 0:
        return None
    if max(width, height) <= limit:
        return int(width), int(height)
    if width >= height:
        return even(limit), even(height * limit / width)
    return even(width * limit / height), even(limit)


def proxy_settings(*, limit: int) -> dict:
    return {
        "limit": limit,
        "crf": PROXY_CRF,
        "preset": PROXY_PRESET,
        "audio": "dropped",
        "audio_bitrate_kbps": PROXY_AUDIO_KBPS,
        "purpose": "scene analysis only; never an export source",
    }


def build_proxy(
    source: Path,
    destination: Path,
    *,
    dimensions: tuple[int, int],
    duration_ms: int,
    cancel: threading.Event | None = None,
    on_progress=None,
) -> dict:
    """Encode the analysis proxy and return what was actually produced."""
    ffmpeg = require("ffmpeg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    width, height = dimensions
    args = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", f"scale={width}:{height}:flags=bicubic",
        "-c:v", "libx264", "-preset", PROXY_PRESET, "-crf", str(PROXY_CRF),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-y", str(destination),
    ]
    expected = max(0.0, duration_ms / 1000.0)
    last = {"percent": -20.0}

    def handle_progress(line: str) -> None:
        if "out_time_us=" not in line or on_progress is None or expected <= 0:
            return
        raw = line.split("=", 1)[1].strip()
        try:
            seconds = int(raw) / 1_000_000
        except ValueError:
            return
        percent = max(0.0, min(100.0, seconds / expected * 100))
        if percent - last["percent"] >= 5:
            last["percent"] = percent
            on_progress(percent)

    progress_args = args[:-2] + ["-progress", "pipe:1", "-nostats"] + args[-2:]
    try:
        result = run(progress_args, timeout_seconds=1800, cancel=cancel, on_stdout_line=handle_progress)
    except ProcessCancelled:
        destination.unlink(missing_ok=True)
        raise
    except ProcessTimeout as exc:
        destination.unlink(missing_ok=True)
        raise AppError("Preparing the analysis proxy timed out", code="proxy_timeout") from exc
    except ProcessFailed as exc:
        destination.unlink(missing_ok=True)
        raise AppError("The analysis proxy could not be prepared", code="proxy_failed") from exc

    if result.returncode != 0 or not destination.exists() or destination.stat().st_size < 4096:
        destination.unlink(missing_ok=True)
        raise AppError("The analysis proxy could not be prepared", code="proxy_failed")
    return {
        **proxy_settings(limit=max(width, height)),
        "width": width,
        "height": height,
        "size_bytes": destination.stat().st_size,
    }
