"""FFmpeg command construction and execution.

Pure functions build the argv; the executor only runs it. FFmpeg is started with
``cwd`` set to the render workspace and relative filenames for anything referenced
inside a filter graph, which avoids the Windows drive-colon escaping minefield.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from clipforge.media.process import ProcessCancelled, ProcessFailed, ProcessTimeout, require, run
from clipforge.rendering.spec import RenderSpec

X264_PRESET = "veryfast"
CRF = 20
AUDIO_BITRATE = "160k"
AUDIO_RATE = 48_000
SILENCE_LABEL = "anullsrc=channel_layout=stereo:sample_rate=48000"


@dataclass
class RenderCommand:
    args: list[str]
    cwd: Path
    output_name: str
    expected_duration_ms: int


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def _fps(spec: RenderSpec, source_fps: float | None) -> int:
    if spec.video.fps:
        return int(spec.video.fps)
    if source_fps and 15 <= source_fps <= 60:
        return int(round(source_fps))
    return 30


def _framing_chain(spec: RenderSpec, width: int, height: int, input_label: str,
                   output_label: str, captions_name: str | None) -> str:
    if spec.video.framing == "blur":
        chain = (
            f"{input_label}split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=32,eq=brightness=-0.08:saturation=0.85,"
            f"setsar=1[bgblur];"
            f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"setsar=1[fgfit];"
            f"[bgblur][fgfit]overlay=(W-w)/2:(H-h)/2[composed];"
            f"[composed]setsar=1"
        )
    else:
        chain = (
            f"{input_label}scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        )
    if captions_name:
        chain += f",ass={captions_name}"
    return chain + f"[{output_label}]"


def _audio_chain(spec: RenderSpec, input_label: str, output_label: str) -> str:
    parts = [f"aformat=sample_rates={AUDIO_RATE}:channel_layouts=stereo", "asetpts=PTS-STARTPTS"]
    if spec.audio.normalize:
        parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return f"{input_label}{','.join(parts)}[{output_label}]"


def build_command(
    spec: RenderSpec,
    *,
    source: Path,
    cwd: Path,
    has_audio: bool,
    source_fps: float | None,
    captions_name: str | None,
    output_name: str = "out.mp4",
) -> RenderCommand:
    ffmpeg = require("ffmpeg")
    width, height = spec.video.dimensions
    fps = _fps(spec, source_fps)
    total_ms = spec.total_duration_ms

    args: list[str] = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
    ]

    for index, segment in enumerate(spec.segments):
        args += ["-ss", _seconds(segment.start_ms), "-t", _seconds(segment.duration_ms), "-i", str(source)]
        if not has_audio:
            args += ["-f", "lavfi", "-t", _seconds(segment.duration_ms), "-i", SILENCE_LABEL]

    filters: list[str] = []
    video_inputs: list[str] = []
    audio_inputs: list[str] = []

    for index in range(len(spec.segments)):
        filters.append(_framing_chain(spec, width, height, f"[{index}:v]", f"v{index}", None))
        video_inputs.append(f"[v{index}]")
        if has_audio:
            filters.append(_audio_chain(spec, f"[{index}:a]", f"a{index}"))
            audio_inputs.append(f"[a{index}]")
        else:
            silence_index = len(spec.segments) + index
            filters.append(_audio_chain(spec, f"[{silence_index}:a]", f"a{index}"))
            audio_inputs.append(f"[a{index}]")

    if len(spec.segments) == 1:
        filters.append(f"[v0]fps={fps}" + (f",ass={captions_name}" if captions_name else "") + "[vout]")
        filters.append("[a0]anull[aout]")
    else:
        concat_inputs = "".join(f"{video_inputs[i]}{audio_inputs[i]}" for i in range(len(spec.segments)))
        filters.append(f"{concat_inputs}concat=n={len(spec.segments)}:v=1:a=1[vc][ac]")
        filters.append(f"[vc]fps={fps}" + (f",ass={captions_name}" if captions_name else "") + "[vout]")
        filters.append("[ac]anull[aout]")

    args += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(CRF),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_RATE), "-ac", "2",
        "-movflags", "+faststart",
        "-map_metadata", "-1",
        "-progress", "pipe:1", "-nostats",
        output_name,
    ]

    return RenderCommand(args=args, cwd=cwd, output_name=output_name, expected_duration_ms=total_ms)


@dataclass
class RenderOutcome:
    stderr: str
    duration_seconds: float


def execute(
    command: RenderCommand,
    *,
    cancel: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
    timeout_seconds: float = 1800,
) -> RenderOutcome:
    expected_ms = max(1, command.expected_duration_ms)
    last_reported = -10.0

    def handle(line: str) -> None:
        nonlocal last_reported
        if on_progress is None:
            return
        if line.startswith("out_time_us="):
            raw = line.split("=", 1)[1].strip()
            if not raw.isdigit():
                return
            seconds = int(raw) / 1_000_000
            percent = max(0.0, min(99.0, seconds * 1000 / expected_ms * 100))
            if percent - last_reported >= 1.5:
                last_reported = percent
                on_progress(percent)

    try:
        result = run(
            command.args,
            cwd=command.cwd,
            timeout_seconds=timeout_seconds,
            cancel=cancel,
            on_stdout_line=handle,
        )
    except ProcessCancelled:
        raise
    except ProcessTimeout as exc:
        raise TimeoutError("Rendering timed out") from exc
    except ProcessFailed as exc:
        raise RuntimeError(exc.stderr or "ffmpeg failed") from exc

    if result.returncode != 0:
        raise RuntimeError(result.stderr or f"ffmpeg exited with {result.returncode}")
    if on_progress is not None:
        on_progress(99.0)
    return RenderOutcome(stderr=result.stderr, duration_seconds=result.duration_seconds)
