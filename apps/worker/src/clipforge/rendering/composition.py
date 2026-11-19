"""Content-aware composition decisions and scene-level render planning.

The first pass is intentionally deterministic. FFmpeg finds cuts, a small set of
frame features classifies each scene, and a rule chooses one of:

* ``crop`` for talking-head and other real-world footage where the subject is
  centred and the target has the same orientation;
* ``fit`` for slides, charts, diagrams, tables, screen recordings and anything
  with information near the edges;
* ``pip``/``split`` are reserved for a later pass and currently degrade to fit
  with a warning rather than claiming a layout the renderer cannot honour.

Every decision can be overridden manually. The plan is persisted in the render
spec before encoding, so the preview and the final render read the same data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from clipforge.media.visual import Scene, SceneAnalysis, analyse_source_window
from clipforge.rendering.spec import (
    CaptionBand,
    CompositionDecision,
    Rect,
    RenderSpec,
    SegmentSpan,
)

logger = logging.getLogger(__name__)
MAX_RENDER_SEGMENTS = 40
ProgressCallback = Callable[[int, int], None]


@dataclass
class CompositionPlan:
    segments: list[SegmentSpan]
    warnings: list[str] = field(default_factory=list)
    analyses: list[SceneAnalysis] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "segments": [
                {
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "role": segment.role,
                    "composition": segment.composition.model_dump() if segment.composition else None,
                }
                for segment in self.segments
            ],
            "warnings": list(dict.fromkeys(self.warnings)),
            "scenes": [
                {
                    "start_ms": scene.start_ms,
                    "end_ms": scene.end_ms,
                    "content_type": scene.content_type,
                    "confidence": scene.confidence,
                    "subject_x": scene.subject_x,
                    "warnings": scene.warnings,
                }
                for analysis in self.analyses
                for scene in analysis.scenes
            ],
        }


def fit_rect(source_aspect: float, target_aspect: float, margin_pct: float = 0.0) -> Rect:
    """Normalised output rectangle for a contained source image."""
    margin = max(0.0, min(0.20, margin_pct))
    if source_aspect <= 0 or target_aspect <= 0:
        return Rect(x_pct=margin, y_pct=margin, width_pct=1 - 2 * margin, height_pct=1 - 2 * margin)
    if source_aspect > target_aspect:
        width = 1.0 - 2 * margin
        height = width * target_aspect / source_aspect
        return Rect(x_pct=margin, y_pct=(1.0 - height) / 2, width_pct=width, height_pct=height)
    height = 1.0 - 2 * margin
    width = height * source_aspect / target_aspect
    return Rect(x_pct=(1.0 - width) / 2, y_pct=margin, width_pct=width, height_pct=height)


def _caption_band(foreground: Rect | None, mode: str) -> CaptionBand:
    if mode == "fit" and foreground is not None:
        top = min(0.84, foreground.y_pct + foreground.height_pct + 0.015)
        bottom = 0.95
        if bottom - top < 0.10:
            top = 0.82
            bottom = 0.97
        return CaptionBand(top_pct=top, bottom_pct=bottom)
    return CaptionBand(top_pct=0.62, bottom_pct=0.90)


def _facecam_rect(scene: Scene) -> Rect | None:
    """Return the detected corner webcam plate, if the heuristic found one."""

    if float(scene.features.get("facecam_present", 0.0)) < 0.5:
        return None
    x = float(scene.features.get("facecam_x", 0.0))
    y = float(scene.features.get("facecam_y", 0.0))
    width = float(scene.features.get("facecam_width", 0.0))
    height = float(scene.features.get("facecam_height", 0.0))
    if width <= 0.0 or height <= 0.0:
        return None
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    width = max(0.05, min(1.0 - x, width))
    height = max(0.05, min(1.0 - y, height))
    return Rect(x_pct=x, y_pct=y, width_pct=width, height_pct=height)


def decide_composition(
    scene: Scene,
    spec: RenderSpec,
    source_width: int,
    source_height: int,
    *,
    forced: str | None = None,
) -> CompositionDecision:
    width, height = spec.video.dimensions
    target_aspect = width / height
    source_aspect = (source_width or 16) / max(1, source_height or 9)
    forced = forced or spec.video.composition
    warnings = list(scene.warnings)
    content = scene.content_type
    confidence = scene.confidence
    reason = ""
    foreground: Rect | None = None
    source_rect: Rect | None = None
    facecam: Rect | None = None

    if forced == "crop":
        mode = "crop"
        reason = "Crop selected for this export."
        auto = False
    elif forced == "fit":
        mode = "fit"
        reason = "Contain selected for this export."
        auto = False
    elif forced == "pip":
        mode = "pip"
        reason = "Picture-in-picture selected for this export."
        auto = False
    elif forced == "stack":
        facecam = _facecam_rect(scene)
        if facecam is None:
            mode = "crop"
            reason = "The stacked layout needs a face-cam; none was detected, so the frame is filled instead."
            warnings.append(
                "No face-cam corner was detected, so the gameplay + face-cam stack fell back to a "
                "full-screen crop. Use a different layout or set a crop focus."
            )
        else:
            mode = "stack"
            reason = "Gameplay + face-cam stack selected; the detected corner webcam sits below the game."
        auto = False
    elif forced == "facecam_focus":
        facecam = _facecam_rect(scene)
        mode = "crop"
        auto = False
        if facecam is None:
            reason = "Face-cam focus was requested, but no face-cam corner was detected; the frame is filled."
            warnings.append(
                "No face-cam corner was detected, so face-cam focus fell back to a full-screen crop. "
                "Use a different layout or set a crop focus."
            )
        else:
            source_rect = facecam
            reason = "Face-cam focus crops to the detected corner webcam."
    elif forced == "split":
        mode = "fit"
        reason = "Split/stacked needs two simultaneous sources; this single source is contained."
        auto = False
        warnings.append("Split/stacked is unavailable for a single source; the scene is contained instead.")
    elif abs(target_aspect - source_aspect) / max(0.01, source_aspect) <= 0.02:
        mode = "crop"
        reason = "The source already matches the output aspect; cropping loses nothing."
        auto = True
    elif content in {"slide", "screen_recording"}:
        mode = "fit"
        reason = (
            "The scene has slide/screen characteristics (bright, high edge density, "
            "content near the edges). Containing it protects the information."
        )
        auto = True
    elif content == "mixed":
        mode = "fit"
        reason = "The scene mixes picture and on-screen content; containing it protects both."
        auto = True
    elif content == "talking_head":
        mode = "crop"
        reason = "A centred speaker was detected; a full-height crop keeps the subject large."
        auto = True
    else:
        mode = "fit"
        confidence = max(0.25, min(confidence, 0.55))
        reason = "The scene type was uncertain; containing it is the safe default."
        warnings.append("Layout chosen conservatively because the scene type was not clear.")
        auto = True

    if confidence < 0.55 and mode == "fit":
        warnings.append("Low-confidence layout; review the composition before publishing.")

    if mode == "fit":
        text_at_edges = float(scene.features.get("text_at_edges", 0.0)) > 0.5
        content_width = float(scene.features.get("content_width", 1.0))
        content_height = float(scene.features.get("content_height", 1.0))
        if (
            content in {"slide", "screen_recording", "mixed", "unknown"}
            and not text_at_edges
            and content_width * content_height < 0.92
            and content_width >= 0.50
            and content_height >= 0.40
        ):
            source_rect = Rect(
                x_pct=float(scene.features.get("content_x", 0.0)),
                y_pct=float(scene.features.get("content_y", 0.0)),
                width_pct=content_width,
                height_pct=content_height,
            )
        cropped_source_aspect = source_aspect * (
            (source_rect.width_pct / source_rect.height_pct) if source_rect else 1.0
        )
        foreground = fit_rect(cropped_source_aspect, target_aspect, 0.0)
        cropped_width = max(1.0, source_width * (source_rect.width_pct if source_rect else 1.0))
        fit_scale = min(
            width * foreground.width_pct / cropped_width,
            height * foreground.height_pct / max(1.0, source_height * (source_rect.height_pct if source_rect else 1.0)),
        )
        if source_rect is not None:
            warnings.append(
                "Empty source margins were cropped before containing the evidence; review that no labels "
                "or context were removed."
            )
        edge_density = float(scene.features.get("edge_density", 0.0))
        if content in {"slide", "screen_recording", "mixed"} and fit_scale < 0.75 and edge_density >= 0.06:
            warnings.append(
                f"Evidence is contained at {fit_scale:.0%} of its source size; check that its text is "
                "readable. Use a closer crop or manual composition when it is not."
            )
    elif mode == "crop" and float(scene.features.get("text_at_edges", 0.0)) > 0.5:
        warnings.append("Information reaches the left/right edges; review the crop for cut-off text.")
    band = _caption_band(foreground, mode)
    if mode == "pip":
        band = CaptionBand(top_pct=0.76, bottom_pct=0.95)
    if mode == "stack":
        # Captions sit over the lower third of the gameplay, not over the face-cam.
        band = CaptionBand(top_pct=0.40, bottom_pct=0.58)
    background = "blur" if mode == "pip" else "solid"
    background_color = "#0B0D11" if background == "solid" else "#000000"
    crop_x = 50.0
    if mode == "crop" and spec.video.crop_x_pct is not None:
        crop_x = spec.video.crop_x_pct
    elif mode == "crop" and content == "talking_head" and forced != "facecam_focus":
        crop_x = max(30.0, min(70.0, scene.subject_x * 100.0))
    crop_y = spec.video.crop_y_pct if spec.video.crop_y_pct is not None else 50.0
    crop_x = round(float(crop_x), 2)
    crop_y = round(float(crop_y), 2)
    if forced == "auto" and spec.video.aspect == "9:16" and source_aspect > target_aspect * 1.5:
        if content == "unknown":
            warnings.append("Wide source contained in a vertical frame; verify the subject has room.")

    return CompositionDecision(
        mode=mode,
        background=background,
        background_color=background_color,
        content_type=content,
        confidence=round(float(confidence), 3),
        reason=reason,
        auto=auto,
        crop_x_pct=crop_x,
        crop_y_pct=crop_y,
        fit_margin_pct=0.0,
        foreground=foreground,
        facecam=facecam,
        source_rect=source_rect,
        caption_band=band,
        warnings=list(dict.fromkeys(warnings)),
    )


def _merge_pair(segments: list[SegmentSpan], index: int, reason: str) -> None:
    left, right = segments[index], segments[index + 1]
    left_decision = left.composition
    right_decision = right.composition
    if left_decision and right_decision:
        keep_left = left.duration_ms >= right.duration_ms
        source = left_decision if keep_left else right_decision
        decision = source.model_copy(deep=True)
        decision.warnings = list(dict.fromkeys(
            decision.warnings + left_decision.warnings + right_decision.warnings + [reason]
        ))
        left.composition = decision
    left.end_ms = right.end_ms
    segments.pop(index + 1)


def _cap_segments(segments: list[SegmentSpan], warnings: list[str]) -> list[SegmentSpan]:
    while len(segments) > MAX_RENDER_SEGMENTS:
        merged = False
        for index in range(len(segments) - 1):
            left, right = segments[index], segments[index + 1]
            if (
                left.composition
                and right.composition
                and left.composition.mode == right.composition.mode
                and left.composition.content_type == right.composition.content_type
            ):
                _merge_pair(segments, index, "Merged adjacent scenes with the same layout.")
                merged = True
                break
        if not merged:
            # Merge the shortest adjacent pair. This is the least destructive
            # way to honour the spec's 40-segment limit; the warning stays in
            # the review report.
            index = min(
                range(len(segments) - 1),
                key=lambda item: segments[item].duration_ms + segments[item + 1].duration_ms,
            )
            _merge_pair(segments, index, "Merged short adjacent scenes to fit the render segment limit.")
            warnings.append("The scene plan exceeded 40 segments and was simplified; review the layout plan.")
    return segments


def _scenes_for_segment(analysis: SceneAnalysis, start_ms: int, end_ms: int) -> list[Scene]:
    scenes: list[Scene] = []
    for scene in analysis.scenes:
        scene_start = max(start_ms, scene.start_ms)
        scene_end = min(end_ms, scene.end_ms)
        if scene_end > scene_start:
            scenes.append(Scene(
                start_ms=scene_start,
                end_ms=scene_end,
                content_type=scene.content_type,
                confidence=scene.confidence,
                subject_x=scene.subject_x,
                features=scene.features,
                warnings=scene.warnings,
            ))
    if not scenes:
        scenes.append(Scene(
            start_ms=start_ms,
            end_ms=end_ms,
            content_type="unknown",
            confidence=0.3,
            subject_x=0.5,
            warnings=["No scene analysis was available for this span."],
        ))
    return scenes


def expand_composition(
    spec: RenderSpec,
    source_path: Path,
    source_width: int | None,
    source_height: int | None,
    *,
    source_duration_ms: int | None = None,
    cache_dir: Path | None = None,
    on_progress: ProgressCallback | None = None,
    scene_fps: float | None = None,
    analysis_height: int = 144,
) -> CompositionPlan:
    """Split the edit into scene spans and attach a decision to every span.

    ``source_path`` may be a low-resolution proxy: framing maths still uses the
    real source dimensions, while ``analysis_height`` records what the frames
    were actually sampled at.
    """
    segments = [segment.model_copy(deep=True) for segment in spec.segments]
    warnings: list[str] = []
    analyses: list[SceneAnalysis] = []
    width = int(source_width or 1920)
    height = int(source_height or 1080)
    if not source_width or not source_height:
        warnings.append("Source dimensions were unknown; composition assumed 1920x1080.")

    forced = spec.video.composition
    if forced == "manual":
        for segment in segments:
            if segment.composition is None:
                scene = Scene(
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    content_type="unknown",
                    confidence=0.4,
                    subject_x=0.5,
                )
                segment.composition = decide_composition(scene, spec, width, height, forced="crop")
        warnings.append("Manual composition is active; only segments without an override use crop.")
        return CompositionPlan(segments=segments, warnings=warnings)

    if forced in {"crop", "fit", "pip", "split"}:
        for segment in segments:
            scene = Scene(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                content_type="unknown",
                confidence=1.0,
                subject_x=0.5,
            )
            segment.composition = decide_composition(scene, spec, width, height, forced=forced)
        return CompositionPlan(segments=segments, warnings=warnings)

    if forced in {"stack", "facecam_focus"}:
        # A face-cam is a property of the source, not of a scene boundary, so
        # detect it once across the whole edit and reuse the decision. That also
        # keeps the layout stable and avoids splitting the render into dozens of
        # identical segments.
        window_start = max(0, min(segment.start_ms for segment in segments) - 250)
        window_end = max(segment.end_ms for segment in segments) + 250
        best: Scene | None = None
        try:
            analysis = analyse_source_window(
                source_path,
                window_start,
                window_end,
                cache_dir=cache_dir,
                source_duration_ms=source_duration_ms,
                scene_fps=scene_fps,
                analysis_height=analysis_height,
                source_height=source_height,
            )
            analyses.append(analysis)
            warnings.extend(analysis.warnings)
            if analysis.scenes:
                best = max(
                    analysis.scenes,
                    key=lambda scene: float(scene.features.get("facecam_confidence", 0.0)),
                )
        except Exception as exc:  # visual analysis must never block an export
            logger.warning("Face-cam analysis failed for %s-%s: %s", window_start, window_end, exc)
            warnings.append("Face-cam analysis failed; the requested layout falls back to a full crop.")
        if best is None:
            best = Scene(
                start_ms=segments[0].start_ms,
                end_ms=segments[0].end_ms,
                content_type="unknown",
                confidence=0.3,
                subject_x=0.5,
            )
        decision = decide_composition(best, spec, width, height, forced=forced)
        for segment in segments:
            segment.composition = decision.model_copy(deep=True)
        return CompositionPlan(
            segments=segments,
            warnings=list(dict.fromkeys(warnings + decision.warnings)),
            analyses=analyses,
        )

    if forced != "auto":
        # Legacy documents keep their global framing chain.
        warnings.append("This render uses the legacy global framing setting; scene-aware layout is off.")
        return CompositionPlan(segments=segments, warnings=warnings)

    expanded: list[SegmentSpan] = []
    total = len(segments)
    for index, segment in enumerate(segments):
        if on_progress:
            on_progress(index, total)
        if segment.composition is not None:
            # A user override is authoritative; do not split the span or
            # replace the chosen layout with an automatic decision.
            expanded.append(segment)
            continue
        window_start = max(0, segment.start_ms - 250)
        window_end = segment.end_ms + 250
        try:
            analysis = analyse_source_window(
                source_path,
                window_start,
                window_end,
                cache_dir=cache_dir,
                source_duration_ms=source_duration_ms,
                scene_fps=scene_fps,
                analysis_height=analysis_height,
                source_height=source_height,
            )
        except Exception as exc:  # visual analysis must never block an export
            logger.warning("Scene analysis failed for %s-%s: %s", segment.start_ms, segment.end_ms, exc)
            warnings.append("Scene analysis failed; using a safe contained layout for this span.")
            analysis = SceneAnalysis(
                scenes=[Scene(segment.start_ms, segment.end_ms, "unknown", 0.3, 0.5)],
                samples=[],
                cuts=[],
                warnings=["Scene analysis unavailable."],
            )
        analyses.append(analysis)
        warnings.extend(analysis.warnings)
        scenes = _scenes_for_segment(analysis, segment.start_ms, segment.end_ms)
        for scene in scenes:
            decision = decide_composition(scene, spec, width, height)
            expanded.append(SegmentSpan(
                start_ms=scene.start_ms,
                end_ms=scene.end_ms,
                role=segment.role,
                composition=decision,
            ))

    merged: list[SegmentSpan] = []
    for segment in expanded:
        if (
            merged
            and merged[-1].role == segment.role
            and merged[-1].composition
            and segment.composition
            and merged[-1].composition.mode == segment.composition.mode
            and merged[-1].composition.content_type == segment.composition.content_type
            and merged[-1].composition.caption_band == segment.composition.caption_band
            and merged[-1].composition.auto
            and segment.composition.auto
        ):
            merged[-1].end_ms = segment.end_ms
            merged[-1].composition.warnings = list(dict.fromkeys(
                merged[-1].composition.warnings + segment.composition.warnings
            ))
        else:
            merged.append(segment)
    merged = _cap_segments(merged, warnings)
    if on_progress:
        on_progress(total, total)
    return CompositionPlan(segments=merged, warnings=warnings, analyses=analyses)
