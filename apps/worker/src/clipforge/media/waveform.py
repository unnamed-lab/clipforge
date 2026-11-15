from __future__ import annotations

import threading
from pathlib import Path

from clipforge.media.process import ProcessFailed, require, run


def render_waveform(source: Path, destination: Path, *, cancel: threading.Event | None = None) -> Path | None:
    """Render a single-picture waveform strip for the trim editor.

    Failure is non-fatal: the editor falls back to a plain track without a waveform.
    """
    ffmpeg = require("ffmpeg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-filter_complex",
        "[0:a]aformat=channel_layouts=mono,showwavespic=s=1600x160:colors=#7d8798:scale=sqrt[w]",
        "-map", "[w]", "-frames:v", "1", "-y", str(destination),
    ]
    try:
        result = run(args, timeout_seconds=300, cancel=cancel)
    except ProcessFailed:
        return None
    except Exception:
        return None
    if result.returncode != 0 or not destination.exists() or destination.stat().st_size < 128:
        return None
    return destination
