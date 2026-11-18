"""Export quality gate.

The gate is intentionally evidence-first: it records what FFprobe and FFmpeg
actually measured and never claims a check passed because the render command
included the right filter. A failed gate blocks publication; a warning is shown
in the review report.
"""

from __future__ import annotations

import re
from pathlib import Path

from clipforge.media.process import ProcessFailed, require, run
from clipforge.media.probe import MediaInfo, probe
from clipforge.rendering.spec import RenderSpec

_BLACK_RE = re.compile(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)")
_FREEZE_RE = re.compile(r"freeze_start:([0-9.]+)\s+freeze_duration:([0-9.]+)")
MAX_BLACK_FRACTION = 0.35


def _check(identifier: str, label: str, status: str, detail: str, **values) -> dict:
    assert status in {"pass", "warn", "fail"}
    return {"id": identifier, "label": label, "status": status, "detail": detail, **values}


def _run_vf(path: Path, vf: str, timeout_seconds: float = 240) -> tuple[bool, str]:
    try:
        ffmpeg = require("ffmpeg")
        result = run(
            [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info",
                "-i", str(path), "-an", "-vf", vf, "-f", "null", "-",
            ],
            timeout_seconds=timeout_seconds,
        )
    except (ProcessFailed, OSError) as exc:
        return False, str(exc)
    return result.returncode == 0, result.stderr


def _decode_check(path: Path) -> tuple[bool, str]:
    try:
        ffmpeg = require("ffmpeg")
        result = run(
            [
                ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
                "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                "-f", "null", "-",
            ],
            timeout_seconds=300,
        )
    except (ProcessFailed, OSError) as exc:
        return False, str(exc)
    return result.returncode == 0, result.stderr


def _stream(info: MediaInfo, kind: str) -> dict:
    for stream in (info.raw.get("streams") or []):
        if stream.get("codec_type") == kind:
            return stream
    return {}


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def run_export_gate(
    *,
    output_path: Path,
    spec: RenderSpec,
    source: dict,
    caption_report: dict | None = None,
    overlay_report: dict | None = None,
    ending_review: dict | None = None,
    info: MediaInfo | None = None,
) -> dict:
    expected_width, expected_height = spec.video.dimensions
    expected_ms = spec.total_duration_ms
    checks: list[dict] = []

    try:
        info = info or probe(output_path)
    except Exception as exc:
        return {
            "version": 1,
            "status": "fail",
            "blocking": True,
            "checks": [_check("probe", "Output is readable", "fail", str(exc))],
            "summary": "The output could not be probed.",
        }

    video = _stream(info, "video")
    audio = _stream(info, "audio")
    checks.append(
        _check(
            "streams",
            "H.264 video and AAC audio",
            "pass" if info.video_codec == "h264" and (info.audio_codec == "aac" or not source.get("has_audio"))
            else "fail",
            f"Video {info.video_codec or 'missing'}; audio {info.audio_codec or 'missing'}.",
            video_codec=info.video_codec,
            audio_codec=info.audio_codec,
        )
    )
    checks.append(
        _check(
            "dimensions",
            "Output dimensions",
            "pass" if info.width == expected_width and info.height == expected_height else "fail",
            f"{info.width}x{info.height}; expected {expected_width}x{expected_height}.",
            actual=[info.width, info.height],
            expected=[expected_width, expected_height],
        )
    )

    target_fps = float(spec.video.fps or source.get("fps") or info.fps or 0)
    if target_fps and info.fps:
        difference = abs(info.fps - target_fps)
        checks.append(
            _check(
                "frame_rate",
                "Frame rate",
                "pass" if difference <= 0.5 else "warn",
                f"{info.fps:.3f} fps; source/request {target_fps:.3f} fps.",
                actual=info.fps,
                expected=target_fps,
            )
        )

    tolerance = max(1_000, expected_ms * 0.03)
    duration_ok = abs(info.duration_ms - expected_ms) <= tolerance
    checks.append(
        _check(
            "duration",
            "Duration",
            "pass" if duration_ok else "fail",
            f"{info.duration_ms / 1000:.2f}s; expected {expected_ms / 1000:.2f}s.",
            actual_ms=info.duration_ms,
            expected_ms=expected_ms,
        )
    )

    target = spec.duration or {}
    target_min = int(target.get("min_ms") or 0)
    target_max = int(target.get("max_ms") or 0)
    if target_min and target_max:
        inside = target_min <= info.duration_ms <= target_max
        checks.append(
            _check(
                "duration_target",
                "Editorial duration target",
                "pass" if inside else "warn",
                (
                    f"{info.duration_ms / 1000:.1f}s fits the {target.get('label', 'selected')} target."
                    if inside
                    else f"{info.duration_ms / 1000:.1f}s is outside the "
                    f"{target_min / 1000:.0f}–{target_max / 1000:.0f}s target; "
                    "the cut was not padded or truncated to force it."
                ),
                actual_ms=info.duration_ms,
                target_min_ms=target_min,
                target_max_ms=target_max,
                preset=target.get("preset"),
            )
        )

    decoded, decode_stderr = _decode_check(output_path)
    checks.append(
        _check(
            "decode",
            "Full-decode check",
            "pass" if decoded else "fail",
            "Decoded every frame and audio packet." if decoded else (decode_stderr[-400:] or "Decode failed."),
        )
    )

    has_audio = bool(info.has_audio)
    audio_ok = has_audio if source.get("has_audio") else True
    checks.append(
        _check(
            "audio_stream",
            "Audio track",
            "pass" if audio_ok else "fail",
            "Audio track present." if has_audio else "The output has no audio track.",
        )
    )

    if audio:
        video_start = _float(video.get("start_time")) or 0.0
        audio_start = _float(audio.get("start_time")) or 0.0
        start_delta = abs(video_start - audio_start)
        video_duration = _float(video.get("duration")) or info.duration_ms / 1000
        audio_duration = _float(audio.get("duration")) or video_duration
        duration_delta = abs(video_duration - audio_duration)
        sync_status = "pass" if start_delta <= 0.12 and duration_delta <= 0.35 else "warn"
        if start_delta > 0.5 or duration_delta > 0.75:
            sync_status = "fail"
        checks.append(
            _check(
                "av_sync",
                "Audio/video sync",
                sync_status,
                f"Stream starts differ by {start_delta * 1000:.0f} ms; durations differ by "
                f"{duration_delta * 1000:.0f} ms.",
                start_delta_ms=round(start_delta * 1000),
                duration_delta_ms=round(duration_delta * 1000),
            )
        )
    else:
        checks.append(_check("av_sync", "Audio/video sync", "pass", "No audio stream to synchronise."))

    black_ok, black_output = _run_vf(output_path, "blackdetect=d=0.4:pix_th=0.10")
    black_frames = [
        {"start": float(start), "end": float(end), "duration": float(end) - float(start)}
        for start, end in _BLACK_RE.findall(black_output)
    ]
    black_total = sum(frame["duration"] for frame in black_frames)
    black_fraction = black_total / max(0.001, info.duration_ms / 1000)
    if not black_ok:
        black_status = "warn"
        black_detail = "Black-frame detection could not run."
    elif black_fraction >= MAX_BLACK_FRACTION:
        black_status = "fail"
        black_detail = f"{black_total:.1f}s of black frames ({black_fraction:.0%} of the output)."
    elif black_frames:
        black_status = "warn"
        black_detail = f"{len(black_frames)} black segment(s), {black_total:.1f}s total."
    else:
        black_status = "pass"
        black_detail = "No black segments longer than 0.4s."
    checks.append(_check("black_frames", "Black frames", black_status, black_detail, frames=black_frames))

    freeze_ok, freeze_output = _run_vf(output_path, "freezedetect=n=0.003:d=1.5")
    freeze_frames = [
        {"start": float(start), "duration": float(duration)}
        for start, duration in _FREEZE_RE.findall(freeze_output)
    ]
    if not freeze_ok:
        freeze_status = "warn"
        freeze_detail = "Freeze detection could not run."
    elif freeze_frames:
        freeze_status = "warn"
        freeze_detail = f"{len(freeze_frames)} frozen segment(s): " + ", ".join(
            f"{frame['start']:.1f}s ({frame['duration']:.1f}s)" for frame in freeze_frames[:6]
        )
    else:
        freeze_status = "pass"
        freeze_detail = "No frozen segments longer than 1.5s."
    checks.append(_check("freeze_frames", "Freeze frames", freeze_status, freeze_detail, frames=freeze_frames))

    report = caption_report or {}
    if spec.captions.enabled and spec.captions.burn_in:
        width = report.get("max_line_width_px") or 0.0
        limit = report.get("max_line_width_limit_px") or 0.0
        if not report.get("used"):
            caption_status, caption_detail = "warn", "Captions were requested but no word timings were rendered."
        elif width > expected_width:
            caption_status, caption_detail = "fail", f"Caption line is {width:.0f}px wide in a {expected_width}px frame."
        elif limit and width > limit + 1:
            caption_status, caption_detail = "warn", f"Caption line is {width:.0f}px, above the {limit:.0f}px safe width."
        elif report.get("bounds_ok") is False:
            caption_status, caption_detail = "warn", "Captions overflow their safe vertical band."
        else:
            caption_status, caption_detail = "pass", "Captions fit their measured safe area."
        checks.append(_check("captions", "Caption bounds", caption_status, caption_detail,
                             width_px=width, limit_px=limit, report=report))
    else:
        checks.append(_check("captions", "Caption bounds", "pass", "Burned-in captions are off."))

    composition_warnings = list(dict.fromkeys(
        warning
        for segment in spec.segments
        if segment.composition is not None
        for warning in segment.composition.warnings
    ))
    if composition_warnings:
        checks.append(
            _check(
                "composition",
                "Composition review",
                "warn",
                " ".join(composition_warnings[:4])[:600],
                warnings=composition_warnings[:12],
            )
        )
    else:
        checks.append(_check("composition", "Composition review", "pass", "No composition warnings."))

    overlays = overlay_report or {}
    if overlays.get("requested"):
        overlay_status = "pass" if overlays.get("applied") else "fail"
        checks.append(
            _check(
                "overlays",
                "Logo overlay",
                overlay_status,
                "Logo was applied." if overlays.get("applied") else overlays.get("reason") or "Logo was not applied.",
                asset=overlays.get("asset"),
            )
        )
    else:
        checks.append(_check("overlays", "Logo overlay", "pass", "No logo requested."))

    if ending_review:
        suspicious = bool(ending_review.get("suspicious"))
        reviewed = ending_review.get("reviewed") or "pending"
        evidence = ending_review.get("evidence") or []
        if suspicious and reviewed == "pending":
            ending_status = "warn"
            ending_detail = (
                "Possible trailing or unrelated shot at the out point. Review the ending evidence "
                "before publishing."
            )
        elif suspicious:
            ending_status = "pass"
            ending_detail = f"Ending review was {reviewed}; the out point is a deliberate choice."
        else:
            ending_status = "pass"
            ending_detail = "No strong evidence of an unrelated trailing shot."
        checks.append(
            _check(
                "ending",
                "Ending review",
                ending_status,
                ending_detail,
                suspicious=suspicious,
                reviewed=reviewed,
                evidence=evidence[:8],
                alternatives=ending_review.get("alternatives") or [],
            )
        )
    else:
        checks.append(_check("ending", "Ending review", "pass", "No candidate was linked to this render."))

    statuses = {check["status"] for check in checks}
    status = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    failures = [check["label"] for check in checks if check["status"] == "fail"]
    warnings = [check["label"] for check in checks if check["status"] == "warn"]
    summary = (
        "Export gate passed."
        if status == "pass"
        else "Export gate failed: " + ", ".join(failures)
        if failures
        else "Export gate passed with warnings: " + ", ".join(warnings)
    )
    return {
        "version": 1,
        "status": status,
        "blocking": status == "fail",
        "summary": summary,
        "checks": checks,
        "edl": spec.to_edl(),
        "output": {
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "duration_ms": info.duration_ms,
            "size_bytes": info.size_bytes,
            "video_codec": info.video_codec,
            "audio_codec": info.audio_codec,
        },
    }
