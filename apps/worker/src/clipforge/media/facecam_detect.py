"""Webcam-panel detection for a pre-composited gameplay stream.

A face is evidence that a webcam may be present, not proof of where its panel
is. This module therefore has two stages:

1. YuNet finds faces in a frame.
2. A bounded set of rectangular candidates is scored. Face-seeded rectangles
   are one source of candidates; independent edge/corner rectangles are another.
   Each candidate is scored by the visual boundary between the candidate and
   the surrounding gameplay, its geometry, and whether a face sits inside it.

The result is a list of time-local candidates, not an identity. Continuity,
locking and rejection of unrelated faces belong to ``media.facecam``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

try:  # onnxruntime is already a worker dependency for the ONNX runtime path.
    import onnxruntime as ort
except ImportError:  # pragma: no cover - exercised only in stripped installs
    ort = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

FACECAM_DETECTOR_VERSION = "yunet-panel-2023mar-7"
MODEL_PATH = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"
INPUT_SIZE = 640
SCORE_THRESHOLD = 0.55
NMS_THRESHOLD = 0.30
MAX_FACES = 50
# A webcam panel is normally a compact rectangle at an edge. The thresholds
# below reject the large, centred face of a full-screen camera scene while still
# allowing a bottom-centre or corner plate.
MIN_PANEL_AREA = 0.004
MAX_PANEL_AREA = 0.24
MIN_FACE_WIDTH = 0.010
PANEL_ASPECT = 16.0 / 9.0

_SESSION = None
_LOAD_ERROR: str | None = None


@dataclass(frozen=True)
class FaceBox:
    """A face rectangle in original-frame pixels."""

    x: float
    y: float
    width: float
    height: float
    score: float


def detector_available() -> bool:
    """Return whether the ONNX detector can be loaded in this process."""

    return _session() is not None


def detector_error() -> str | None:
    """Return the last model-loading error, if any."""

    _session()
    return _LOAD_ERROR


def _session():
    global _SESSION, _LOAD_ERROR
    if _SESSION is not None:
        return _SESSION
    if _LOAD_ERROR is not None:
        return None
    if ort is None:
        _LOAD_ERROR = "onnxruntime is not installed"
        logger.warning("Face-cam detection is unavailable: %s", _LOAD_ERROR)
        return None
    if not MODEL_PATH.exists():
        _LOAD_ERROR = f"face detector model is missing at {MODEL_PATH}"
        logger.warning("Face-cam detection is unavailable: %s", _LOAD_ERROR)
        return None
    try:
        _SESSION = ort.InferenceSession(
            str(MODEL_PATH),
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:  # pragma: no cover - depends on a broken install
        _LOAD_ERROR = f"face detector could not be loaded: {exc}"
        logger.warning("Face-cam detection is unavailable: %s", _LOAD_ERROR)
        return None
    return _SESSION


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _letterbox(rgb: np.ndarray) -> tuple[np.ndarray, float, int, int]:
    """Scale an RGB frame into a square 640x640 BGR tensor.

    YuNet was trained on BGR pixels in the 0..255 range. Letterboxing preserves
    the face's shape; the scale and offsets are returned so boxes can be mapped
    back to the original frame without using preview coordinates directly.
    """

    height, width = rgb.shape[:2]
    scale = min(INPUT_SIZE / max(1, width), INPUT_SIZE / max(1, height))
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    left = (INPUT_SIZE - new_w) // 2
    top = (INPUT_SIZE - new_h) // 2
    canvas = Image.new("RGB", (INPUT_SIZE, INPUT_SIZE), (0, 0, 0))
    resized = Image.fromarray(rgb).resize((new_w, new_h), Image.Resampling.BILINEAR)
    canvas.paste(resized, (left, top))
    array = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]
    return array.transpose(2, 0, 1)[None], scale, left, top


def _nms(boxes: list[FaceBox]) -> list[FaceBox]:
    if len(boxes) <= 1:
        return boxes
    ordered = sorted(boxes, key=lambda item: item.score, reverse=True)
    keep: list[FaceBox] = []
    for candidate in ordered:
        cx0, cy0 = candidate.x, candidate.y
        cx1, cy1 = candidate.x + candidate.width, candidate.y + candidate.height
        suppressed = False
        for kept in keep:
            kx0, ky0 = kept.x, kept.y
            kx1, ky1 = kept.x + kept.width, kept.y + kept.height
            ix0, iy0 = max(cx0, kx0), max(cy0, ky0)
            ix1, iy1 = min(cx1, kx1), min(cy1, ky1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            intersection = (ix1 - ix0) * (iy1 - iy0)
            union = candidate.width * candidate.height + kept.width * kept.height - intersection
            if union > 0 and intersection / union > NMS_THRESHOLD:
                suppressed = True
                break
        if not suppressed:
            keep.append(candidate)
            if len(keep) >= MAX_FACES:
                break
    return keep


def detect_faces(rgb: np.ndarray) -> list[FaceBox]:
    """Detect faces and return their original-frame pixel rectangles."""

    session = _session()
    if session is None or rgb.ndim != 3 or rgb.shape[2] < 3:
        return []
    height, width = rgb.shape[:2]
    if width < 32 or height < 32:
        return []

    blob, scale, left, top = _letterbox(rgb)
    try:
        outputs = session.run(None, {"input": blob})
    except Exception as exc:  # pragma: no cover - depends on a broken model
        logger.warning("Face detection inference failed: %s", exc)
        return []

    names = [item.name for item in session.get_outputs()]
    values = dict(zip(names, outputs, strict=False))
    boxes: list[FaceBox] = []
    for stride in (8, 16, 32):
        try:
            cls = values[f"cls_{stride}"][0, :, 0]
            obj = values[f"obj_{stride}"][0, :, 0]
            bbox = values[f"bbox_{stride}"][0]
        except KeyError:
            continue
        score = np.sqrt(np.clip(cls, 0.0, 1.0) * np.clip(obj, 0.0, 1.0))
        cols = INPUT_SIZE // stride
        for index in np.where(score >= SCORE_THRESHOLD)[0]:
            row, column = divmod(int(index), cols)
            cx = (column + float(bbox[index, 0])) * stride
            cy = (row + float(bbox[index, 1])) * stride
            box_w = math.exp(float(bbox[index, 2])) * stride
            box_h = math.exp(float(bbox[index, 3])) * stride
            x = (cx - box_w / 2.0 - left) / scale
            y = (cy - box_h / 2.0 - top) / scale
            box_w /= scale
            box_h /= scale
            x = max(0.0, min(float(width), x))
            y = max(0.0, min(float(height), y))
            box_w = max(0.0, min(float(width) - x, box_w))
            box_h = max(0.0, min(float(height) - y, box_h))
            if box_w < 4.0 or box_h < 4.0:
                continue
            boxes.append(FaceBox(x, y, box_w, box_h, float(score[index])))
    return _nms(boxes)


def _iou(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    ix0, iy0 = max(lx, rx), max(ly, ry)
    ix1, iy1 = min(lx + lw, rx + rw), min(ly + lh, ry + rh)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    intersection = (ix1 - ix0) * (iy1 - iy0)
    union = lw * lh + rw * rh - intersection
    return intersection / max(1e-6, union)


def _face_containment(
    rect: tuple[float, float, float, float],
    faces: list[FaceBox],
    frame_width: int,
    frame_height: int,
) -> tuple[float, float, float, float, float, float]:
    """Return (contained fraction, best face score) for a normalised rect.

    A face centre just inside the edge is not enough. The fraction of the face
    box inside the panel must be substantial, which keeps a game character
    touching a panel boundary from becoming the panel's identity.
    """

    x, y, width, height = rect
    best_fraction = 0.0
    best_score = 0.0
    best_center_x = 0.0
    best_center_y = 0.0
    best_width = 0.0
    best_height = 0.0
    for face in faces:
        fx = face.x / max(1, frame_width)
        fy = face.y / max(1, frame_height)
        fw = face.width / max(1, frame_width)
        fh = face.height / max(1, frame_height)
        ix0, iy0 = max(x, fx), max(y, fy)
        ix1, iy1 = min(x + width, fx + fw), min(y + height, fy + fh)
        if ix1 <= ix0 or iy1 <= iy0:
            continue
        fraction = ((ix1 - ix0) * (iy1 - iy0)) / max(1e-9, fw * fh)
        if fraction > best_fraction:
            best_fraction = fraction
            best_score = face.score
            best_center_x = fx + fw / 2.0
            best_center_y = fy + fh / 2.0
            best_width = fw
            best_height = fh
    return best_fraction, best_score, best_center_x, best_center_y, best_width, best_height


def _panel_evidence(
    rgb: np.ndarray,
    rect: tuple[float, float, float, float],
    faces: list[FaceBox],
    gradient: np.ndarray | None = None,
    array: np.ndarray | None = None,
    total_sum: np.ndarray | None = None,
) -> dict[str, float]:
    """Score how much a rectangle looks like a distinct overlay panel."""

    frame_height, frame_width = rgb.shape[:2]
    x, y, width, height = rect
    px0 = int(round(x * frame_width))
    py0 = int(round(y * frame_height))
    px1 = int(round((x + width) * frame_width))
    py1 = int(round((y + height) * frame_height))
    if px1 - px0 < 12 or py1 - py0 < 12:
        return {
            "score": 0.0, "border": 0.0, "interior": 0.0, "edge": 0.0,
            "face_inside": 0.0, "face_score": 0.0, "face_cx": 0.0, "face_cy": 0.0,
            "face_w": 0.0, "face_h": 0.0,
        }

    if array is None:
        array = rgb.astype(np.float32)
    band = max(2, int(round(min(px1 - px0, py1 - py0) * 0.035)))
    inner_x0, inner_y0 = min(px1, px0 + band), min(py1, py0 + band)
    inner_x1, inner_y1 = max(inner_x0 + 1, px1 - band), max(inner_y0 + 1, py1 - band)
    outer_x0, outer_y0 = max(0, px0 - band), max(0, py0 - band)
    outer_x1, outer_y1 = min(frame_width, px1 + band), min(frame_height, py1 + band)

    inner = array[inner_y0:inner_y1, inner_x0:inner_x1]
    outer = array[outer_y0:outer_y1, outer_x0:outer_x1]
    if inner.size == 0 or outer.size == 0:
        return {
            "score": 0.0, "border": 0.0, "interior": 0.0, "edge": 0.0,
            "face_inside": 0.0, "face_score": 0.0, "face_cx": 0.0, "face_cy": 0.0,
            "face_w": 0.0, "face_h": 0.0,
        }
    inner_mean = inner.reshape(-1, 3).mean(axis=0)
    outer_mean = outer.reshape(-1, 3).mean(axis=0)
    border = float(np.abs(inner_mean - outer_mean).mean() / 255.0)

    interior = array[py0:py1, px0:px1]
    if total_sum is None:
        total_sum = array.sum(axis=(0, 1))
    total_pixels = max(1, frame_height * frame_width)
    interior_sum = interior.sum(axis=(0, 1))
    interior_pixels = max(1, interior.shape[0] * interior.shape[1])
    exterior_mean = (total_sum - interior_sum) / max(1, total_pixels - interior_pixels) if total_pixels > interior_pixels else outer_mean
    interior_mean = interior_sum / interior_pixels
    interior_difference = float(np.abs(interior_mean - exterior_mean).mean() / 255.0)

    if gradient is None:
        luma = 0.299 * array[:, :, 0] + 0.587 * array[:, :, 1] + 0.114 * array[:, :, 2]
        grad_y, grad_x = np.gradient(luma)
        gradient = np.sqrt(grad_x * grad_x + grad_y * grad_y)
    edge = float(gradient[py0:py1, px0:px1].mean() * 5.0)

    face_inside, face_score, face_cx, face_cy, face_w, face_h = _face_containment(
        rect, faces, frame_width, frame_height
    )
    area = width * height
    center_x, center_y = x + width / 2.0, y + height / 2.0
    corner = min(center_x, 1.0 - center_x, center_y, 1.0 - center_y)
    area_score = _clamp((0.19 - area) / 0.13)
    corner_score = _clamp(1.0 - corner / 0.32)
    score = (
        0.35 * face_score * face_inside
        + 0.25 * _clamp(border * 4.0)
        + 0.15 * _clamp(interior_difference * 3.0)
        + 0.10 * _clamp(edge)
        + 0.10 * area_score
        + 0.05 * corner_score
    )
    # A large, centred rectangle is how a full-screen camera looks. Keep it as
    # a weak candidate so a compact edge plate wins instead.
    if area > 0.17 and corner > 0.30:
        score *= 0.25
    if area > 0.20:
        score *= 0.55
    return {
        "score": _clamp(score),
        "border": _clamp(border * 4.0),
        "interior": _clamp(interior_difference * 3.0),
        "edge": _clamp(edge),
        "face_inside": face_inside,
        "face_score": face_score,
        "face_cx": face_cx,
        "face_cy": face_cy,
        "face_w": face_w,
        "face_h": face_h,
    }


def _line_score(profile: np.ndarray, contrast: np.ndarray) -> float:
    return float(profile) + float(contrast) * 55.0


def _refine_panel_edges(
    rgb: np.ndarray,
    rect: tuple[float, float, float, float],
    gradient: np.ndarray,
) -> tuple[float, float, float, float]:
    """Snap a face-derived rectangle to the strongest nearby panel border.

    A face tells us roughly where a webcam is. The border, when it exists, tells
    us where the panel actually ends. Searching is local and bounded so a game
    edge elsewhere cannot pull the rectangle across the frame.
    """

    frame_height, frame_width = rgb.shape[:2]
    x, y, width, height = rect
    array = rgb.astype(np.float32)
    search_x = max(3, int(round(frame_width * 0.06)))
    search_y = max(3, int(round(frame_height * 0.09)))

    def horizontal_edge(target: int, left: int, right: int, *, span: int | None = None) -> int:
        reach = search_y if span is None else span
        low = max(2, target - reach)
        high = min(frame_height - 3, target + reach)
        scored: list[tuple[float, int]] = []
        for row in range(low, high + 1):
            above = array[row - 1:row, left:right]
            below = array[row:row + 1, left:right]
            if above.size == 0 or below.size == 0:
                continue
            contrast = float(np.abs(above.mean(axis=(0, 1)) - below.mean(axis=(0, 1))).mean() / 255.0)
            scored.append((_line_score(gradient[row, left:right].mean(), contrast), row))
        if not scored:
            return target
        return max(scored, key=lambda item: item[0])[1]

    def vertical_edge(target: int, top: int, bottom: int, *, leftmost: bool = False) -> int:
        low = max(2, target - search_x)
        high = min(frame_width - 3, target + search_x)
        scored: list[tuple[float, int]] = []
        for col in range(low, high + 1):
            left = array[top:bottom, col - 1:col]
            right = array[top:bottom, col:col + 1]
            if left.size == 0 or right.size == 0:
                continue
            contrast = float(np.abs(left.mean(axis=(0, 1)) - right.mean(axis=(0, 1))).mean() / 255.0)
            scored.append((_line_score(gradient[top:bottom, col].mean(), contrast), col))
        if not scored:
            return target
        best = max(score for score, _ in scored)
        candidates = [col for score, col in scored if score >= best * 0.80]
        return min(candidates) if leftmost else max(candidates)

    px0 = int(round(x * frame_width))
    py0 = int(round(y * frame_height))
    px1 = int(round((x + width) * frame_width))
    py1 = int(round((y + height) * frame_height))
    py0 = horizontal_edge(py0, max(0, px0), min(frame_width, px1))
    py1 = horizontal_edge(
        py1, max(0, px0), min(frame_width, px1),
        span=max(3, int(round(frame_height * 0.02))),
    )
    px0 = vertical_edge(px0, max(0, py0), min(frame_height, py1), leftmost=True)
    px1 = vertical_edge(px1, max(0, py0), min(frame_height, py1))
    rx = max(0.0, px0 / frame_width)
    ry = max(0.0, py0 / frame_height)
    rw = max(0.02, min(1.0 - rx, (px1 - px0) / frame_width))
    rh = max(0.02, min(1.0 - ry, (py1 - py0) / frame_height))
    if rw * rh > MAX_PANEL_AREA or rw < 0.05 or rh < 0.05:
        return rect
    return (rx, ry, rw, rh)


def _panel_from_face(face: FaceBox, frame_width: int, frame_height: int) -> tuple[float, float, float, float] | None:
    face_w = face.width / max(1, frame_width)
    face_h = face.height / max(1, frame_height)
    if face_w < MIN_FACE_WIDTH:
        return None
    panel_w = _clamp(face_w * 6.0, 0.06, 0.45)
    panel_h = panel_w * (frame_width / max(1, frame_height)) / PANEL_ASPECT
    panel_w = max(panel_w, face_w * 3.2)
    panel_h = max(panel_h, face_h * 2.4)
    panel_w = _clamp(panel_w, 0.04, 0.50)
    panel_h = _clamp(panel_h, 0.04, 0.50)
    face_cx = (face.x + face.width / 2.0) / max(1, frame_width)
    face_cy = (face.y + face.height * 0.45) / max(1, frame_height)
    x = _clamp(face_cx - panel_w / 2.0, 0.0, 1.0 - panel_w)
    y = _clamp(face_cy - panel_h * 0.42, 0.0, 1.0 - panel_h)
    area = panel_w * panel_h
    if area < MIN_PANEL_AREA or area > MAX_PANEL_AREA:
        return None
    return (x, y, panel_w, panel_h)


def _grid_candidates(frame_width: int, frame_height: int) -> list[tuple[float, float, float, float]]:
    """Independent edge/corner rectangles, used even when no face is found."""

    candidates: list[tuple[float, float, float, float]] = []
    for width in (0.18, 0.28):
        height = width * (frame_width / max(1, frame_height)) / PANEL_ASPECT
        if height <= 0.02:
            continue
        # Corners and the common bottom-centre/edge positions. This is only a
        # continuity prop for frames where YuNet misses the streamer; the
        # face-seeded rectangles carry the precise geometry.
        for x in (0.0, 0.97 - width):
            for y in (0.0, 0.72):
                if x < 0 or y < 0 or x + width > 1.0 or y + height > 1.0:
                    continue
                candidates.append((x, y, width, height))
        x = 0.50 - width / 2.0
        if 0 <= x and 0.72 + height <= 1.0:
            candidates.append((x, 0.72, width, height))
    return candidates


def detect_facecam_candidates(rgb: np.ndarray) -> list[dict[str, Any]]:
    """Return scored panel candidates for one frame.

    The list includes face-seeded and independent rectangles. It is deliberately
    small and explainable; continuity and locking are applied by the tracker.
    """

    session = _session()
    if session is None or rgb.ndim != 3 or rgb.shape[2] < 3:
        return []
    frame_height, frame_width = rgb.shape[:2]
    faces = detect_faces(rgb)
    array = rgb.astype(np.float32)
    luma = 0.299 * array[:, :, 0] + 0.587 * array[:, :, 1] + 0.114 * array[:, :, 2]
    grad_y, grad_x = np.gradient(luma)
    gradient = np.sqrt(grad_x * grad_x + grad_y * grad_y)
    total_sum = array.sum(axis=(0, 1))
    rectangles: list[tuple[float, float, float, float]] = []
    for face in faces:
        rect = _panel_from_face(face, frame_width, frame_height)
        if rect is not None:
            rectangles.append(_refine_panel_edges(rgb, rect, gradient))
    rectangles.extend(_grid_candidates(frame_width, frame_height))

    candidates: list[dict[str, Any]] = []
    for rect in rectangles:
        evidence = _panel_evidence(rgb, rect, faces, gradient, array, total_sum)
        area = rect[2] * rect[3]
        if area < MIN_PANEL_AREA or area > MAX_PANEL_AREA:
            continue
        candidates.append(
            {
                "x": rect[0],
                "y": rect[1],
                "width": rect[2],
                "height": rect[3],
                "area": area,
                **evidence,
            }
        )

    # Deduplicate near-identical rectangles, keeping the stronger evidence.
    candidates.sort(key=lambda item: float(item["score"]), reverse=True)
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        rect = (candidate["x"], candidate["y"], candidate["width"], candidate["height"])
        if any(
            _iou(rect, (other["x"], other["y"], other["width"], other["height"])) > 0.62
            for other in kept
        ):
            continue
        kept.append(candidate)
        if len(kept) >= 12:
            break
    return kept


def facecam_features_from_candidates(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """Reduce a candidate list to the flat features stored with each sample."""

    if not candidates:
        return {
            "facecam_present": 0.0,
            "facecam_x": 0.0,
            "facecam_y": 0.0,
            "facecam_width": 0.0,
            "facecam_height": 0.0,
            "facecam_confidence": 0.0,
            "facecam_skin_density": 0.0,
            "facecam_outside_skin": 0.0,
            "facecam_corner": 0.0,
            "facecam_face_score": 0.0,
            "facecam_panel_area": 0.0,
            "facecam_candidate_count": 0.0,
            "facecam_detector": 1.0,
        }

    face_candidates = [item for item in candidates if item["face_inside"] >= 0.50]
    best = max(face_candidates or candidates, key=lambda item: float(item["score"]))
    confidence = _clamp(0.25 + 0.70 * float(best["score"]) + 0.05 * float(best["face_score"]), 0.0, 0.96)
    center_x = float(best["x"]) + float(best["width"]) / 2.0
    center_y = float(best["y"]) + float(best["height"]) / 2.0
    return {
        "facecam_present": 1.0 if best["face_inside"] >= 0.50 else 0.0,
        "facecam_x": float(best["x"]),
        "facecam_y": float(best["y"]),
        "facecam_width": float(best["width"]),
        "facecam_height": float(best["height"]),
        "facecam_confidence": confidence,
        "facecam_skin_density": 0.0,
        "facecam_outside_skin": 0.0,
        "facecam_corner": 1.0 if min(center_x, 1.0 - center_x, center_y, 1.0 - center_y) <= 0.20 else 0.0,
        "facecam_face_score": float(best["face_score"]),
        "facecam_panel_area": float(best["area"]),
        "facecam_candidate_count": float(len(candidates)),
        "facecam_detector": 1.0,
    }


def detect_facecam_features(rgb: np.ndarray) -> dict[str, float] | None:
    """Return the best per-frame face-cam evidence for ``frame_features``.

    ``None`` means the detector itself is unavailable. An empty result is
    different: the detector ran and found no face inside a usable panel, so the
    caller must not silently keep a skin-colour guess.
    """

    if not detector_available():
        return None
    return facecam_features_from_candidates(detect_facecam_candidates(rgb))
