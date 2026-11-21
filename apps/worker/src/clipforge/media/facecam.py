"""Time-aware webcam-panel tracking built on the existing frame sampler.

Detection produces scored rectangular candidates. This module turns those
candidates into one accepted webcam track:

* an initial panel needs a face inside a compact rectangle, not merely the
  largest face in the frame;
* once accepted, the panel is the authority. A new face elsewhere is ignored;
* a brief face loss keeps the accepted rectangle as "uncertain" rather than
  jumping to gameplay;
* a genuine move or resize needs the old panel to disappear and the new one to
  persist across several samples;
* a face growing inside a fixed panel does not resize the panel;
* user overrides are time-ranged and either lock the region or seed tracking.

The output is time-indexed evidence. The composition planner decides what each
range means for the output layout.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

RectTuple = tuple[float, float, float, float]
STATE_VISIBLE = "visible"
STATE_MISSING = "missing"
STATE_UNCERTAIN = "uncertain"
STATE_MANUAL = "manual"

# A plate must be seen for at least this long before a change is accepted.
MIN_CHANGE_OBSERVATIONS = 2
# Movement smaller than this is the person, not the camera.
STABLE_CENTER_TOLERANCE = 0.045
STABLE_SIZE_LOW = 0.85
STABLE_SIZE_HIGH = 1.18
# A new plate must be at least this different before it is even a candidate.
CHANGE_CENTER_DISTANCE = 0.06
CHANGE_SIZE_LOW = 0.75
CHANGE_SIZE_HIGH = 1.33
# Ignore plates that occupy an implausible amount of the source.
MIN_PLATE_AREA = 0.004
MAX_PLATE_AREA = 0.45
# Candidate-based tracking is stricter than the legacy heuristic. A new panel
# is only accepted after the accepted one has been missing and the replacement
# has persisted for this many samples.
MIN_SWITCH_MISSING = 2
SWITCH_PERSISTENCE = 3
MIN_INITIAL_PANEL_SCORE = 0.38
MIN_INITIAL_FACE_SCORE = 0.55
MIN_SWITCH_PANEL_SCORE = 0.45
MIN_SWITCH_FACE_SCORE = 0.50
# A face that fills a large share of the source is a full-screen camera, not
# an embedded webcam. A distinct panel also needs either an interior/exterior
# difference or a visible border; otherwise the rectangle is just a guess.
MAX_INITIAL_FACE_AREA = 0.007
MAX_INITIAL_PANEL_AREA = 0.10
MIN_PANEL_INTERIOR = 0.20
MIN_PANEL_BORDER = 0.055
# A resize needs stronger boundary evidence than the accepted panel, otherwise
# a face leaning towards the lens would resize the whole webcam panel.
RESIZE_SCORE_MARGIN = 0.08


@dataclass
class Observation:
    time_ms: int
    state: str
    rect: RectTuple | None
    confidence: float
    reason: str = ""
    face_rect: RectTuple | None = None

    def to_dict(self) -> dict:
        return {
            "time_ms": self.time_ms,
            "state": self.state,
            "rect": None if self.rect is None else {
                "x_pct": round(self.rect[0], 4),
                "y_pct": round(self.rect[1], 4),
                "width_pct": round(self.rect[2], 4),
                "height_pct": round(self.rect[3], 4),
            },
            "face_rect": None if self.face_rect is None else {
                "x_pct": round(self.face_rect[0], 4),
                "y_pct": round(self.face_rect[1], 4),
                "width_pct": round(self.face_rect[2], 4),
                "height_pct": round(self.face_rect[3], 4),
            },
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }


@dataclass
class TrackRange:
    start_ms: int
    end_ms: int
    state: str
    rect: RectTuple | None
    confidence: float
    reason: str = ""
    face_rect: RectTuple | None = None

    def to_dict(self) -> dict:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "state": self.state,
            "rect": None if self.rect is None else {
                "x_pct": round(self.rect[0], 4),
                "y_pct": round(self.rect[1], 4),
                "width_pct": round(self.rect[2], 4),
                "height_pct": round(self.rect[3], 4),
            },
            "face_rect": None if self.face_rect is None else {
                "x_pct": round(self.face_rect[0], 4),
                "y_pct": round(self.face_rect[1], 4),
                "width_pct": round(self.face_rect[2], 4),
                "height_pct": round(self.face_rect[3], 4),
            },
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }


@dataclass
class FaceCamTrack:
    observations: list[Observation] = field(default_factory=list)
    ranges: list[TrackRange] = field(default_factory=list)
    sample_fps: float | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "observations": [item.to_dict() for item in self.observations],
            "ranges": [item.to_dict() for item in self.ranges],
            "sample_fps": self.sample_fps,
            "warnings": list(dict.fromkeys(self.warnings)),
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> "FaceCamTrack | None":
        if not payload:
            return None
        observations = [
            Observation(
                time_ms=int(item["time_ms"]),
                state=str(item.get("state") or STATE_MISSING),
                rect=_rect_from_dict(item.get("rect")),
                face_rect=_rect_from_dict(item.get("face_rect")),
                confidence=float(item.get("confidence") or 0.0),
                reason=str(item.get("reason") or ""),
            )
            for item in payload.get("observations") or []
        ]
        ranges = [
            TrackRange(
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
                state=str(item.get("state") or STATE_MISSING),
                rect=_rect_from_dict(item.get("rect")),
                face_rect=_rect_from_dict(item.get("face_rect")),
                confidence=float(item.get("confidence") or 0.0),
                reason=str(item.get("reason") or ""),
            )
            for item in payload.get("ranges") or []
        ]
        return cls(
            observations=observations,
            ranges=ranges,
            sample_fps=payload.get("sample_fps"),
            warnings=list(payload.get("warnings") or []),
        )


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _rect_from_features(features: dict) -> RectTuple | None:
    if float(features.get("facecam_present", 0.0)) < 0.5:
        return None
    if float(features.get("facecam_confidence", 0.0)) < 0.35:
        return None
    x = _clamp(float(features.get("facecam_x", 0.0)))
    y = _clamp(float(features.get("facecam_y", 0.0)))
    width = float(features.get("facecam_width", 0.0))
    height = float(features.get("facecam_height", 0.0))
    if width <= 0.0 or height <= 0.0:
        return None
    width = max(0.02, min(1.0 - x, width))
    height = max(0.02, min(1.0 - y, height))
    if width * height < MIN_PLATE_AREA or width * height > MAX_PLATE_AREA:
        return None
    return (x, y, width, height)


def _center(rect: RectTuple) -> tuple[float, float]:
    return rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0


def _center_distance(left: RectTuple, right: RectTuple) -> float:
    lx, ly = _center(left)
    rx, ry = _center(right)
    return ((lx - rx) ** 2 + (ly - ry) ** 2) ** 0.5


def _size_ratio(left: RectTuple, right: RectTuple) -> float:
    left_area = max(1e-6, left[2] * left[3])
    right_area = max(1e-6, right[2] * right[3])
    return (right_area / left_area) ** 0.5


def _iou(left: RectTuple, right: RectTuple) -> float:
    lx0, ly0, lw, lh = left
    rx0, ry0, rw, rh = right
    lx1, ly1 = lx0 + lw, ly0 + lh
    rx1, ry1 = rx0 + rw, ry0 + rh
    ix0, iy0 = max(lx0, rx0), max(ly0, ry0)
    ix1, iy1 = min(lx1, rx1), min(ly1, ry1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    intersection = (ix1 - ix0) * (iy1 - iy0)
    union = lw * lh + rw * rh - intersection
    return intersection / max(1e-6, union)


def _same_plate(left: RectTuple, right: RectTuple) -> bool:
    return (
        _center_distance(left, right) <= STABLE_CENTER_TOLERANCE
        and STABLE_SIZE_LOW <= _size_ratio(left, right) <= STABLE_SIZE_HIGH
    ) or _iou(left, right) >= 0.55


def _candidate_plate(left: RectTuple, right: RectTuple) -> bool:
    return (
        _center_distance(left, right) <= CHANGE_CENTER_DISTANCE
        and CHANGE_SIZE_LOW <= _size_ratio(left, right) <= CHANGE_SIZE_HIGH
    ) or _iou(left, right) >= 0.35


def _median_rect(rects: Sequence[RectTuple]) -> RectTuple:
    xs = statistics.median(rect[0] for rect in rects)
    ys = statistics.median(rect[1] for rect in rects)
    ws = statistics.median(rect[2] for rect in rects)
    hs = statistics.median(rect[3] for rect in rects)
    x = _clamp(xs, 0.0, 1.0 - ws)
    y = _clamp(ys, 0.0, 1.0 - hs)
    return (x, y, ws, hs)


def _candidate_rect(candidate: dict) -> RectTuple | None:
    try:
        x = _clamp(float(candidate["x"]))
        y = _clamp(float(candidate["y"]))
        width = float(candidate["width"])
        height = float(candidate["height"])
    except (KeyError, TypeError, ValueError):
        return None
    width = max(0.02, min(1.0 - x, width))
    height = max(0.02, min(1.0 - y, height))
    if width * height < MIN_PLATE_AREA or width * height > MAX_PLATE_AREA:
        return None
    return (x, y, width, height)


def _candidate_score(candidate: dict) -> float:
    try:
        return float(candidate.get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _candidate_face_score(candidate: dict) -> float:
    try:
        return float(candidate.get("face_score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _candidate_face_inside(candidate: dict) -> float:
    try:
        return float(candidate.get("face_inside", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _candidate_face_rect(candidate: dict) -> RectTuple | None:
    try:
        width = float(candidate.get("face_w", 0.0))
        height = float(candidate.get("face_h", 0.0))
        center_x = float(candidate.get("face_cx", 0.0))
        center_y = float(candidate.get("face_cy", 0.0))
    except (TypeError, ValueError):
        return None
    if width <= 0.0 or height <= 0.0 or (center_x <= 0.0 and center_y <= 0.0):
        return None
    x = _clamp(center_x - width / 2.0, 0.0, 1.0 - width)
    y = _clamp(center_y - height / 2.0, 0.0, 1.0 - height)
    return (x, y, width, height)


def _candidate_webcam_like(candidate: dict, *, switching: bool = False) -> bool:
    face_rect = _candidate_face_rect(candidate)
    panel_rect = _candidate_rect(candidate)
    max_face_area = 0.015 if switching else MAX_INITIAL_FACE_AREA
    max_panel_area = 0.20 if switching else MAX_INITIAL_PANEL_AREA
    if (
        face_rect is None
        or panel_rect is None
        or face_rect[2] * face_rect[3] > max_face_area
        or panel_rect[2] * panel_rect[3] > max_panel_area
    ):
        return False
    try:
        interior = float(candidate.get("interior", 0.0))
        border = float(candidate.get("border", 0.0))
    except (TypeError, ValueError):
        return False
    if switching:
        # A replacement panel may be larger than the initial small plate, but
        # it still needs a real border. Interior difference alone is not enough:
        # a full-frame camera also differs from the room around it.
        return border >= 0.05 and (interior >= MIN_PANEL_INTERIOR or border >= MIN_PANEL_BORDER)
    return interior >= MIN_PANEL_INTERIOR or border >= MIN_PANEL_BORDER


def _point_in_rect(x: float, y: float, rect: RectTuple, margin: float = 0.012) -> bool:
    return rect[0] + margin <= x <= rect[0] + rect[2] - margin and rect[1] + margin <= y <= rect[1] + rect[3] - margin


def _accepted_face_rect(accepted: RectTuple, candidates: list[dict]) -> RectTuple | None:
    for candidate in candidates:
        panel = _candidate_rect(candidate)
        if panel is None or not _candidate_plate(accepted, panel):
            continue
        if _candidate_face_score(candidate) <= 0.0:
            continue
        try:
            face_x = float(candidate.get("face_cx", 0.0))
            face_y = float(candidate.get("face_cy", 0.0))
        except (TypeError, ValueError):
            continue
        if face_x <= 0.0 and face_y <= 0.0:
            continue
        if _point_in_rect(face_x, face_y, accepted):
            return _candidate_face_rect(candidate)
    return None


def _accepted_has_face(accepted: RectTuple, candidates: list[dict]) -> bool:
    return _accepted_face_rect(accepted, candidates) is not None


def _accepted_boundary_supported(accepted: RectTuple, candidates: list[dict]) -> bool:
    for candidate in candidates:
        rect = _candidate_rect(candidate)
        if rect is None:
            continue
        if _iou(accepted, rect) < 0.55:
            continue
        if _candidate_score(candidate) >= 0.40 and float(candidate.get("border", 0.0)) >= 0.12:
            return True
    return False


def _best_initial_candidate(candidates: list[dict]) -> dict | None:
    plausible = [
        candidate
        for candidate in candidates
        if _candidate_face_inside(candidate) >= 0.50
        and _candidate_face_score(candidate) >= MIN_INITIAL_FACE_SCORE
        and _candidate_score(candidate) >= MIN_INITIAL_PANEL_SCORE
        and _candidate_rect(candidate) is not None
        and _candidate_webcam_like(candidate)
    ]
    if not plausible:
        return None
    return max(plausible, key=_candidate_score)


def _best_switch_candidate(candidates: list[dict]) -> dict | None:
    plausible = [
        candidate
        for candidate in candidates
        if _candidate_face_inside(candidate) >= 0.50
        and _candidate_face_score(candidate) >= MIN_SWITCH_FACE_SCORE
        and _candidate_score(candidate) >= MIN_SWITCH_PANEL_SCORE
        and _candidate_rect(candidate) is not None
        and _candidate_webcam_like(candidate, switching=True)
    ]
    if not plausible:
        return None
    return max(plausible, key=_candidate_score)


def _smooth_rect(accepted: RectTuple, candidate: RectTuple, alpha: float = 0.22) -> RectTuple:
    x = accepted[0] * (1.0 - alpha) + candidate[0] * alpha
    y = accepted[1] * (1.0 - alpha) + candidate[1] * alpha
    width = accepted[2] * (1.0 - alpha) + candidate[2] * alpha
    height = accepted[3] * (1.0 - alpha) + candidate[3] * alpha
    width = max(0.02, min(1.0, width))
    height = max(0.02, min(1.0, height))
    return (_clamp(x, 0.0, 1.0 - width), _clamp(y, 0.0, 1.0 - height), width, height)


def candidate_observations(
    samples: Iterable[Any],
    *,
    overrides: Sequence[dict] | None = None,
) -> list[Observation]:
    """Track one accepted webcam panel across candidate-bearing samples.

    The accepted rectangle is deliberately sticky. A globally stronger face
    elsewhere cannot replace it while the streamer's face or the panel boundary
    still supports the accepted region.
    """

    ordered = sorted(samples, key=lambda item: int(getattr(item, "time_ms", 0)))
    track_overrides = []
    for override in overrides or []:
        if str(override.get("mode") or "lock") != "track":
            continue
        rect = _rect_from_dict(override.get("rect"))
        if rect is None:
            continue
        track_overrides.append((int(override.get("start_ms", 0)), rect, str(override.get("reason") or "Tracked from a manual region.")))
    track_overrides.sort(key=lambda item: item[0])

    accepted: RectTuple | None = None
    accepted_score = 0.0
    accepted_border = 0.0
    accepted_face: RectTuple | None = None
    missing_count = 0
    pending: RectTuple | None = None
    pending_count = 0
    next_seed = 0
    observations: list[Observation] = []

    def emit(observation: Observation) -> None:
        observation.face_rect = accepted_face if observation.rect is not None else None
        observations.append(observation)

    for sample in ordered:
        time_ms = int(getattr(sample, "time_ms", 0))
        while next_seed < len(track_overrides) and track_overrides[next_seed][0] <= time_ms:
            _, seed_rect, seed_reason = track_overrides[next_seed]
            accepted = seed_rect
            accepted_score = 1.0
            accepted_border = 0.0
            missing_count = 0
            pending = None
            pending_count = 0
            next_seed += 1
            accepted_face = None
            emit(
                Observation(
                    time_ms=time_ms,
                    state=STATE_VISIBLE,
                    rect=accepted,
                    confidence=1.0,
                    reason=seed_reason,
                )
            )
            break
        else:
            candidates = [
                candidate
                for candidate in (getattr(sample, "facecam_candidates", None) or [])
                if _candidate_rect(candidate) is not None
            ]

            if accepted is None:
                best = _best_initial_candidate(candidates)
                if best is None:
                    emit(
                        Observation(
                            time_ms=time_ms,
                            state=STATE_MISSING,
                            rect=None,
                            confidence=0.0,
                            reason="No face inside a compact webcam-panel candidate.",
                        )
                    )
                    continue
                accepted = _candidate_rect(best)
                accepted_score = _candidate_score(best)
                accepted_border = float(best.get("border", 0.0))
                accepted_face = _candidate_face_rect(best)
                missing_count = 0
                pending = None
                pending_count = 0
                emit(
                    Observation(
                        time_ms=time_ms,
                        state=STATE_VISIBLE,
                        rect=accepted,
                        confidence=min(0.96, 0.35 + accepted_score),
                        reason="Webcam panel accepted: compact rectangle with the streamer's face inside.",
                    )
                )
                continue

            # A face inside the accepted panel is the strongest continuity
            # evidence. It keeps the panel through a globally stronger in-game
            # face, and it stops a face leaning into the lens from resizing it.
            face_inside_accepted = _accepted_has_face(accepted, candidates)
            compatible = [
                candidate
                for candidate in candidates
                if (rect := _candidate_rect(candidate)) is not None
                and _candidate_face_inside(candidate) >= 0.50
                and _candidate_plate(accepted, rect)
                and _candidate_score(candidate) >= 0.30
            ]
            if compatible:
                best = max(compatible, key=_candidate_score)
                rect = _candidate_rect(best)
                assert rect is not None
                if _same_plate(accepted, rect):
                    accepted = _smooth_rect(accepted, rect)
                    accepted_score = max(accepted_score, _candidate_score(best))
                    accepted_border = max(accepted_border, float(best.get("border", 0.0)))
                    accepted_face = _candidate_face_rect(best) or accepted_face
                    missing_count = 0
                    pending = None
                    pending_count = 0
                    emit(
                        Observation(
                            time_ms=time_ms,
                            state=STATE_VISIBLE,
                            rect=accepted,
                            confidence=min(0.96, 0.35 + _candidate_score(best)),
                            reason="Stable webcam panel; movement stayed inside the accepted region.",
                        )
                    )
                    continue
                # The centre is stable but the size changed. That is either a
                # real panel zoom or the streamer moving towards the lens. Only
                # a sustained, stronger boundary evidence change is accepted.
                if face_inside_accepted and (
                    _candidate_score(best) <= accepted_score + RESIZE_SCORE_MARGIN
                    and float(best.get("border", 0.0)) < accepted_border + 0.05
                ):
                    missing_count = 0
                    pending = None
                    pending_count = 0
                    emit(
                        Observation(
                            time_ms=time_ms,
                            state=STATE_VISIBLE,
                            rect=accepted,
                            confidence=min(0.96, 0.35 + accepted_score),
                            reason="Face moved inside the fixed webcam panel; panel size kept.",
                        )
                    )
                    continue
                if pending is not None and _candidate_plate(pending, rect):
                    pending_count += 1
                else:
                    pending = rect
                    pending_count = 1
                if pending_count >= SWITCH_PERSISTENCE:
                    accepted = rect
                    accepted_score = _candidate_score(best)
                    accepted_border = float(best.get("border", 0.0))
                    accepted_face = _candidate_face_rect(best) or accepted_face
                    missing_count = 0
                    pending = None
                    pending_count = 0
                    emit(
                        Observation(
                            time_ms=time_ms,
                            state=STATE_VISIBLE,
                            rect=accepted,
                            confidence=min(0.96, 0.35 + accepted_score),
                            reason="Sustained webcam-panel resize with stronger boundary evidence.",
                        )
                    )
                else:
                    emit(
                        Observation(
                            time_ms=time_ms,
                            state=STATE_UNCERTAIN,
                            rect=accepted,
                            confidence=min(0.90, 0.30 + accepted_score),
                            reason="Possible webcam-panel resize; keeping the accepted panel until it persists.",
                        )
                    )
                continue

            if face_inside_accepted:
                accepted_face = _accepted_face_rect(accepted, candidates) or accepted_face
                missing_count = 0
                pending = None
                pending_count = 0
                emit(
                    Observation(
                        time_ms=time_ms,
                        state=STATE_VISIBLE,
                        rect=accepted,
                        confidence=min(0.92, 0.35 + accepted_score),
                        reason="Streamer face remains inside the accepted webcam panel; unrelated faces ignored.",
                    )
                )
                continue
            if _accepted_boundary_supported(accepted, candidates):
                missing_count = 0
                pending = None
                pending_count = 0
                emit(
                    Observation(
                        time_ms=time_ms,
                        state=STATE_UNCERTAIN,
                        rect=accepted,
                        confidence=min(0.85, 0.30 + accepted_score * 0.8),
                        reason="Face is briefly absent, but the accepted panel boundary is still visible.",
                    )
                )
                continue

            missing_count += 1
            if missing_count < MIN_SWITCH_MISSING:
                emit(
                    Observation(
                        time_ms=time_ms,
                        state=STATE_UNCERTAIN,
                        rect=accepted,
                        confidence=0.35,
                        reason="Brief webcam-panel gap; keeping the last accepted region.",
                    )
                )
                continue

            switch = _best_switch_candidate(candidates)
            if switch is None:
                pending = None
                pending_count = 0
                emit(
                    Observation(
                        time_ms=time_ms,
                        state=STATE_MISSING,
                        rect=None,
                        confidence=0.0,
                        reason="Accepted webcam panel is missing and no plausible replacement was found.",
                    )
                )
                continue
            rect = _candidate_rect(switch)
            assert rect is not None
            if pending is not None and _candidate_plate(pending, rect):
                pending_count += 1
            else:
                pending = rect
                pending_count = 1
            if pending_count >= SWITCH_PERSISTENCE:
                accepted = rect
                accepted_score = _candidate_score(switch)
                accepted_border = float(switch.get("border", 0.0))
                accepted_face = _candidate_face_rect(switch) or accepted_face
                missing_count = 0
                pending = None
                pending_count = 0
                emit(
                    Observation(
                        time_ms=time_ms,
                        state=STATE_VISIBLE,
                        rect=accepted,
                        confidence=min(0.94, 0.35 + accepted_score),
                        reason="Webcam panel genuinely moved; the replacement persisted across samples.",
                    )
                )
            else:
                emit(
                    Observation(
                        time_ms=time_ms,
                        state=STATE_UNCERTAIN,
                        rect=None,
                        confidence=0.0,
                        reason="Accepted panel is missing; a different candidate is being verified, not adopted yet.",
                    )
                )
    return observations


def raw_observations(samples: Iterable[Any]) -> list[Observation]:
    observations: list[Observation] = []
    for sample in samples:
        features = getattr(sample, "features", {}) or {}
        rect = _rect_from_features(features)
        confidence = float(features.get("facecam_confidence", 0.0)) if rect else 0.0
        observations.append(
            Observation(
                time_ms=int(getattr(sample, "time_ms", 0)),
                state=STATE_VISIBLE if rect else STATE_MISSING,
                rect=rect,
                confidence=confidence,
                reason="Webcam-panel candidate detected." if rect else "No webcam panel in this sample.",
            )
        )
    return sorted(observations, key=lambda item: item.time_ms)


def smooth_observations(
    observations: list[Observation],
    *,
    window: int = 2,
) -> list[Observation]:
    """Median-filter the plate and add persistence hysteresis.

    The filter runs over visible neighbours only, so a missing frame cannot drag
    the plate towards the top-left origin. Hysteresis keeps the previous plate
    through one-frame detection errors and accepts a real move or zoom only when
    the new plate survives a second sample.
    """

    if not observations:
        return []

    filtered: list[Observation] = []
    for index, observation in enumerate(observations):
        if observation.state != STATE_VISIBLE or observation.rect is None:
            filtered.append(observation)
            continue
        neighbours = [
            item.rect
            for item in observations[max(0, index - window): index + window + 1]
            if item.state == STATE_VISIBLE and item.rect is not None
        ]
        if len(neighbours) < 2:
            filtered.append(observation)
            continue
        filtered.append(
            Observation(
                time_ms=observation.time_ms,
                state=STATE_VISIBLE,
                rect=_median_rect(neighbours),
                confidence=statistics.median(
                    item.confidence
                    for item in observations[max(0, index - window): index + window + 1]
                    if item.state == STATE_VISIBLE
                ),
                reason="Smoothed across neighbouring samples.",
            )
        )

    stable: RectTuple | None = None
    pending: RectTuple | None = None
    pending_count = 0
    missing_count = 0
    output: list[Observation] = []

    for observation in filtered:
        if observation.state == STATE_VISIBLE and observation.rect is not None:
            rect = observation.rect
            missing_count = 0
            if stable is None:
                stable = rect
                pending = None
                pending_count = 0
                output.append(observation)
                continue
            if _same_plate(stable, rect):
                pending = None
                pending_count = 0
                output.append(
                    Observation(
                        time_ms=observation.time_ms,
                        state=STATE_VISIBLE,
                        rect=stable,
                        confidence=observation.confidence,
                        reason="Stable face-cam plate; person movement stayed inside it.",
                    )
                )
                continue
            if pending is not None and _candidate_plate(pending, rect):
                pending_count += 1
            else:
                pending = rect
                pending_count = 1
            if pending_count >= MIN_CHANGE_OBSERVATIONS:
                stable = pending
                pending = None
                pending_count = 0
                output.append(
                    Observation(
                        time_ms=observation.time_ms,
                        state=STATE_VISIBLE,
                        rect=stable,
                        confidence=observation.confidence,
                        reason="Sustained change in face-cam position or size.",
                    )
                )
            else:
                output.append(
                    Observation(
                        time_ms=observation.time_ms,
                        state=STATE_UNCERTAIN,
                        rect=stable,
                        confidence=observation.confidence * 0.8,
                        reason="Possible face-cam change; waiting for another sample.",
                    )
                )
            continue

        missing_count += 1
        if missing_count == 1 and stable is not None:
            output.append(
                Observation(
                    time_ms=observation.time_ms,
                    state=STATE_UNCERTAIN,
                    rect=stable,
                    confidence=observation.confidence,
                    reason="Brief detection gap; keeping the last plate.",
                )
            )
        else:
            output.append(
                Observation(
                    time_ms=observation.time_ms,
                    state=STATE_MISSING,
                    rect=None,
                    confidence=0.0,
                    reason="Face-cam not detected for two or more samples.",
                )
            )

    return output


def change_windows(
    observations: list[Observation],
    *,
    pad_ms: int = 2_500,
    limit: int = 8,
) -> list[tuple[int, int]]:
    """Where a denser look is worth its cost.

    Only sustained candidates are interesting. A person moving inside a stable
    plate produces no window; a move, zoom, appearance or disappearance does.
    """

    candidates: list[int] = []
    previous: Observation | None = None
    for observation in observations:
        if previous is not None:
            if observation.state != previous.state:
                candidates.append(observation.time_ms)
            elif observation.state == STATE_VISIBLE and observation.rect and previous.rect:
                if not _same_plate(previous.rect, observation.rect):
                    candidates.append(observation.time_ms)
        previous = observation

    windows: list[tuple[int, int]] = []
    for time_ms in sorted(set(candidates)):
        start = max(0, time_ms - pad_ms)
        end = time_ms + pad_ms
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows[:limit]


def _override_for(time_ms: int, overrides: Sequence[dict]) -> dict | None:
    for override in overrides:
        if int(override.get("start_ms", 0)) <= time_ms < int(override.get("end_ms", 0)):
            return override
    return None


def _rect_from_dict(value: Any) -> RectTuple | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return None
    try:
        x = _clamp(float(value["x_pct"]))
        y = _clamp(float(value["y_pct"]))
        width = max(0.02, min(1.0 - x, float(value["width_pct"])))
        height = max(0.02, min(1.0 - y, float(value["height_pct"])))
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y, width, height)


def _segments_from_observations(
    observations: list[Observation],
    start_ms: int,
    end_ms: int,
    overrides: Sequence[dict],
) -> list[TrackRange]:
    boundaries = {start_ms, end_ms}
    boundaries.update(item.time_ms for item in observations if start_ms <= item.time_ms <= end_ms)
    for override in overrides:
        boundaries.add(max(start_ms, min(end_ms, int(override.get("start_ms", start_ms)))))
        boundaries.add(max(start_ms, min(end_ms, int(override.get("end_ms", end_ms)))))
    ordered = sorted(value for value in boundaries if start_ms <= value <= end_ms)
    if len(ordered) < 2:
        ordered = [start_ms, max(start_ms + 1, end_ms)]

    ranges: list[TrackRange] = []
    for left, right in zip(ordered, ordered[1:]):
        if right <= left:
            continue
        midpoint = left + (right - left) // 2
        override = _override_for(midpoint, overrides)
        if override is not None and str(override.get("mode") or "lock") != "track":
            rect = _rect_from_dict(override.get("rect"))
            if rect is not None:
                ranges.append(
                    TrackRange(
                        start_ms=left,
                        end_ms=right,
                        state=STATE_MANUAL,
                        rect=rect,
                        confidence=1.0,
                        reason=str(override.get("reason") or "Manual face-cam region."),
                    )
                )
                continue
        before = [item for item in observations if item.time_ms <= midpoint]
        observation = before[-1] if before else None
        if observation is None:
            ranges.append(
                TrackRange(
                    start_ms=left,
                    end_ms=right,
                    state=STATE_MISSING,
                    rect=None,
                    confidence=0.0,
                    reason="No face-cam sample covered this range.",
                )
            )
        else:
            ranges.append(
                TrackRange(
                    start_ms=left,
                    end_ms=right,
                    state=observation.state,
                    rect=observation.rect,
                    face_rect=observation.face_rect,
                    confidence=observation.confidence,
                    reason=observation.reason,
                )
            )

    merged: list[TrackRange] = []
    for item in ranges:
        if item.face_rect is not None and item.rect is not None:
            # A face larger than this is usually a false positive on a game
            # character or a body region, not the streamer inside a small cam.
            if (
                item.face_rect[2] > item.rect[2] * 0.55
                or item.face_rect[3] > item.rect[3] * 0.65
            ):
                item.face_rect = None
        if merged:
            previous = merged[-1]
            same_rect = (
                previous.rect is not None
                and item.rect is not None
                and _same_plate(previous.rect, item.rect)
            )
            same_missing = previous.rect is None and item.rect is None
            if previous.state == item.state and (same_rect or same_missing):
                previous.end_ms = item.end_ms
                previous.confidence = max(previous.confidence, item.confidence)
                if item.rect is not None:
                    previous.rect = item.rect
                if item.face_rect is not None:
                    previous.face_rect = item.face_rect
                continue
        merged.append(item)

    for item in merged:
        if item.rect is None:
            continue
        faces = [
            observation.face_rect
            for observation in observations
            if item.start_ms <= observation.time_ms < item.end_ms
            and observation.face_rect is not None
            and observation.face_rect[2] <= item.rect[2] * 0.55
            and observation.face_rect[3] <= item.rect[3] * 0.65
        ]
        if faces:
            item.face_rect = _median_rect(faces)
    return merged


def track_facecam(
    samples: Iterable[Any],
    start_ms: int,
    end_ms: int,
    *,
    overrides: Sequence[dict] | None = None,
    sample_fps: float | None = None,
) -> FaceCamTrack:
    """Build the canonical face-cam track for one analysed window."""

    materialised = list(samples)
    uses_candidates = any(getattr(sample, "facecam_candidates", None) for sample in materialised)
    if uses_candidates:
        observations = candidate_observations(materialised, overrides=overrides or [])
    else:
        observations = smooth_observations(raw_observations(materialised))
    ranges = _segments_from_observations(observations, start_ms, end_ms, overrides or [])
    warnings: list[str] = []
    if not observations:
        warnings.append("No face-cam samples were available; the requested layout falls back to a full crop.")
    elif all(item.state == STATE_MISSING for item in ranges):
        warnings.append("No face-cam or webcam panel was detected anywhere in this window.")
    elif any(item.state == STATE_MISSING for item in ranges):
        warnings.append("The webcam panel disappears for part of this clip; those ranges fall back safely.")
    if any(item.state == STATE_UNCERTAIN for item in ranges):
        warnings.append("Part of the webcam-panel track is uncertain; review the flagged interval.")
    return FaceCamTrack(
        observations=observations,
        ranges=ranges,
        sample_fps=sample_fps,
        warnings=warnings,
    )


def ranges_for_span(
    track: FaceCamTrack,
    start_ms: int,
    end_ms: int,
) -> list[TrackRange]:
    """Intersect the tracked ranges with one output segment."""

    result: list[TrackRange] = []
    for item in track.ranges:
        left = max(start_ms, item.start_ms)
        right = min(end_ms, item.end_ms)
        if right <= left:
            continue
        result.append(
            TrackRange(
                start_ms=left,
                end_ms=right,
                state=item.state,
                rect=item.rect,
                face_rect=item.face_rect,
                confidence=item.confidence,
                reason=item.reason,
            )
        )
    if not result:
        result.append(
            TrackRange(
                start_ms=start_ms,
                end_ms=end_ms,
                state=STATE_MISSING,
                rect=None,
                confidence=0.0,
                reason="No tracked face-cam range covered this segment.",
            )
        )
    return result
