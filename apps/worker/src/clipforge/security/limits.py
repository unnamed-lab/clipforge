"""Shared numeric limits and timestamp validation."""

from __future__ import annotations

MIN_CLIP_MS = 2_000
MAX_CLIP_MS = 15 * 60 * 1000
RENDER_TIMEOUT_SECONDS = 30 * 60
IMPORT_TIMEOUT_SECONDS = 45
DOWNLOAD_TIMEOUT_SECONDS = 20 * 60
MAX_CANDIDATE_WARNINGS = 8


def clamp_timestamp(ms: int, duration_ms: int) -> int:
    return max(0, min(int(ms), int(duration_ms)))


def validate_span(start_ms: int, end_ms: int, duration_ms: int, *, min_ms: int = MIN_CLIP_MS, max_ms: int = MAX_CLIP_MS) -> tuple[int, int]:
    """Validate a clip span and return it as ``(start, end)`` in milliseconds.

    Raises ValueError with a user-presentable message; the API maps it to a 422.
    """
    start = int(round(start_ms))
    end = int(round(end_ms))
    if start < 0:
        raise ValueError("Start time cannot be negative")
    if end <= start:
        raise ValueError("End time must be after the start time")
    if duration_ms > 0 and end > duration_ms:
        raise ValueError("End time is past the end of the source video")
    length = end - start
    if length < min_ms:
        raise ValueError(f"A clip must be at least {min_ms // 1000} seconds long")
    if length > max_ms:
        raise ValueError(f"A clip cannot be longer than {max_ms // 60000} minutes")
    return start, end


def format_timecode(ms: int) -> str:
    total_seconds, millis = divmod(max(0, int(ms)), 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{millis // 100:01d}"
    return f"{minutes}:{seconds:02d}"
