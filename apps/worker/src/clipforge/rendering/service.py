"""Render orchestration: policy -> assets -> spec -> captions -> ffmpeg -> artefact."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Callable

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.errors import AppError, JobFailed
from clipforge.media.assets import visual_input
from clipforge.media.process import ProcessCancelled
from clipforge.media.probe import probe
from clipforge.quality.boundaries import review_ending
from clipforge.quality.gate import run_export_gate
from clipforge.ranking.sentences import build_transcript_model
from clipforge.rendering.composition import expand_composition
from clipforge.rendering.encode import validate_encoders
from clipforge.rendering.ffmpeg import build_command, execute
from clipforge.rendering.fonts import copy_fonts, resolve_font
from clipforge.rendering.spec import RenderSpec
from clipforge.rendering.subtitles import build_captions
from clipforge.storage.base import StorageBackend, StorageObjectMissing, ensure_local
from clipforge.storage.workspace import clear_tmp, ensure_layout, ensure_space

logger = logging.getLogger(__name__)

CAPTIONS_FILENAME = "captions.ass"
SIDECAR_FILENAME = "captions.srt"
OUTPUT_FILENAME = "out.mp4"
LOGO_FILENAME = "logo.png"
MUSIC_FILENAME = "music"
FONTS_DIR = "fonts"

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
    result: list[list[dict]] = [[] for _ in spans]
    for word in flat:
        word_start = int(word.get("start_ms", 0))
        word_end = int(word.get("end_ms", word_start))
        best_index = None
        best_overlap = 0
        for index, (start, end) in enumerate(spans):
            overlap = min(end, word_end) - max(start, word_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = index
        if best_index is not None:
            # A word belongs to exactly one output span even when a scene cut
            # splits a sentence. Duplicating it would repeat captions and audio.
            result[best_index].append(word)
    return result


def _snapshot_document(spec: RenderSpec) -> dict:
    snapshot = spec.profile_snapshot or {}
    document = snapshot.get("document") if isinstance(snapshot, dict) else None
    return document if isinstance(document, dict) else {}


def _policy_checks(spec: RenderSpec) -> None:
    document = _snapshot_document(spec)
    branding = document.get("branding") or {}
    captions = document.get("captions") or {}
    audio = document.get("audio") or {}

    if branding.get("logo_required") and not spec.logo.enabled:
        raise JobFailed(
            "logo_required",
            "This client profile requires a logo. Upload one or explicitly override the requirement.",
        )
    if captions.get("required") and not spec.captions.enabled:
        raise JobFailed(
            "captions_required",
            "This client profile requires captions. Turn them on or override the requirement.",
        )
    if spec.audio.mode == "replace" and not audio.get("allow_replacement"):
        raise JobFailed(
            "audio_replacement_not_permitted",
            "This profile does not permit replacing the original audio.",
        )


def _ending_review_for_render(
    repos: Repositories,
    *,
    project_id: str,
    candidate_id: str | None,
    source: dict,
    source_path: Path,
    cache_dir: Path,
    scene_fps: float | None = None,
    analysis_height: int = 144,
) -> dict | None:
    if not candidate_id:
        return None
    try:
        candidate = repos.candidates.get(project_id, candidate_id)
    except AppError:
        return None
    boundary = candidate.get("boundary") or {}
    if boundary.get("evidence"):
        return boundary
    transcript = repos.transcripts.latest(project_id)
    if transcript is None:
        return boundary or None
    model = build_transcript_model(
        transcript["segments"], int(source["duration_ms"]), transcript.get("language")
    )
    review = review_ending(
        candidate_start_ms=int(candidate["start_ms"]),
        candidate_end_ms=int(candidate["end_ms"]),
        sentences=model.sentences,
        source_path=source_path,
        source_duration_ms=int(source["duration_ms"]),
        cache_dir=cache_dir,
        scene_fps=scene_fps,
        analysis_height=analysis_height,
        source_height=source.get("height"),
    )
    repos.candidates.set_boundary(project_id, candidate_id, review)
    return review


def _asset_path(storage: StorageBackend, asset: dict) -> Path | None:
    try:
        return ensure_local(
            storage,
            asset.get("relative_path"),
            legacy_path=asset.get("storage_path"),
            missing_message="The configured asset is missing from storage.",
        )
    except StorageObjectMissing:
        return None


def _prepare_assets(spec: RenderSpec, repos: Repositories, storage: StorageBackend, work: Path) -> list[str]:
    warnings: list[str] = []

    if spec.logo.enabled and spec.logo.asset_id:
        asset = repos.assets.get(spec.logo.asset_id)
        if not asset.get("renderable"):
            if spec.logo.required:
                raise JobFailed(
                    "logo_not_renderable",
                    "The configured logo is an SVG, which FFmpeg cannot overlay. Export a transparent PNG.",
                )
            spec.logo.enabled = False
            warnings.append("The configured logo is an SVG and was skipped; upload a transparent PNG to render it.")
        else:
            source = _asset_path(storage, asset)
            if source is None:
                if spec.logo.required:
                    raise JobFailed("logo_missing", "The configured logo file is missing from storage.")
                spec.logo.enabled = False
                warnings.append("The configured logo file is missing and was skipped.")
            else:
                shutil.copyfile(source, work / LOGO_FILENAME)
                spec.logo.asset_name = LOGO_FILENAME

    if spec.audio.mode != "original":
        if not spec.audio.music_asset_id:
            raise JobFailed("music_missing", "This audio mode needs an approved music file.")
        asset = repos.assets.get(spec.audio.music_asset_id)
        if not asset.get("licence_confirmed"):
            raise JobFailed(
                "music_unlicensed",
                "The selected music has not been confirmed as licensed for this use. "
                "Confirm its licence before exporting.",
            )
        source = _asset_path(storage, asset)
        if source is None:
            raise JobFailed("music_missing", "The approved music file is missing from storage.")
        suffix = "." + asset["original_name"].rsplit(".", 1)[-1].lower()
        shutil.copyfile(source, work / f"{MUSIC_FILENAME}{suffix}")
        spec.audio.music_name = f"{MUSIC_FILENAME}{suffix}"

    return warnings


def _export_resolution_warnings(source: dict, spec: RenderSpec) -> list[str]:
    """Say when the retained source cannot support the requested export size.

    The retained source is the only export input. A proxy is smaller by design,
    so using one here would silently throw away detail the user chose to keep.
    """
    out_width, out_height = spec.output_dimensions
    source_width = int(source.get("width") or 0)
    source_height = int(source.get("height") or 0)
    if not source_width or not source_height:
        return []
    if out_width <= source_width and out_height <= source_height:
        return []
    factor = max(out_width / source_width, out_height / source_height)
    return [
        f"The export is {out_width}×{out_height} but the retained source is "
        f"{source_width}×{source_height}. The encoder upscales by about {factor:.2f}×, so no extra "
        "detail is recovered. Import a higher-quality source for a sharper export."
    ]


def perform_render(
    repos: Repositories,
    settings: Settings,
    storage: StorageBackend,
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
    _policy_checks(spec)
    if not source.get("has_video"):
        raise JobFailed(
            "no_video_stream",
            "This project's source has no video stream, so it cannot be rendered. "
            "Import the video again to export clips.",
        )
    # A codec this FFmpeg cannot run would fail halfway through a long encode.
    # Check the build first and fall back with an explicit note.
    spec.encode, encoder_note = validate_encoders(spec.encode)
    encode_warnings = [encoder_note] if encoder_note else []

    paths = ensure_layout(settings, project_id)
    clear_tmp(settings, project_id)
    work = paths / "tmp" / render_id
    work.mkdir(parents=True, exist_ok=True)
    try:
        source_path = ensure_local(
            storage,
            source.get("relative_path"),
            legacy_path=source.get("storage_path"),
            missing_message="The original video file is missing from this project.",
        )
    except StorageObjectMissing as exc:
        raise JobFailed("source_missing", exc.message) from exc

    ensure_space(settings, paths, required_bytes=400 * 1024 * 1024)
    logo_requested = bool(spec.logo.enabled)
    on_stage("assets", 2, "Checking brand assets and audio policy")
    warnings = [
        *encode_warnings,
        *_export_resolution_warnings(source, spec),
        *_prepare_assets(spec, repos, storage, work),
    ]
    overlay_report = {
        "requested": logo_requested,
        "applied": bool(spec.logo.enabled and spec.logo.asset_name == LOGO_FILENAME),
        "asset": spec.logo.asset_id,
        "reason": None if logo_requested == bool(spec.logo.enabled) else
        next((warning for warning in warnings if "logo" in warning.lower()), "The logo was skipped."),
    }

    on_stage("compose", 2.5, "Planning the layout from scene changes")
    download = source.get("download") or {}
    scene_fps = (download.get("processing") or {}).get("scene_fps")
    analysis_path, analysis_height = visual_input(repos, storage, source, source_path)
    plan = expand_composition(
        spec,
        analysis_path,
        source.get("width"),
        source.get("height"),
        source_duration_ms=int(source["duration_ms"]),
        cache_dir=paths / "analysis" / "scenes",
        scene_fps=scene_fps,
        analysis_height=analysis_height,
        on_progress=lambda index, total: on_stage(
            "compose",
            2.5 + (index / max(1, total)) * 1.5,
            f"Planning layout for scene {min(index + 1, total)} of {total}",
        ),
    )
    spec.segments = plan.segments
    spec.facecam_track = plan.facecam_track
    repos.renders.update_spec(render_id, spec.model_dump())
    warnings.extend(plan.warnings)
    warnings = list(dict.fromkeys(warnings))

    spans = [(segment.start_ms, segment.end_ms) for segment in spec.segments]
    compositions = [segment.composition for segment in spec.segments]
    captions_name: str | None = None
    sidecar_text: str | None = None
    caption_report: dict | None = None

    if spec.captions.enabled:
        transcript = repos.transcripts.latest(project_id)
        if transcript is None:
            warnings.append("Captions were requested but no transcript exists for this project.")
        else:
            words = _words_for_spans(transcript["segments"], spans)
            if any(words):
                width, height = spec.output_dimensions
                captions = build_captions(spans, words, spec, width, height, compositions)
                caption_report = captions.report.to_dict()
                warnings.extend(captions.report.warnings)
                if spec.captions.export_sidecar:
                    sidecar_text = captions.srt
                if spec.captions.burn_in:
                    (work / CAPTIONS_FILENAME).write_text(captions.ass, encoding="utf-8")
                    captions_name = CAPTIONS_FILENAME
                    copy_fonts(work / FONTS_DIR, resolve_font(
                        spec.captions.font_family, weight=spec.captions.weight, bold=spec.captions.bold
                    ))
            else:
                warnings.append("Captions were requested but this span has no word timings.")

    warnings = list(dict.fromkeys(warnings))
    on_stage("rendering", 5, "Encoding video")
    command = build_command(
        spec,
        source=source_path,
        cwd=work,
        has_audio=bool(source["has_audio"]),
        source_fps=source["fps"],
        captions_name=captions_name,
        output_name=OUTPUT_FILENAME,
        source_width=source.get("width"),
        source_height=source.get("height"),
    )

    def handle_progress(percent: float) -> None:
        on_stage("rendering", 5 + percent * 0.89, "Encoding video")

    try:
        outcome = execute(command, cancel=cancel_event, on_progress=handle_progress, timeout_seconds=2400)
    except ProcessCancelled:
        raise
    except (TimeoutError, RuntimeError) as exc:
        logger.error("Render failed: %s", exc)
        raise JobFailed(
            "render_failed",
            "The video encoder failed. This is usually a source format or asset issue; "
            "check the logo and music files, then try again.",
        ) from exc

    on_stage("validating", 95, "Checking the output file")
    output = work / OUTPUT_FILENAME
    if not output.exists() or output.stat().st_size < 4096:
        raise JobFailed("render_empty", "The encoder produced no usable file.")

    try:
        info = probe(output)
    except AppError as exc:
        raise JobFailed("render_invalid", "The rendered file could not be read back.") from exc

    on_stage("validating", 96, "Running the export quality gate")
    ending_review = _ending_review_for_render(
        repos,
        project_id=project_id,
        candidate_id=render.get("candidate_id"),
        source=source,
        source_path=analysis_path,
        cache_dir=paths / "analysis" / "scenes",
        scene_fps=scene_fps,
        analysis_height=analysis_height,
    )
    gate = run_export_gate(
        output_path=output,
        spec=spec,
        source=source,
        caption_report=caption_report,
        overlay_report=overlay_report,
        ending_review=ending_review,
        info=info,
    )
    if gate["blocking"]:
        # Keep the encoded draft and its review evidence. The draft is never
        # advertised as a finished render, but it can be downloaded for review.
        draft_key = f"projects/{project_id}/renders/{render_id}.draft.mp4"
        try:
            storage.put_file(draft_key, output, content_type="video/mp4")
            gate["draft_relative_path"] = draft_key
        except Exception as exc:
            logger.warning("Could not preserve the blocked render draft: %s", exc)
            gate["draft_error"] = "The draft could not be copied into durable storage."
        repos.renders.set_review(render_id, gate)
        raise JobFailed(
            "export_gate_failed",
            gate["summary"] + " The draft was kept so you can review the evidence and retry.",
        )
    repos.renders.set_review(render_id, gate)
    if gate["status"] == "warn":
        warnings.append(gate["summary"])

    final_key = f"projects/{project_id}/renders/{render_id}.mp4"
    subtitle_key = f"projects/{project_id}/renders/{render_id}.srt" if sidecar_text else None
    size_bytes = output.stat().st_size
    try:
        storage.put_file(final_key, output, content_type="video/mp4")
        if sidecar_text and subtitle_key:
            storage.put_bytes(subtitle_key, sidecar_text.encode("utf-8"), content_type="application/x-subrip")
        repos.renders.mark_completed(
            render_id,
            relative_path=final_key,
            size_bytes=size_bytes,
            duration_ms=info.duration_ms,
            width=info.width or 0,
            height=info.height or 0,
            subtitle_path=subtitle_key,
        )
    except Exception:
        # A partially written render is not advertised, and its objects must not
        # become paid orphans. Missing objects are already deleted.
        for key in (final_key, subtitle_key):
            if not key:
                continue
            try:
                storage.delete(key)
            except Exception:
                logger.warning("Could not remove the failed render object %s", key)
        raise
    if sidecar_text:
        warnings.append("Subtitles were exported as a separate .srt file.")
    on_stage("done", 100, "Render complete")

    return {
        "render_id": render_id,
        "duration_ms": info.duration_ms,
        "width": info.width,
        "height": info.height,
        "size_bytes": size_bytes,
        "subtitle_path": subtitle_key,
        "warnings": warnings,
        "review": gate,
        "encode_seconds": round(outcome.duration_seconds, 1),
    }
