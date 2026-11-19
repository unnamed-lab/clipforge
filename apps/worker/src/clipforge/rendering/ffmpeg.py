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
from clipforge.rendering.spec import CompositionDecision, Rect, RenderSpec, SegmentSpan

AUDIO_RATE = 48_000
LOGO_MARGIN = 48
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
    requested = spec.video.fps
    if requested:
        fps = int(requested)
    elif source_fps and 15 <= source_fps <= 60:
        fps = int(round(source_fps))
    else:
        fps = 30
    if spec.encode.max_fps:
        fps = min(fps, int(spec.encode.max_fps))
    return max(15, fps)


def _even(value: float) -> int:
    rounded = max(2, int(round(value)))
    return rounded + (rounded % 2)


def _fit_rect(source_aspect: float, target_aspect: float, margin: float) -> Rect:
    margin = max(0.0, min(0.20, margin))
    if source_aspect <= 0 or target_aspect <= 0:
        return Rect(x_pct=margin, y_pct=margin, width_pct=1 - 2 * margin, height_pct=1 - 2 * margin)
    if source_aspect > target_aspect:
        width = 1.0 - 2 * margin
        height = width * target_aspect / source_aspect
        return Rect(x_pct=margin, y_pct=(1.0 - height) / 2, width_pct=width, height_pct=height)
    height = 1.0 - 2 * margin
    width = height * source_aspect / target_aspect
    return Rect(x_pct=(1.0 - width) / 2, y_pct=margin, width_pct=width, height_pct=height)


def _foreground_rect(decision: CompositionDecision, source_aspect: float, target_aspect: float) -> Rect:
    return decision.foreground or _fit_rect(source_aspect, target_aspect, decision.fit_margin_pct)


def _composition_chain(
    decision: CompositionDecision,
    *,
    width: int,
    height: int,
    source_aspect: float,
    target_aspect: float,
    input_label: str,
    output_label: str,
) -> str:
    uid = output_label
    mode = decision.mode
    if mode == "split":
        # Split/stacked needs two simultaneous sources. A single-source segment
        # must never pretend to show two; contain it instead.
        mode = "fit"
    if mode in {"fit", "manual"} and decision.foreground is not None:
        mode = "fit"
    elif mode == "manual":
        mode = "crop"
    if mode == "stack" and decision.facecam is None:
        mode = "crop"

    if mode == "stack":
        face = decision.facecam
        assert face is not None  # guarded above
        game_height = max(2, min(height - 2, _even(height * 0.60)))
        cam_height = max(2, height - game_height)
        fx = max(0.0, min(1.0, face.x_pct))
        fy = max(0.0, min(1.0, face.y_pct))
        fw = max(0.01, min(1.0 - fx, face.width_pct))
        fh = max(0.01, min(1.0 - fy, face.height_pct))
        chain = (
            f"{input_label}split=2[gamesrc_{uid}][camsrc_{uid}];"
            f"[gamesrc_{uid}]scale={width}:{game_height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{game_height}:(iw-ow)*0.5:(ih-oh)*0.5,setsar=1[game_{uid}];"
            f"[camsrc_{uid}]crop=iw*{fw:.4f}:ih*{fh:.4f}:iw*{fx:.4f}:ih*{fy:.4f},"
            f"scale={width}:{cam_height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{cam_height}:(iw-ow)*0.5:(ih-oh)*0.5,setsar=1[cam_{uid}];"
            f"color=c={decision.background_color}:s={width}x{height}:r=30[bg_{uid}];"
            f"[bg_{uid}][game_{uid}]overlay=0:0:shortest=1[stacked_{uid}];"
            f"[stacked_{uid}][cam_{uid}]overlay=0:{game_height}:shortest=1[composed_{uid}];"
            f"[composed_{uid}]setsar=1"
        )
        return chain + f"[{output_label}]"

    if mode == "crop":
        x = max(0.0, min(1.0, decision.crop_x_pct / 100))
        y = max(0.0, min(1.0, decision.crop_y_pct / 100))
        source_crop = ""
        if decision.source_rect is not None:
            source = decision.source_rect
            source_crop = (
                f"crop=iw*{source.width_pct:.4f}:ih*{source.height_pct:.4f}:"
                f"iw*{source.x_pct:.4f}:ih*{source.y_pct:.4f},"
            )
        chain = (
            f"{input_label}{source_crop}scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-ow)*{x:.4f}:(ih-oh)*{y:.4f},setsar=1"
        )
        return chain + f"[{output_label}]"

    if mode == "pip":
        pip_width = _even(width * decision.pip_scale_pct / 100)
        pip_height = _even(height * decision.pip_scale_pct / 100)
        margin = max(0, int(decision.pip_margin_px))
        if decision.pip_position == "top":
            y = str(margin)
        elif decision.pip_position == "bottom":
            y = f"H-h-{margin}"
        else:
            y = "(H-h)/2"
        chain = (
            f"{input_label}split=2[bgsrc_{uid}][fgsrc_{uid}];"
            f"[bgsrc_{uid}]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=32,eq=brightness=-0.08:saturation=0.85,"
            f"setsar=1[bg_{uid}];"
            f"[fgsrc_{uid}]scale={pip_width}:{pip_height}:force_original_aspect_ratio=decrease,"
            f"setsar=1[fg_{uid}];"
            f"[bg_{uid}][fg_{uid}]overlay=(W-w)/2:{y}:shortest=1[composed_{uid}];"
            f"[composed_{uid}]setsar=1"
        )
        return chain + f"[{output_label}]"

    rect = _foreground_rect(decision, source_aspect, target_aspect)
    fg_width = min(width - 2, _even(rect.width_pct * width))
    fg_height = min(height - 2, _even(rect.height_pct * height))
    x = max(0, min(width - fg_width, int(round(rect.x_pct * width))))
    y = max(0, min(height - fg_height, int(round(rect.y_pct * height))))
    source_crop = ""
    if decision.source_rect is not None:
        rect = decision.source_rect
        source_crop = (
            f"crop=iw*{rect.width_pct:.4f}:ih*{rect.height_pct:.4f}:"
            f"iw*{rect.x_pct:.4f}:ih*{rect.y_pct:.4f},"
        )
    if decision.background in {"solid", "none"}:
        background_color = decision.background_color if decision.background == "solid" else "#000000"
        chain = (
            f"color=c={background_color}:s={width}x{height}:r=30[bg_{uid}];"
            f"{input_label}{source_crop}scale={fg_width}:{fg_height}:force_original_aspect_ratio=decrease,"
            f"setsar=1[fg_{uid}];"
            f"[bg_{uid}][fg_{uid}]overlay={x}:{y}:shortest=1[composed_{uid}];"
            f"[composed_{uid}]setsar=1"
        )
        return chain + f"[{output_label}]"

    chain = (
        f"{input_label}split=2[bgsrc_{uid}][fgsrc_{uid}];"
        f"[bgsrc_{uid}]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=32,eq=brightness=-0.08:saturation=0.85,"
        f"setsar=1[bg_{uid}];"
        f"[fgsrc_{uid}]{source_crop}scale={fg_width}:{fg_height}:force_original_aspect_ratio=decrease,"
        f"setsar=1[fg_{uid}];"
        f"[bg_{uid}][fg_{uid}]overlay={x}:{y}[composed_{uid}];"
        f"[composed_{uid}]setsar=1"
    )
    return chain + f"[{output_label}]"


def _framing_chain(spec: RenderSpec, width: int, height: int, input_label: str,
                   output_label: str, segment: SegmentSpan, source_aspect: float) -> str:
    if segment.composition is not None:
        return _composition_chain(
            segment.composition,
            width=width,
            height=height,
            source_aspect=source_aspect,
            target_aspect=width / height,
            input_label=input_label,
            output_label=output_label,
        )
    if spec.video.framing == "blur":
        uid = output_label
        chain = (
            f"{input_label}split=2[bgsrc_{uid}][fgsrc_{uid}];"
            f"[bgsrc_{uid}]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=32,eq=brightness=-0.08:saturation=0.85,"
            f"setsar=1[bgblur_{uid}];"
            f"[fgsrc_{uid}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"setsar=1[fgfit_{uid}];"
            f"[bgblur_{uid}][fgfit_{uid}]overlay=(W-w)/2:(H-h)/2[composed_{uid}];"
            f"[composed_{uid}]setsar=1"
        )
    else:
        chain = (
            f"{input_label}scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        )
    return chain + f"[{output_label}]"


def _audio_chain(spec: RenderSpec, input_label: str, output_label: str) -> str:
    parts = [f"aformat=sample_rates={AUDIO_RATE}:channel_layouts=stereo", "asetpts=PTS-STARTPTS"]
    if spec.audio.denoise:
        parts.append("afftdn=nr=12:nf=-30")
    if spec.audio.dialogue_enhance:
        parts.append("equalizer=f=180:t=q:w=1:g=2,equalizer=f=3000:t=q:w=1.4:g=3,dynaudnorm=f=200:g=15")
    if spec.audio.normalize:
        parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return f"{input_label}{','.join(parts)}[{output_label}]"


def _logo_position(spec: RenderSpec, width: int, height: int) -> tuple[str, str]:
    logo = spec.logo
    margin = logo.margin_px
    if logo.position == "top-left":
        return str(margin), str(margin)
    if logo.position == "bottom-left":
        return str(margin), f"H-h-{margin}"
    if logo.position == "bottom-right":
        return f"W-w-{margin}", f"H-h-{margin}"
    if logo.position == "custom":
        return f"(W-w)*{logo.custom_x_pct / 100:.4f}", f"(H-h)*{logo.custom_y_pct / 100:.4f}"
    return f"W-w-{margin}", str(margin)


def _audio_graph(spec: RenderSpec, base_label: str, music_index: int | None, total_ms: int) -> list[str]:
    audio = spec.audio
    filters: list[str] = []
    tail: list[str] = []
    if audio.fade_in_ms:
        tail.append(f"afade=t=in:st=0:d={_seconds(audio.fade_in_ms)}")
    if audio.fade_out_ms:
        start = max(0, total_ms - audio.fade_out_ms)
        tail.append(f"afade=t=out:st={_seconds(start)}:d={_seconds(audio.fade_out_ms)}")
    tail.append("anull")

    if audio.mode == "replace" and music_index is not None:
        filters.append(
            f"[{music_index}:a]volume={audio.music_gain_db}dB,atrim=0:{_seconds(total_ms)},"
            f"asetpts=PTS-STARTPTS[music]"
        )
        filters.append(f"[music]{','.join(tail)}[aout]")
        return filters

    if audio.mode == "original_plus_music" and music_index is not None:
        filters.append(
            f"[{music_index}:a]volume={audio.music_gain_db}dB,atrim=0:{_seconds(total_ms)},"
            f"asetpts=PTS-STARTPTS[music]"
        )
        if audio.duck_music:
            filters.append(f"{base_label}asplit=2[mainmix][sidechain]")
            filters.append(
                "[music][sidechain]sidechaincompress=threshold=0.05:ratio=4:attack=20:release=400"
                "[ducked]"
            )
            filters.append("[mainmix][ducked]amix=inputs=2:duration=first:normalize=0[mixed]")
        else:
            filters.append(f"{base_label}[music]amix=inputs=2:duration=first:normalize=0[mixed]")
        base_label = "[mixed]"

    filters.append(f"{base_label}{','.join(tail)}[aout]")
    return filters


def _logo_filters(spec: RenderSpec, video_label: str, width: int, height: int, logo_index: int) -> list[str]:
    logo = spec.logo
    if not logo.enabled:
        return [f"{video_label}null[vout]"]
    logo_width = max(16, int(round(width * logo.width_pct / 100)))
    x, y = _logo_position(spec, width, height)
    enable = ""
    if logo.from_ms is not None and logo.to_ms is not None:
        enable = f":enable='between(t,{logo.from_ms / 1000:.3f},{logo.to_ms / 1000:.3f})'"
    elif logo.from_ms is not None:
        enable = f":enable='gte(t,{logo.from_ms / 1000:.3f})'"
    elif logo.to_ms is not None:
        enable = f":enable='lte(t,{logo.to_ms / 1000:.3f})'"
    return [
        f"[{logo_index}:v]scale={logo_width}:-1,format=rgba,"
        f"colorchannelmixer=aa={logo.opacity:.3f}[logo]",
        f"{video_label}[logo]overlay={x}:{y}{enable}[vout]",
    ]


def build_command(
    spec: RenderSpec,
    *,
    source: Path,
    cwd: Path,
    has_audio: bool,
    source_fps: float | None,
    captions_name: str | None,
    output_name: str = "out.mp4",
    source_width: int | None = None,
    source_height: int | None = None,
) -> RenderCommand:
    ffmpeg = require("ffmpeg")
    width, height = spec.output_dimensions
    fps = _fps(spec, source_fps)
    total_ms = spec.total_duration_ms
    segment_count = len(spec.segments)
    source_aspect = (source_width or 1920) / max(1, source_height or 1080)

    args: list[str] = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
    ]

    audio_indexes: list[int] = []
    next_input = 0
    for segment in spec.segments:
        args += ["-ss", _seconds(segment.start_ms), "-t", _seconds(segment.duration_ms), "-i", str(source)]
        audio_indexes.append(next_input)
        next_input += 1
        if not has_audio:
            args += ["-f", "lavfi", "-t", _seconds(segment.duration_ms), "-i", SILENCE_LABEL]
            audio_indexes[-1] = next_input
            next_input += 1

    music_index: int | None = None
    if spec.audio.mode != "original" and spec.audio.music_name:
        args += ["-i", spec.audio.music_name]
        music_index = next_input
        next_input += 1

    logo_index: int | None = None
    if spec.logo.enabled and spec.logo.asset_name:
        args += ["-loop", "1", "-framerate", str(fps), "-t", _seconds(total_ms), "-i", spec.logo.asset_name]
        logo_index = next_input
        next_input += 1

    filters: list[str] = []
    video_inputs: list[str] = []
    audio_inputs: list[str] = []

    caption_filter = f",ass={captions_name}:fontsdir=fonts" if captions_name else ""
    for index, segment in enumerate(spec.segments):
        filters.append(
            _framing_chain(
                spec, width, height, f"[{index}:v]", f"v{index}", segment, source_aspect
            )
        )
        video_inputs.append(f"[v{index}]")
        filters.append(_audio_chain(spec, f"[{audio_indexes[index]}:a]", f"a{index}"))
        audio_inputs.append(f"[a{index}]")

    if segment_count == 1:
        filters.append(f"[v0]fps={fps}{caption_filter}[vcapped]")
        filters.append("[a0]anull[abase]")
    else:
        concat_inputs = "".join(f"{video_inputs[i]}{audio_inputs[i]}" for i in range(segment_count))
        filters.append(f"{concat_inputs}concat=n={segment_count}:v=1:a=1[vc][ac]")
        filters.append(f"[vc]fps={fps}{caption_filter}[vcapped]")
        filters.append("[ac]anull[abase]")

    filters.extend(_audio_graph(spec, "[abase]", music_index, total_ms))
    filters.extend(
        _logo_filters(spec, "[vcapped]", width, height, logo_index)
        if logo_index is not None
        else ["[vcapped]null[vout]"]
    )

    codec_args = ["-c:v", spec.encode.ffmpeg_codec]
    if spec.encode.codec == "h264":
        codec_args += ["-preset", spec.encode.x264_preset, "-profile:v", "high", "-level", "4.2"]
    else:
        codec_args += ["-preset", "medium", "-tag:v", "hvc1"]
    args += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        *codec_args, "-crf", str(spec.encode.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", spec.encode.audio_bitrate, "-ar", str(AUDIO_RATE), "-ac", "2",
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
