"""Render orchestration: spec -> captions -> ffmpeg -> validated artefact."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.errors import AppError, JobFailed
from clipforge.media.process import ProcessCancelled
from clipforge.media.probe import probe
from clipforge.rendering.ffmpeg import build_command, execute
from clipforge.rendering.spec import RenderSpec
from clipforge.rendering.subtitles import build_ass
from clipforge.storage.workspace import clear_tmp, ensure_layout, ensure_space

logger = logging.getLogger(__name__)

CAPTIONS_FILENAME = "captions.ass"
OUTPUT_FILENAME = "out.mp4"
DURATION_TOLERANCE_MS = 2_500

StageCallback = Callable[[str, float, str], None]


def _words_for_spans(segments: list[dict], spans: list[tuple[int, int]]) -> list[list[dict]]:
    flat: list[dict] = []
    for segment in segments:
        words = segment.get("words") or []
        if words:
            flat.extend(words)
        else:
            flat.append({"start_ms": segment.get("start_ms", 0), "end_ms": segment.get("end_ms", 0),
                         "word": segment.get("text", "")})
    result: list[list[dict]] = []
    for start, end in spans:
        result.append([w for w in flat if int(w.get("end_ms", 0)) > start and int(w.get("start_ms", 0)) < end])
    return result


def perform_render(
    repos: Repositories,
    settings: Settings,
    *,
    render_id: str,
    on_stage: StageCallback,
    cancel_event: threading.Event,
    should_cancel: Callable[[], bool],
) -> dict:
    render = repos.renders.get_raw(render_id)
    spec = RenderSpec.model_validate(json.loads(render["spec_json"] or "{}"))
    project_id = render["project_id"]
    source = repos.sources.get(project_id)

    try:
        spec.validate_against(int(source["duration_ms"]))
    except ValueError as exc:
        raise JobFailed("invalid_render_spec", str(exc)) from exc

    paths = ensure_layout(settings, project_id)
    clear_tmp(settings, project_id)
    work = paths / "tmp" / render_id
    work.mkdir(parents=True, exist_ok=True)
    source_path = Path(source["storage_path"])
    if not source_path.exists():
        raise JobFailed("source_missing", "The original video file is missing from this project.")

    ensure_space(settings, paths, required_bytes=400 * 1024 * 1024)

    spans = [(segment.start_ms, segment.end_ms) for segment in spec.segments]
    captions_name: str | None = None
    warnings: list[str] = []

    if spec.captions.enabled:
        transcript = repos.transcripts.latest(project_id)
        if transcript is None:
            warnings.append("Captions were requested but no transcript exists for this project.")
        else:
            words = _words_for_spans(transcript["segments"], spans)
            if any(words):
                width, height = spec.video.dimensions
                ass = build_ass(spans, words, spec, width, height)
                (work / CAPTIONS_FILENAME).write_text(ass, encoding="utf-8")
                captions_name = CAPTIONS_FILENAME
            else:
                warnings.append("Captions were requested but this span has no word timings.")

    on_stage("rendering", 4, "Encoding video")
    command = build_command(
        spec,
        source=source_path,
        cwd=work,
        has_audio=bool(source["has_audio"]),
        source_fps=source["fps"],
        captions_name=captions_name,
        output_name=OUTPUT_FILENAME,
    )

    def handle_progress(percent: float) -> None:
        on_stage("rendering", 4 + percent * 0.9, "Encoding video")

    try:
        outcome = execute(command, cancel=cancel_event, on_progress=handle_progress, timeout_seconds=2400)
    except ProcessCancelled:
        raise
    except (TimeoutError, RuntimeError) as exc:
        logger.error("Render failed: %s", exc)
        raise JobFailed(
            "render_failed",
            "The video encoder failed. This is usually a source format issue; try a different clip or preset.",
        ) from exc

    on_stage("validating", 95, "Checking the output file")
    output = work / OUTPUT_FILENAME
    if not output.exists() or output.stat().st_size < 4096:
        raise JobFailed("render_empty", "The encoder produced no usable file.")

    try:
        info = probe(output)
    except AppError as exc:
        raise JobFailed("render_invalid", "The rendered file could not be read back.") from exc

    expected = spec.total_duration_ms
    if abs(info.duration_ms - expected) > max(DURATION_TOLERANCE_MS, expected * 0.05):
        raise JobFailed(
            "render_duration_mismatch",
            f"The rendered clip is {info.duration_ms // 1000}s but {expected // 1000}s was expected.",
        )
    if not info.has_audio:
        raise JobFailed("render_no_audio", "The rendered file has no audio track.")

    final_dir = paths / "renders"
    final_dir.mkdir(parents=True, exist_ok=True)
    final_path = final_dir / f"{render_id}.mp4"
    output.replace(final_path)

    relative = final_path.relative_to(settings.data_dir.resolve()).as_posix()
    repos.renders.mark_completed(
        render_id,
        relative_path=relative,
        size_bytes=final_path.stat().st_size,
        duration_ms=info.duration_ms,
        width=info.width or 0,
        height=info.height or 0,
    )
    on_stage("done", 100, "Render complete")

    return {
        "render_id": render_id,
        "duration_ms": info.duration_ms,
        "width": info.width,
        "height": info.height,
        "size_bytes": final_path.stat().st_size,
        "warnings": warnings,
        "encode_seconds": round(outcome.duration_seconds, 1),
    }
