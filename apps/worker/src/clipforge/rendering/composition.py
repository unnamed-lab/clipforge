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

from clipforge.media.facecam import (
    STATE_MANUAL,
    STATE_MISSING,
    STATE_UNCERTAIN,
    STATE_VISIBLE,
    TrackRange,
    ranges_for_span,
)
from clipforge.media.visual import Scene, SceneAnalysis, analyse_source_window
from clipforge.rendering.spec import (
    CaptionBand,
    CompositionDecision,
    FaceCamObservation as SpecFaceCamObservation,
    FaceCamRange as SpecFaceCamRange,
    FaceCamTrack as SpecFaceCamTrack,
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
    facecam_track: SpecFaceCamTrack | None = None

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
            "facecam_track": self.facecam_track.model_dump() if self.facecam_track else None,
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


def _facecam_rect(scene: Scene, manual: Rect | None = None) -> Rect | None:
    """Return a detected webcam panel when no time-indexed track was supplied.

    The tracked path passes an explicit rectangle, so this fallback is mainly
    for legacy analyses and direct scene decisions. A manual rectangle always
    wins.
    """

    if manual is not None:
        return manual
    if float(scene.features.get("facecam_present", 0.0)) < 0.5:
        return None
    if float(scene.features.get("facecam_confidence", 0.0)) < 0.35:
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


def _stack_caption_band(position: str, split_pct: float) -> CaptionBand:
    """Keep stacked captions inside the gameplay panel, never over the face.

    The gameplay panel is the upper panel when the face-cam sits below and the
    lower panel when it sits above. The band is derived from the same split the
    encoder uses, so the guide and the rendered result cannot drift apart.
    """

    game_height = max(0.05, min(0.95, split_pct / 100.0))
    game_top = 0.0 if position == "bottom" else 1.0 - game_height
    top = game_top + game_height * 0.62
    bottom = min(0.94, game_top + game_height * 0.93)
    if bottom - top < 0.10:
        bottom = min(0.96, top + 0.10)
    return CaptionBand(top_pct=round(top, 4), bottom_pct=round(bottom, 4))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _rect_area(rect: Rect) -> float:
    return max(0.0, rect.width_pct) * max(0.0, rect.height_pct)


def _intersection_area(left: Rect, right: Rect) -> float:
    x0 = max(left.x_pct, right.x_pct)
    y0 = max(left.y_pct, right.y_pct)
    x1 = min(left.x_pct + left.width_pct, right.x_pct + right.width_pct)
    y1 = min(left.y_pct + left.height_pct, right.y_pct + right.height_pct)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def _expand_rect_to_aspect(
    rect: Rect,
    target_aspect: float,
    source_width: int,
    source_height: int,
) -> Rect:
    """Grow a normalised source rectangle to a panel aspect and keep it in frame.

    A face-cam plate is detected in source space; the output panel has its own
    aspect. Growing around the plate's centre means the panel can be filled
    without stretching and without cutting the face. When the plate sits near an
    edge the window slides inwards instead of becoming distorted.
    """

    if source_width <= 0 or source_height <= 0 or target_aspect <= 0:
        return rect
    source_aspect = source_width / source_height
    norm_aspect = target_aspect / source_aspect
    width = max(0.02, min(1.0, rect.width_pct))
    height = max(0.02, min(1.0, rect.height_pct))
    if width / height < norm_aspect:
        width = height * norm_aspect
    else:
        height = width / norm_aspect
    if width >= 1.0:
        width = 1.0
        height = width / norm_aspect
    if height >= 1.0:
        height = 1.0
        width = height * norm_aspect
    if width > 1.0 or height > 1.0:
        return Rect(x_pct=0.0, y_pct=0.0, width_pct=1.0, height_pct=1.0)
    center_x = rect.x_pct + rect.width_pct / 2.0
    center_y = rect.y_pct + rect.height_pct / 2.0
    x = max(0.0, min(1.0 - width, center_x - width / 2.0))
    y = max(0.0, min(1.0 - height, center_y - height / 2.0))
    return Rect(x_pct=x, y_pct=y, width_pct=width, height_pct=height)


def _facecam_panel_rect(
    facecam: Rect,
    face_rect: Rect | None,
    target_aspect: float,
    source_width: int,
    source_height: int,
    output_width: int,
) -> Rect:
    """Return the crop window used for the webcam panel.

    The window never leaves the accepted webcam rectangle. When the output
    panel has a different aspect it crops inside the cam, centred on the face,
    rather than growing outside it and showing gameplay. A simple upscale guard
    widens the window when the source cam is too small to reach the panel
    without soft scaling.
    """

    source_aspect = (source_width or 16) / max(1, source_height or 9)
    norm_aspect = target_aspect / source_aspect if source_aspect > 0 else 1.0
    if norm_aspect <= 0:
        return facecam
    margin = 0.012
    # Chat and alert overlays often sit immediately below the webcam. A slightly
    # larger bottom inset keeps that strip out of the cam panel without cutting
    # the streamer's face.
    bottom_margin = min(0.030, facecam.height_pct * 0.20)
    max_width = max(0.02, facecam.width_pct - margin * 2)
    max_height = max(0.02, facecam.height_pct - margin - bottom_margin)
    if max_width / max_height > norm_aspect:
        height = max_height
        width = height * norm_aspect
    else:
        width = max_width
        height = width / norm_aspect
    if width > max_width:
        width = max_width
        height = width / norm_aspect
    if height > max_height:
        height = max_height
        width = height * norm_aspect

    # Keep the face at roughly 28% of the panel width when the source has
    # enough pixels. Below that, the upscale guard wins: a sharp face beats a
    # slightly larger soft one.
    if face_rect is not None and face_rect.width_pct > 0:
        desired_width = face_rect.width_pct / 0.28
        desired_height = desired_width / norm_aspect
        if desired_width <= max_width and desired_height <= max_height:
            width = max(width, desired_width)
            height = width / norm_aspect
    if output_width > 0 and width > 0:
        min_width_for_upscale = output_width / (3.0 * max(1, source_width or output_width))
        width = max(width, min(max_width, min_width_for_upscale))
        height = min(max_height, width / norm_aspect)
        width = height * norm_aspect

    center_x = face_rect.x_pct + face_rect.width_pct / 2.0 if face_rect else facecam.x_pct + facecam.width_pct / 2.0
    center_y = face_rect.y_pct + face_rect.height_pct * 0.45 if face_rect else facecam.y_pct + facecam.height_pct / 2.0
    x = max(facecam.x_pct + margin, min(facecam.x_pct + facecam.width_pct - margin - width, center_x - width / 2.0))
    y = max(
        facecam.y_pct + margin,
        min(facecam.y_pct + facecam.height_pct - bottom_margin - height, center_y - height * 0.42),
    )
    return Rect(x_pct=x, y_pct=y, width_pct=width, height_pct=height)


def _protected_regions(scene: Scene, source_width: int, source_height: int) -> list[Rect]:
    """Best-effort regions a gameplay crop should try not to lose.

    These are cheap signals, not object detection: the content rectangle from
    edge density, HUD-like detail cells near the frame edges, and a subject box
    when skin evidence says a person is in the shot. They are penalties, never
    guarantees; the decision records them so the reviewer can see what was
    protected.
    """

    features = scene.features
    regions: list[Rect] = []
    content_width = float(features.get("content_width", 1.0))
    content_height = float(features.get("content_height", 1.0))
    if (
        content_width >= 0.50
        and content_height >= 0.40
        and content_width * content_height < 0.92
    ):
        regions.append(Rect(
            x_pct=float(features.get("content_x", 0.0)),
            y_pct=float(features.get("content_y", 0.0)),
            width_pct=content_width,
            height_pct=content_height,
        ))

    grid = [float(features.get(f"grid_edge_{index}", 0.0)) for index in range(9)]
    if grid and any(grid):
        average = sum(grid) / len(grid)
        threshold = max(0.030, average * 1.35)
        for index, density in enumerate(grid):
            if density < threshold:
                continue
            row, column = divmod(index, 3)
            # HUD, score, minimap and objective text live at the frame edges.
            if row not in (0, 2) and column not in (0, 2):
                continue
            regions.append(Rect(
                x_pct=column / 3.0,
                y_pct=row / 3.0,
                width_pct=1 / 3.0,
                height_pct=1 / 3.0,
            ))

    subject_x = _clamp01(scene.subject_x)
    if float(features.get("skin_fraction", 0.0)) > 0.012 and scene.content_type in {"talking_head", "mixed"}:
        regions.append(Rect(
            x_pct=max(0.0, min(1.0 - 0.36, subject_x - 0.18)),
            y_pct=0.20,
            width_pct=0.36,
            height_pct=0.70,
        ))
    return regions


def _gameplay_rect(
    scene: Scene,
    spec: RenderSpec,
    source_width: int,
    source_height: int,
    panel_aspect: float,
    facecam: Rect | None,
) -> tuple[Rect, list[Rect], list[str]]:
    """Choose the source window that best fills a gameplay panel.

    The panel aspect is derived from the actual layout, so a short gameplay
    panel gets a squarer crop than the 16:9 source. Candidates are scored on how
    much protected detail they keep, how much face-cam they avoid duplicating
    inside the gameplay panel, and how close they stay to the requested focus.
    """

    warnings: list[str] = []
    protected = _protected_regions(scene, source_width, source_height)
    if source_width <= 0 or source_height <= 0 or panel_aspect <= 0:
        return Rect(), protected, warnings
    source_aspect = source_width / source_height
    norm_aspect = panel_aspect / source_aspect
    if abs(norm_aspect - 1.0) <= 0.015:
        return Rect(), protected, warnings

    if norm_aspect > 1.0:
        width = 1.0
        height = min(1.0, 1.0 / norm_aspect)
    else:
        width = max(0.02, norm_aspect)
        height = 1.0

    focus_x = (
        float(spec.video.crop_x_pct) / 100.0
        if spec.video.crop_x_pct is not None
        else _clamp01(scene.subject_x)
    )
    focus_y = (
        float(spec.video.crop_y_pct) / 100.0
        if spec.video.crop_y_pct is not None
        else 0.5
    )
    x_positions = [index / 20.0 * (1.0 - width) for index in range(21)]
    y_positions = [index / 20.0 * (1.0 - height) for index in range(21)]
    if width >= 1.0:
        x_positions = [0.0]
    if height >= 1.0:
        y_positions = [0.0]

    protected_area = sum(_rect_area(item) for item in protected) or 1.0
    best: tuple[float, Rect, float] | None = None
    for x in x_positions:
        for y in y_positions:
            candidate = Rect(x_pct=x, y_pct=y, width_pct=width, height_pct=height)
            covered = sum(
                _intersection_area(candidate, item) for item in protected
            ) / protected_area
            avoid = 0.0
            if facecam is not None and _rect_area(facecam) > 0:
                avoid = _intersection_area(candidate, facecam) / _rect_area(facecam)
            center_x = candidate.x_pct + candidate.width_pct / 2.0
            center_y = candidate.y_pct + candidate.height_pct / 2.0
            focus_penalty = abs(center_x - focus_x) / 0.5 + abs(center_y - focus_y) / 0.5
            score = covered * 1.8 - avoid * 1.4 - focus_penalty * 0.55
            if best is None or score > best[0]:
                best = (score, candidate, covered)

    if best is None:
        return Rect(), protected, warnings
    _, chosen, covered = best
    if protected and covered < 0.65:
        warnings.append(
            f"The gameplay panel keeps about {covered:.0%} of the detected detail; "
            "use Whole gameplay frame if HUD or objectives are being cut off."
        )
    return chosen, protected, warnings


def _stack_split_for_range(spec: RenderSpec, facecam: Rect | None, split_pct: float) -> float:
    """Stable split by default; an explicit adaptive option may shift it a little."""
    if not spec.video.adaptive_stack or facecam is None:
        return split_pct
    # A larger source face-cam gets a modestly larger panel. The bound is small
    # and the result is a per-range decision, so it cannot jitter frame to frame.
    target = split_pct + (facecam.height_pct - 0.22) * 35.0
    low = max(35.0, split_pct - 8.0)
    high = min(80.0, split_pct + 8.0)
    return round(max(low, min(high, target)), 1)


def decide_composition(
    scene: Scene,
    spec: RenderSpec,
    source_width: int,
    source_height: int,
    *,
    forced: str | None = None,
    facecam_override: Rect | tuple[float, float, float, float] | None = None,
    facecam_face_rect: Rect | None = None,
    facecam_state: str = STATE_VISIBLE,
    stack_split_pct: float | None = None,
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
    gameplay_rect: Rect | None = None
    facecam_panel_rect: Rect | None = None
    protected: list[Rect] = []
    split_pct = float(stack_split_pct if stack_split_pct is not None else spec.video.stack_split_pct)
    if facecam_override is not None and not isinstance(facecam_override, Rect):
        facecam_override = Rect(
            x_pct=facecam_override[0],
            y_pct=facecam_override[1],
            width_pct=facecam_override[2],
            height_pct=facecam_override[3],
        )

    def resolve_facecam() -> Rect | None:
        if facecam_state == STATE_MISSING:
            return None
        if facecam_override is not None:
            return facecam_override
        return _facecam_rect(scene, spec.video.face_cam_rect)

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
        facecam = resolve_facecam()
        if facecam is None:
            mode = "crop"
            reason = (
                "The stacked layout needs a face-cam; none was detected for this range, "
                "so the frame is filled instead."
            )
            warnings.append(
                "No face-cam was detected for this range, so the gameplay + face-cam stack "
                "fell back to a full-screen crop. Set a manual face-cam region or use a "
                "different layout for this part of the clip."
            )
        else:
            mode = "stack"
            split_pct = _stack_split_for_range(spec, facecam, split_pct)
            game_height = max(0.05, min(0.95, split_pct / 100.0))
            game_aspect = width / max(1.0, height * game_height)
            cam_aspect = width / max(1.0, height * (1.0 - game_height))
            gameplay_rect, protected, crop_warnings = _gameplay_rect(
                scene, spec, source_width, source_height, game_aspect, facecam
            )
            facecam_panel_rect = _facecam_panel_rect(
                facecam, facecam_face_rect, cam_aspect, source_width, source_height, width
            )
            if facecam_face_rect is not None and facecam_panel_rect.width_pct > 0:
                face_share = facecam_face_rect.width_pct / facecam_panel_rect.width_pct
                if face_share < 0.22:
                    warnings.append(
                        f"The webcam face fills about {face_share:.0%} of its panel. "
                        "The source panel is too small for a tighter 25-40% face crop at this output size; "
                        "raising the source resolution is the only way to enlarge it without heavy upscaling."
                    )
            warnings.extend(crop_warnings)
            where = "below" if spec.video.face_cam_position == "bottom" else "above"
            tracked = "manual" if facecam_state == STATE_MANUAL else "tracked"
            reason = (
                f"Gameplay + face-cam stack: the {tracked} face-cam sits {where} a "
                f"{split_pct:.0f}% gameplay panel cropped to {game_aspect:.2f}:1."
            )
            if facecam_state == STATE_UNCERTAIN:
                warnings.append(
                    "The face-cam detection is uncertain for this range; the last confident "
                    "plate is reused. Review the region before publishing."
                )
        auto = False
    elif forced == "facecam_focus":
        facecam = resolve_facecam()
        mode = "crop"
        auto = False
        if facecam is None:
            reason = (
                "Face-cam focus was requested, but no face-cam was detected for this range; "
                "the frame is filled."
            )
            warnings.append(
                "No face-cam was detected for this range, so face-cam focus fell back to a "
                "full-screen crop. Use a different layout or set a manual face-cam region."
            )
        else:
            source_rect = _expand_rect_to_aspect(
                facecam, target_aspect, source_width, source_height
            )
            reason = "Face-cam focus crops to the tracked face-cam region."
            if _rect_area(source_rect) > _rect_area(facecam) * 1.6:
                warnings.append(
                    "Face-cam focus adds surrounding source context to fill the output aspect."
                )
            if facecam_state == STATE_UNCERTAIN:
                warnings.append(
                    "The face-cam detection is uncertain for this range; the last confident "
                    "plate is reused."
                )
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
        # Captions sit inside the gameplay panel, never over the face-cam.
        band = _stack_caption_band(spec.video.face_cam_position, split_pct)
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
        facecam_state=facecam_state,
        source_rect=source_rect,
        gameplay_rect=gameplay_rect,
        facecam_panel_rect=facecam_panel_rect,
        protected_regions=protected,
        face_cam_position=spec.video.face_cam_position,
        stack_split_pct=split_pct,
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
                and _compatible(left.composition, right.composition)
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


def _scene_for_range(
    analysis: SceneAnalysis,
    start_ms: int,
    end_ms: int,
    tracked: TrackRange,
) -> Scene:
    """Build a scene view for one tracked face-cam range.

    Scene classification still comes from the visual analysis; the face-cam
    fields are replaced with the tracked plate for this exact range. That keeps
    content-aware framing and face-cam tracking independent but composable.
    """

    overlapping = [
        scene for scene in analysis.scenes
        if min(end_ms, scene.end_ms) > max(start_ms, scene.start_ms)
    ]
    if not overlapping:
        base = Scene(
            start_ms=start_ms,
            end_ms=end_ms,
            content_type="unknown",
            confidence=0.3,
            subject_x=0.5,
        )
    else:
        base = max(
            overlapping,
            key=lambda scene: min(end_ms, scene.end_ms) - max(start_ms, scene.start_ms),
        )
    features = dict(base.features)
    if tracked.rect is not None:
        features.update({
            "facecam_present": 1.0,
            "facecam_x": tracked.rect[0],
            "facecam_y": tracked.rect[1],
            "facecam_width": tracked.rect[2],
            "facecam_height": tracked.rect[3],
            "facecam_confidence": 1.0 if tracked.state == STATE_MANUAL else tracked.confidence,
        })
    warnings = list(base.warnings)
    if tracked.state == STATE_UNCERTAIN:
        warnings.append("Face-cam tracking is uncertain for this range; the last confident plate is reused.")
    elif tracked.state == STATE_MISSING:
        warnings.append("Face-cam is missing for this range; the layout falls back safely.")
    return Scene(
        start_ms=start_ms,
        end_ms=end_ms,
        content_type=base.content_type,
        confidence=base.confidence,
        subject_x=base.subject_x,
        features=features,
        warnings=warnings,
    )


def _to_spec_track(track) -> SpecFaceCamTrack | None:
    if track is None:
        return None
    return SpecFaceCamTrack(
        observations=[
            SpecFaceCamObservation(
                time_ms=item.time_ms,
                state=item.state,
                rect=Rect(
                    x_pct=item.rect[0],
                    y_pct=item.rect[1],
                    width_pct=item.rect[2],
                    height_pct=item.rect[3],
                ) if item.rect else None,
                face_rect=Rect(
                    x_pct=item.face_rect[0],
                    y_pct=item.face_rect[1],
                    width_pct=item.face_rect[2],
                    height_pct=item.face_rect[3],
                ) if item.face_rect else None,
                confidence=item.confidence,
                reason=item.reason,
            )
            for item in track.observations
        ],
        ranges=[
            SpecFaceCamRange(
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                state=item.state,
                rect=Rect(
                    x_pct=item.rect[0],
                    y_pct=item.rect[1],
                    width_pct=item.rect[2],
                    height_pct=item.rect[3],
                ) if item.rect else None,
                face_rect=Rect(
                    x_pct=item.face_rect[0],
                    y_pct=item.face_rect[1],
                    width_pct=item.face_rect[2],
                    height_pct=item.face_rect[3],
                ) if item.face_rect else None,
                confidence=item.confidence,
                reason=item.reason,
            )
            for item in track.ranges
        ],
        sample_fps=track.sample_fps,
        warnings=list(track.warnings),
    )


def _same_tracked_plate(left: Rect | None, right: Rect | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    lx = left.x_pct + left.width_pct / 2
    ly = left.y_pct + left.height_pct / 2
    rx = right.x_pct + right.width_pct / 2
    ry = right.y_pct + right.height_pct / 2
    return abs(lx - rx) <= 0.04 and abs(ly - ry) <= 0.04


def _compatible(left: CompositionDecision, right: CompositionDecision) -> bool:
    if left.mode != right.mode or left.content_type != right.content_type:
        return False
    if left.mode == "stack":
        return (
            left.facecam_state == right.facecam_state
            and abs(left.stack_split_pct - right.stack_split_pct) <= 0.5
            and _same_tracked_plate(left.facecam, right.facecam)
            and _same_tracked_plate(left.gameplay_rect, right.gameplay_rect)
        )
    if left.source_rect is not None or right.source_rect is not None:
        return _same_tracked_plate(left.source_rect, right.source_rect)
    return True

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
        # The face-cam is a property of the source, not of a scene boundary.
        # Analyse the edit window once, track the plate over time, then attach a
        # decision per tracked range. The panel dimensions stay stable by
        # default; the source crops follow the tracked plate.
        window_start = max(0, min(segment.start_ms for segment in segments) - 250)
        window_end = max(segment.end_ms for segment in segments) + 250
        overrides = [item.model_dump() for item in spec.video.face_cam_overrides]
        if spec.video.face_cam_rect is not None:
            overrides.append({
                "start_ms": window_start,
                "end_ms": window_end,
                "rect": spec.video.face_cam_rect.model_dump(),
                "mode": "lock",
                "reason": "Manual face-cam region for the whole clip.",
            })
        analysis: SceneAnalysis | None = None
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
                track_facecam=True,
                face_cam_overrides=overrides,
            )
            analyses.append(analysis)
            warnings.extend(analysis.warnings)
        except Exception as exc:  # visual analysis must never block an export
            logger.warning("Face-cam analysis failed for %s-%s: %s", window_start, window_end, exc)
            warnings.append("Face-cam analysis failed; the requested layout falls back to a full crop.")

        track = analysis.facecam_track if analysis is not None else None
        if track is None or not track.ranges:
            # No usable track: keep the requested layout honest by making one
            # decision per scene. If no face-cam is present the decision itself
            # falls back with an explicit warning.
            fallback_analysis = analysis or SceneAnalysis(
                scenes=[Scene(window_start, window_end, "unknown", 0.3, 0.5)],
                samples=[],
                cuts=[],
                warnings=["Scene analysis unavailable."],
            )
            for segment in segments:
                if segment.composition is not None:
                    continue
                scene = _scene_for_range(
                    fallback_analysis,
                    segment.start_ms,
                    segment.end_ms,
                    TrackRange(
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        state=STATE_MISSING,
                        rect=None,
                        confidence=0.0,
                        reason="No tracked face-cam range was available.",
                    ),
                )
                decision = decide_composition(scene, spec, width, height, forced=forced)
                segment.composition = decision
                warnings.extend(decision.warnings)
            return CompositionPlan(
                segments=segments,
                warnings=list(dict.fromkeys(warnings)),
                analyses=analyses,
                facecam_track=_to_spec_track(track),
            )

        expanded: list[SegmentSpan] = []
        for segment in segments:
            if segment.composition is not None:
                # A user override is authoritative; do not split or replace it.
                expanded.append(segment)
                continue
            for tracked in ranges_for_span(track, segment.start_ms, segment.end_ms):
                scene = _scene_for_range(analysis, tracked.start_ms, tracked.end_ms, tracked)
                decision = decide_composition(
                    scene,
                    spec,
                    width,
                    height,
                    forced=forced,
                    facecam_override=tracked.rect,
                    facecam_face_rect=(
                        Rect(
                            x_pct=tracked.face_rect[0],
                            y_pct=tracked.face_rect[1],
                            width_pct=tracked.face_rect[2],
                            height_pct=tracked.face_rect[3],
                        ) if tracked.face_rect else None
                    ),
                    facecam_state=tracked.state,
                    stack_split_pct=spec.video.stack_split_pct,
                )
                warnings.extend(decision.warnings)
                candidate = SegmentSpan(
                    start_ms=tracked.start_ms,
                    end_ms=tracked.end_ms,
                    role=segment.role,
                    composition=decision,
                )
                if (
                    expanded
                    and expanded[-1].role == candidate.role
                    and expanded[-1].composition is not None
                    and _compatible(expanded[-1].composition, decision)
                ):
                    expanded[-1].end_ms = candidate.end_ms
                    expanded[-1].composition.warnings = list(dict.fromkeys(
                        expanded[-1].composition.warnings + decision.warnings
                    ))
                else:
                    expanded.append(candidate)
        expanded = _cap_segments(expanded, warnings)
        return CompositionPlan(
            segments=expanded,
            warnings=list(dict.fromkeys(warnings)),
            analyses=analyses,
            facecam_track=_to_spec_track(track),
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
