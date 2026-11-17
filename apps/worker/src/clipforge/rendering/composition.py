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

    foreground: Rect | None = None
    if mode == "fit":
        foreground = fit_rect(source_aspect, target_aspect, 0.0)
    band = _caption_band(foreground, mode)
    if mode == "pip":
        band = CaptionBand(top_pct=0.76, bottom_pct=0.95)
    crop_x = 50.0
    if mode == "crop" and spec.video.crop_x_pct is not None:
        crop_x = spec.video.crop_x_pct
    elif mode == "crop" and content == "talking_head":
        crop_x = max(30.0, min(70.0, scene.subject_x * 100.0))
    crop_y = spec.video.crop_y_pct if spec.video.crop_y_pct is not None else 50.0
    crop_x = round(float(crop_x), 2)
    crop_y = round(float(crop_y), 2)
    if forced == "auto" and spec.video.aspect == "9:16" and source_aspect > target_aspect * 1.5:
        if content == "unknown":
            warnings.append("Wide source contained in a vertical frame; verify the subject has room.")

    return CompositionDecision(
        mode=mode,
        background="blur",
        content_type=content,
        confidence=round(float(confidence), 3),
        reason=reason,
        auto=auto,
        crop_x_pct=crop_x,
        crop_y_pct=crop_y,
        fit_margin_pct=0.0,
        foreground=foreground,
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
) -> CompositionPlan:
    """Split the edit into scene spans and attach a decision to every span."""
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
