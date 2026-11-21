"""Deterministic, CPU-only scene analysis for composition decisions.

It detects cuts with FFmpeg's scene score, samples the window, and classifies
each frame from cheap, explainable features: luminance, edge density, colour
spread, letterboxing and a skin-tone centroid. When a caller needs a webcam
track, the same frames are decoded at a higher face-detection height and passed
through YuNet plus a bounded, explainable panel-candidate scorer. OCR and
saliency models can replace individual functions later; the composition
contract stays the same.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from clipforge.media.facecam import (
    FaceCamTrack,
    change_windows,
    raw_observations,
    track_facecam as build_facecam_track,
)
from clipforge.media.facecam_detect import (
    FACECAM_DETECTOR_VERSION,
    detect_facecam_candidates,
    detector_available,
    detector_error,
    facecam_features_from_candidates,
)
from clipforge.media.process import ProcessFailed, require, run

logger = logging.getLogger(__name__)

SCENE_THRESHOLD = 0.28
MIN_SCENE_MS = 1_200
MAX_SAMPLES = 600
CACHE_VERSION = 17
# YuNet needs more pixels than the scene classifier. Sampling the tracking pass
# at 360p keeps a small corner face above roughly 20px while the scene features
# are still computed from the 144p proxy.
FACECAM_ANALYSIS_HEIGHT = 360
# Text smaller than roughly a 720p frame cannot be read back at 144p. The sample
# height is recorded with every analysis so a conclusion that rests on an
# unreadable frame is never presented as if the text had been verified.
DEFAULT_ANALYSIS_HEIGHT = 144
TEXT_READABLE_HEIGHT = 720
LOW_EVIDENCE_HEIGHT = 480
_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


@dataclass
class FrameSample:
    time_ms: int
    content_type: str
    confidence: float
    subject_x: float
    features: dict
    # Scored webcam-panel candidates for this frame. Keeping the full bounded
    # list lets the tracker choose the candidate that continues the accepted
    # panel, instead of trusting the globally strongest face in the frame.
    facecam_candidates: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "time_ms": self.time_ms,
            "content_type": self.content_type,
            "confidence": round(self.confidence, 3),
            "subject_x": round(self.subject_x, 3),
            "features": {key: round(float(value), 4) for key, value in self.features.items()},
            "facecam_candidates": [
                {
                    key: round(value, 5) if isinstance(value, float) else value
                    for key, value in candidate.items()
                }
                for candidate in self.facecam_candidates
            ],
        }


@dataclass
class Scene:
    start_ms: int
    end_ms: int
    content_type: str
    confidence: float
    subject_x: float
    features: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "content_type": self.content_type,
            "confidence": round(self.confidence, 3),
            "subject_x": round(self.subject_x, 3),
            "features": {key: round(float(value), 4) for key, value in self.features.items()},
            "warnings": self.warnings,
        }


@dataclass
class SceneAnalysis:
    scenes: list[Scene]
    samples: list[FrameSample]
    cuts: list[int]
    warnings: list[str] = field(default_factory=list)
    analysis: dict = field(default_factory=dict)
    facecam_track: FaceCamTrack | None = None

    def to_dict(self) -> dict:
        return {
            "version": CACHE_VERSION,
            "scenes": [scene.to_dict() for scene in self.scenes],
            "samples": [sample.to_dict() for sample in self.samples],
            "cuts": self.cuts,
            "warnings": self.warnings,
            "analysis": self.analysis,
            "facecam_track": self.facecam_track.to_dict() if self.facecam_track else None,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "SceneAnalysis":
        scenes = [
            Scene(
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
                content_type=str(item.get("content_type") or "unknown"),
                confidence=float(item.get("confidence") or 0.0),
                subject_x=float(item.get("subject_x") or 0.5),
                features=item.get("features") or {},
                warnings=list(item.get("warnings") or []),
            )
            for item in payload.get("scenes") or []
        ]
        samples = [
            FrameSample(
                time_ms=int(item["time_ms"]),
                content_type=str(item.get("content_type") or "unknown"),
                confidence=float(item.get("confidence") or 0.0),
                subject_x=float(item.get("subject_x") or 0.5),
                features=item.get("features") or {},
                facecam_candidates=list(item.get("facecam_candidates") or []),
            )
            for item in payload.get("samples") or []
        ]
        return cls(scenes=scenes, samples=samples, cuts=[int(value) for value in payload.get("cuts") or []],
                   warnings=list(payload.get("warnings") or []),
                   analysis=dict(payload.get("analysis") or {}),
                   facecam_track=FaceCamTrack.from_dict(payload.get("facecam_track")))


def _cache_key(
    path: Path,
    start_ms: int,
    end_ms: int,
    fps: float,
    height: int,
    *,
    track: bool = False,
    facecam_height: int = 0,
    overrides: list[dict] | None = None,
) -> str:
    try:
        stat = path.stat()
        identity = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        identity = str(path)
    override_digest = ""
    if overrides:
        override_digest = hashlib.sha1(
            json.dumps(overrides, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]
    raw = (
        f"{CACHE_VERSION}|{identity}|{start_ms}|{end_ms}|{fps:.3f}|{height}"
        f"|track={int(track)}|facecam_height={facecam_height}"
        f"|detector={FACECAM_DETECTOR_VERSION if track else 'off'}"
        f"|overrides={override_digest}"
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def sampling_fps(duration_ms: int, desired: float) -> float:
    """Keep the sample count bounded by slowing the rate, never by truncating.

    Capping the frame count with ``-frames:v`` used to stop extraction partway
    through a long window, so the tail of the window silently had no samples and
    their timestamps drifted. Lowering the rate covers the whole window instead.
    """
    if duration_ms <= 0:
        return max(0.1, desired)
    ceiling = MAX_SAMPLES / (duration_ms / 1000.0)
    return max(0.1, min(float(desired), ceiling))


def _detect_cuts(path: Path, start_ms: int, duration_ms: int) -> tuple[list[int], list[str]]:
    warnings: list[str] = []
    try:
        ffmpeg = require("ffmpeg")
    except Exception as exc:
        return [], [f"Scene detection is unavailable: {exc}"]
    try:
        result = run(
            [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info",
                "-ss", f"{start_ms / 1000:.3f}", "-t", f"{max(0.2, duration_ms / 1000):.3f}",
                "-i", str(path), "-an",
                "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
                "-f", "null", "-",
            ],
            timeout_seconds=180,
        )
    except (ProcessFailed, OSError) as exc:
        return [], [f"Scene detection failed; using one scene: {exc}"]
    if result.returncode != 0:
        return [], ["Scene detection failed; using one scene."]
    cuts: list[int] = []
    for line in result.stderr.splitlines():
        match = _PTS_RE.search(line)
        if not match:
            continue
        cut = start_ms + int(round(float(match.group(1)) * 1000))
        if start_ms + 400 < cut < start_ms + duration_ms - 400:
            cuts.append(cut)
    return sorted(set(cuts)), warnings


def _extract_samples(
    path: Path,
    start_ms: int,
    duration_ms: int,
    frames_dir: Path,
    *,
    fps: float,
    analysis_height: int,
    facecam_height: int | None = None,
) -> tuple[list[FrameSample], list[str], tuple[int, int] | None]:
    warnings: list[str] = []
    frames_dir.mkdir(parents=True, exist_ok=True)
    for child in frames_dir.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)
    # Decode once at the larger of the two heights, then shrink a copy for the
    # cheap scene features. A second FFmpeg pass would double the decode cost and
    # still leave the two passes at different timestamps.
    tracking_height = max(analysis_height, facecam_height or analysis_height)
    max_frames = min(MAX_SAMPLES, max(1, int(duration_ms / 1000 * fps) + 2))
    try:
        ffmpeg = require("ffmpeg")
        result = run(
            [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start_ms / 1000:.3f}", "-t", f"{max(0.2, duration_ms / 1000):.3f}",
                "-i", str(path), "-an",
                "-vf", f"fps={fps:.4f},scale=-2:{tracking_height}",
                "-frames:v", str(max_frames), "-q:v", "4",
                str(frames_dir / "%05d.jpg"),
            ],
            timeout_seconds=180,
        )
    except (ProcessFailed, OSError) as exc:
        return [], [f"Frame sampling failed; composition will use a safe fallback: {exc}"], None
    if result.returncode != 0:
        return [], ["Frame sampling failed; composition will use a safe fallback."], None

    use_yunet = bool(facecam_height) and detector_available()
    samples: list[FrameSample] = []
    frame_size: tuple[int, int] | None = None
    for index, frame in enumerate(sorted(frames_dir.glob("*.jpg")), start=0):
        try:
            with Image.open(frame) as image:
                tracking_rgb = np.asarray(image.convert("RGB"))
                frame_size = frame_size or (int(image.width), int(image.height))
        except OSError:
            continue
        time_ms = start_ms + int(round(index * 1000 / fps))
        feature_rgb = tracking_rgb
        if tracking_rgb.shape[0] != analysis_height:
            scaled_height = max(1, int(analysis_height))
            scaled_width = max(1, int(round(tracking_rgb.shape[1] * scaled_height / max(1, tracking_rgb.shape[0]))))
            feature_rgb = np.asarray(
                Image.fromarray(tracking_rgb).resize((scaled_width, scaled_height), Image.Resampling.BILINEAR)
            )
        features = frame_features(feature_rgb)
        candidates: list[dict] = []
        if use_yunet:
            candidates = detect_facecam_candidates(tracking_rgb)
            features.update(facecam_features_from_candidates(candidates))
        content_type, confidence = classify_features(features)
        samples.append(FrameSample(
            time_ms=min(start_ms + duration_ms - 1, time_ms),
            content_type=content_type,
            confidence=confidence,
            subject_x=float(features.get("skin_x", 0.5)),
            features=features,
            facecam_candidates=candidates,
        ))
    if not samples:
        warnings.append("No frames could be sampled; composition will use a safe fallback.")
    return samples, warnings, frame_size


def _content_rect(edge_mask: np.ndarray) -> tuple[float, float, float, float]:
    """Normalised bounds of the region that actually contains detail.

    Dark margins and empty letterbox bars are ignored. The result is rejected
    when the remaining region is too small or barely smaller than the frame, so
    a scene with a small logo is not mistaken for a document.
    """
    height, width = edge_mask.shape
    if not edge_mask.any():
        return 0.0, 0.0, 1.0, 1.0
    columns = edge_mask.mean(axis=0)
    rows = edge_mask.mean(axis=1)
    xs = np.where(columns > 0.004)[0]
    ys = np.where(rows > 0.004)[0]
    if not len(xs) or not len(ys):
        return 0.0, 0.0, 1.0, 1.0

    pad_x = max(1, int(round(width * 0.02)))
    pad_y = max(1, int(round(height * 0.02)))
    x0 = max(0, int(xs[0]) - pad_x)
    x1 = min(width, int(xs[-1]) + 1 + pad_x)
    y0 = max(0, int(ys[0]) - pad_y)
    y1 = min(height, int(ys[-1]) + 1 + pad_y)
    content_w = (x1 - x0) / max(1, width)
    content_h = (y1 - y0) / max(1, height)
    area = content_w * content_h
    if area >= 0.92 or area < 0.45 or content_w < 0.50 or content_h < 0.40:
        return 0.0, 0.0, 1.0, 1.0
    return x0 / max(1, width), y0 / max(1, height), content_w, content_h


def frame_features(rgb: np.ndarray) -> dict:
    array = rgb.astype(np.float32) / 255.0
    if array.ndim != 3 or array.shape[2] != 3:
        return {}
    luma = 0.299 * array[:, :, 0] + 0.587 * array[:, :, 1] + 0.114 * array[:, :, 2]
    height, width = luma.shape
    grad_y, grad_x = np.gradient(luma)
    magnitude = np.sqrt(grad_x * grad_x + grad_y * grad_y)
    edge_density = float(np.mean(magnitude > 0.12))
    bright_fraction = float(np.mean(luma > 0.80))
    dark_fraction = float(np.mean(luma < 0.14))
    colorfulness = float(np.mean(np.std(array, axis=2)))

    border = int(max(2, min(height, width) * 0.14))
    border_mask = np.zeros_like(luma, dtype=bool)
    border_mask[:border, :] = True
    border_mask[-border:, :] = True
    border_mask[:, :border] = True
    border_mask[:, -border:] = True
    border_edge_density = float(np.mean(magnitude[border_mask] > 0.12))

    center = luma[int(height * 0.20): int(height * 0.80), int(width * 0.25): int(width * 0.75)]
    center_luma = float(np.mean(center)) if center.size else float(np.mean(luma))
    border_luma = float(np.mean(luma[border_mask])) if border_mask.any() else center_luma

    top = luma[: max(1, int(height * 0.12)), :]
    bottom = luma[-max(1, int(height * 0.12)):, :]
    top_dark = float(np.mean(top < 0.10))
    bottom_dark = float(np.mean(bottom < 0.10))

    content_x, content_y, content_w, content_h = _content_rect(magnitude > 0.12)

    red, green, blue = array[:, :, 0], array[:, :, 1], array[:, :, 2]
    skin = (red > 0.32) & (red > green * 1.04) & (green > blue * 0.72) & ((red - blue) > 0.035)
    skin_fraction = float(np.mean(skin))
    skin_x = 0.5
    if skin_fraction > 0.002:
        ys, xs = np.nonzero(skin)
        if len(xs):
            skin_x = float(np.median(xs) / max(1, width - 1))
    facecam = _facecam_features(skin)

    grid: dict[str, float] = {}
    for gy in range(3):
        for gx in range(3):
            cell = magnitude[
                int(height * gy / 3): int(height * (gy + 1) / 3),
                int(width * gx / 3): int(width * (gx + 1) / 3),
            ]
            grid[f"grid_edge_{gy * 3 + gx}"] = float(np.mean(cell > 0.12)) if cell.size else 0.0

    return {
        "mean_luma": float(np.mean(luma)),
        "std_luma": float(np.std(luma)),
        "edge_density": edge_density,
        "border_edge_density": border_edge_density,
        "bright_fraction": bright_fraction,
        "dark_fraction": dark_fraction,
        "colorfulness": colorfulness,
        "center_luma": center_luma,
        "border_luma": border_luma,
        "top_dark": top_dark,
        "bottom_dark": bottom_dark,
        "skin_fraction": skin_fraction,
        "skin_x": max(0.0, min(1.0, skin_x)),
        "letterboxed": 1.0 if top_dark > 0.82 and bottom_dark > 0.82 else 0.0,
        "text_at_edges": 1.0 if border_edge_density > 0.055 else 0.0,
        "content_x": content_x,
        "content_y": content_y,
        "content_width": content_w,
        "content_height": content_h,
        **facecam,
        **grid,
    }


def _facecam_features(skin: np.ndarray) -> dict:
    """Find a compact webcam plate from clustered skin tones.

    This is intentionally a cheap, explainable heuristic, not face recognition.
    It scans a 3x3 anchor grid so a cam that moves from one corner to another is
    still found, while preferring edge/corner cells over the centre where a
    full-screen speaker would otherwise look like an overlay. When the test
    fails the caller gets zeros and the renderer falls back with an explicit
    warning.
    """

    empty = {
        "facecam_present": 0.0,
        "facecam_x": 0.0,
        "facecam_y": 0.0,
        "facecam_width": 0.0,
        "facecam_height": 0.0,
        "facecam_confidence": 0.0,
        "facecam_skin_density": 0.0,
        "facecam_outside_skin": 0.0,
        "facecam_corner": 0.0,
    }
    if skin.ndim != 2 or skin.size == 0:
        return empty
    height, width = skin.shape
    if width < 40 or height < 30:
        return empty
    total_skin = int(skin.sum())
    if total_skin < 40 or total_skin / skin.size > 0.30:
        return empty

    cell_w = int(width * 0.34)
    cell_h = int(height * 0.34)
    best: dict | None = None
    for anchor_x in (0.0, 0.33, 0.66):
        for anchor_y in (0.0, 0.33, 0.66):
            x0 = min(width - cell_w, int(anchor_x * width))
            y0 = min(height - cell_h, int(anchor_y * height))
            x1 = min(width, x0 + cell_w)
            y1 = min(height, y0 + cell_h)
            region = skin[y0:y1, x0:x1]
            if region.size == 0:
                continue
            count = int(region.sum())
            density = float(region.mean())
            concentration = count / total_skin
            if count < 30 or density < 0.02 or concentration < 0.35:
                continue
            # A centred plate is possible, but it is also how a normal
            # full-screen speaker looks. Require more evidence there.
            corner = anchor_x in (0.0, 0.66) and anchor_y in (0.0, 0.66)
            edge = anchor_x in (0.0, 0.66) or anchor_y in (0.0, 0.66)
            preference = 1.0 if corner else 0.92 if edge else 0.78
            score = density * min(1.0, concentration / 0.45) * preference
            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "density": density,
                    "concentration": concentration,
                    "region": region,
                    "x0": x0,
                    "y0": y0,
                    "corner": corner,
                }

    if best is None:
        return empty

    region = best["region"]
    ys, xs = np.nonzero(region)
    pad_x = max(2, int(round(width * 0.03)))
    pad_y = max(2, int(round(height * 0.03)))
    bx0 = best["x0"] + int(xs.min()) - pad_x
    bx1 = best["x0"] + int(xs.max()) + 1 + pad_x
    by0 = best["y0"] + int(ys.min()) - pad_y
    by1 = best["y0"] + int(ys.max()) + 1 + pad_y

    # A tiny detected blob is usually a UI icon, not a webcam. Grow it to a
    # usable plate while keeping the same centre.
    box_w = max(int(round(width * 0.18)), bx1 - bx0)
    box_h = max(int(round(height * 0.18)), by1 - by0)
    cx = (bx0 + bx1) / 2
    cy = (by0 + by1) / 2
    box_w = min(box_w, width)
    box_h = min(box_h, height)
    bx0 = max(0, min(width - box_w, int(round(cx - box_w / 2))))
    by0 = max(0, min(height - box_h, int(round(cy - box_h / 2))))

    outside_skin = (total_skin - int(region.sum())) / max(1, skin.size - region.size)
    confidence = min(0.92, 0.40 + best["density"] * 2.5 + best["concentration"] * 0.30)
    if not best["corner"]:
        confidence *= 0.92
    return {
        "facecam_present": 1.0,
        "facecam_x": bx0 / width,
        "facecam_y": by0 / height,
        "facecam_width": box_w / width,
        "facecam_height": box_h / height,
        "facecam_confidence": confidence,
        "facecam_skin_density": best["density"],
        "facecam_outside_skin": float(outside_skin),
        "facecam_corner": 1.0 if best["corner"] else 0.0,
    }


def classify_features(features: dict) -> tuple[str, float]:
    if not features:
        return "unknown", 0.30
    edge = float(features.get("edge_density", 0.0))
    bright = float(features.get("bright_fraction", 0.0))
    dark = float(features.get("dark_fraction", 0.0))
    colour = float(features.get("colorfulness", 0.0))
    center = float(features.get("center_luma", 0.0))
    border = float(features.get("border_luma", 0.0))
    skin = float(features.get("skin_fraction", 0.0))
    letterboxed = bool(features.get("letterboxed", 0.0))
    text_edges = bool(features.get("text_at_edges", 0.0))

    if (bright > 0.16 and edge > 0.032) or (text_edges and bright > 0.10):
        confidence = min(0.96, 0.50 + min(0.18, edge * 1.4) + min(0.18, bright * 0.5))
        if letterboxed:
            confidence = max(0.55, confidence - 0.05)
        return "slide", round(confidence, 3)
    # Flat, bright graphics (charts, slides) often have very few edges because
    # their blocks are solid colour. A bright outdoor shot with a clear subject
    # has more edge detail, so require both flatness and high tonal spread.
    if (
        bright > 0.30
        and edge < 0.030
        and colour > 0.060
        and float(features.get("std_luma", 0.0)) > 0.180
        and float(features.get("border_edge_density", 0.0)) < 0.020
    ):
        return "slide", 0.68
    # Dark documents and terminal captures have a low bright fraction, but
    # their text creates substantially more edges than a dark room or face.
    if (
        dark > 0.30
        and bright > 0.003
        and edge > 0.040
        and float(features.get("border_edge_density", 0.0)) < 0.020
    ):
        return "slide", 0.62
    if edge > 0.095 and bright > 0.05 and colour < 0.24:
        confidence = min(0.92, 0.45 + min(0.25, edge) + min(0.15, bright * 0.4))
        return "screen_recording", round(confidence, 3)
    if skin > 0.010 or (edge < 0.060 and center > border * 1.035 and dark > 0.08):
        confidence = min(0.94, 0.48 + min(0.30, skin * 8.0) + (0.10 if center > border else 0.0))
        return "talking_head", round(confidence, 3)
    if text_edges and edge > 0.045:
        return "mixed", 0.52
    if edge > 0.060 and bright > 0.04:
        return "mixed", 0.46
    return "unknown", 0.35


def _aggregate(
    samples: list[FrameSample],
    start_ms: int,
    end_ms: int,
    *,
    analysis_height: int = DEFAULT_ANALYSIS_HEIGHT,
) -> Scene:
    inside = [sample for sample in samples if start_ms <= sample.time_ms < end_ms]
    if not inside:
        return Scene(start_ms=start_ms, end_ms=end_ms, content_type="unknown", confidence=0.3,
                     subject_x=0.5, warnings=["No samples in this scene."])
    weights: dict[str, float] = {}
    for sample in inside:
        weights[sample.content_type] = weights.get(sample.content_type, 0.0) + max(0.2, sample.confidence)
    content_type = max(weights, key=weights.get)
    matching = [sample for sample in inside if sample.content_type == content_type]
    confidence = sum(sample.confidence for sample in matching) / max(1, len(matching))
    confidence *= min(1.0, 0.65 + 0.35 * len(matching) / len(inside))
    talking = [sample.subject_x for sample in inside if sample.content_type == "talking_head"]
    subject_x = float(np.median(talking)) if talking else 0.5

    features = {}
    keys = ("edge_density", "border_edge_density", "bright_fraction", "dark_fraction",
            "colorfulness", "std_luma", "letterboxed", "text_at_edges", "skin_fraction",
            "content_x", "content_y", "content_width", "content_height",
            "facecam_present", "facecam_x", "facecam_y", "facecam_width",
            "facecam_height", "facecam_confidence", "facecam_skin_density",
            "facecam_outside_skin", "facecam_corner", "facecam_face_score",
            "facecam_panel_area", "facecam_candidate_count",
            "grid_edge_0", "grid_edge_1", "grid_edge_2",
            "grid_edge_3", "grid_edge_4", "grid_edge_5",
            "grid_edge_6", "grid_edge_7", "grid_edge_8")
    for key in keys:
        features[key] = float(np.mean([sample.features.get(key, 0.0) for sample in inside]))
    if content_type == "unknown":
        aggregate_type, aggregate_confidence = classify_features(features)
        if aggregate_type != "unknown":
            content_type = aggregate_type
            confidence = max(confidence, aggregate_confidence * 0.85)
    warnings: list[str] = []
    if content_type == "unknown":
        warnings.append("The scene type could not be identified confidently.")
    if features.get("letterboxed", 0.0) > 0.5:
        warnings.append("The source already contains black bars; containing it avoids double-cropping.")
    if features.get("text_at_edges", 0.0) > 0.5:
        warnings.append("Information reaches the left/right edges; containing it protects the text.")
    if analysis_height < TEXT_READABLE_HEIGHT and (
        content_type in {"slide", "screen_recording"} or features.get("text_at_edges", 0.0) > 0.5
    ):
        warnings.append(
            f"This scene looks text-heavy but was sampled at {analysis_height}p. "
            "Small text cannot be read at that resolution; analyse the scene at a higher "
            "resolution before relying on it."
        )
    return Scene(
        start_ms=start_ms,
        end_ms=end_ms,
        content_type=content_type,
        confidence=round(max(0.25, min(0.97, confidence)), 3),
        subject_x=max(0.30, min(0.70, subject_x)),
        features=features,
        warnings=warnings,
    )


def _boundaries_from_samples(samples: list[FrameSample], start_ms: int, end_ms: int) -> list[int]:
    ordered = sorted((sample for sample in samples if start_ms <= sample.time_ms < end_ms), key=lambda item: item.time_ms)
    boundaries: list[int] = []
    for index in range(2, len(ordered) - 2):
        before = [item.content_type for item in ordered[index - 2:index]]
        after = [item.content_type for item in ordered[index:index + 2]]
        if len(set(before)) == 1 and len(set(after)) == 1 and before[0] != after[0]:
            if after[0] != "unknown" and before[0] != "unknown":
                boundaries.append(ordered[index].time_ms)
    return boundaries


def _merge_samples(coarse: list[FrameSample], dense: list[FrameSample]) -> list[FrameSample]:
    """Merge coarse and adaptive samples without duplicating a timestamp."""
    by_time: dict[int, FrameSample] = {sample.time_ms: sample for sample in coarse}
    for sample in dense:
        # Dense samples carry the better evidence for their moment.
        by_time[sample.time_ms] = sample
    return sorted(by_time.values(), key=lambda item: item.time_ms)


def analyse_source_window(
    path: Path,
    start_ms: int,
    end_ms: int,
    *,
    cache_dir: Path | None = None,
    source_duration_ms: int | None = None,
    scene_fps: float | None = None,
    analysis_height: int = DEFAULT_ANALYSIS_HEIGHT,
    source_height: int | None = None,
    track_facecam: bool = False,
    face_cam_overrides: list[dict] | None = None,
) -> SceneAnalysis:
    """Analyse one source window, caching the deterministic result on disk.

    ``analysis_height`` is the height frames are sampled at. It is recorded in
    the result so the caller can tell an evidence-backed composition decision
    from one taken on a frame too small to read text. Callers that hold a
    low-resolution proxy pass its height here; the source's real height is kept
    in ``source_height`` so the difference stays visible.

    When ``track_facecam`` is set, the coarse sample is followed by a short,
    denser look around every suspected face-cam change. The extra frames are
    bounded, so a two-hour stream does not become a frame-by-frame scan.
    """
    start_ms = max(0, int(start_ms))
    end_ms = max(start_ms + 500, int(end_ms))
    if source_duration_ms:
        end_ms = min(end_ms, int(source_duration_ms))
    duration_ms = max(500, end_ms - start_ms)
    height = max(72, min(2160, int(analysis_height)))
    desired_fps = float(scene_fps) if scene_fps and scene_fps > 0 else (
        1.0 if duration_ms <= 300_000 else 0.5
    )
    fps = sampling_fps(duration_ms, desired_fps)
    facecam_height = max(height, FACECAM_ANALYSIS_HEIGHT) if track_facecam else 0

    key = _cache_key(
        path, start_ms, end_ms, fps, height,
        track=track_facecam,
        facecam_height=facecam_height,
        overrides=face_cam_overrides,
    )
    cache_file = (cache_dir / f"{key}.json") if cache_dir else None
    if cache_file and cache_file.exists():
        try:
            return SceneAnalysis.from_dict(json.loads(cache_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            logger.debug("Ignoring unreadable scene cache %s", cache_file)

    cuts, cut_warnings = _detect_cuts(path, start_ms, duration_ms)
    frames_dir = (cache_dir / f"frames-{key}") if cache_dir else None
    if frames_dir is None:
        import tempfile

        frames_dir = Path(tempfile.mkdtemp(prefix="clipforge-scenes-"))
    samples, sample_warnings, frame_size = _extract_samples(
        path, start_ms, duration_ms, frames_dir,
        fps=fps,
        analysis_height=height,
        facecam_height=facecam_height or None,
    )
    coarse_samples = list(samples)
    dense_windows: list[tuple[int, int]] = []
    if track_facecam and samples:
        dense_fps = min(4.0, max(2.0, fps * 4.0))
        dense_windows = [
            (max(start_ms, left), min(end_ms, right))
            for left, right in change_windows(raw_observations(samples))
            if min(end_ms, right) > max(start_ms, left)
        ]
        dense_samples: list[FrameSample] = []
        for index, (left, right) in enumerate(dense_windows):
            dense_dir = frames_dir.parent / f"dense-{key}-{index}"
            extra, extra_warnings, _ = _extract_samples(
                path, left, right - left, dense_dir,
                fps=dense_fps,
                analysis_height=height,
                facecam_height=facecam_height or None,
            )
            dense_samples.extend(extra)
            sample_warnings.extend(extra_warnings)
            shutil.rmtree(dense_dir, ignore_errors=True)
        if dense_samples:
            samples = _merge_samples(samples, dense_samples)

    raw_boundaries = [start_ms, *cuts, *_boundaries_from_samples(coarse_samples, start_ms, end_ms), end_ms]
    raw_boundaries = sorted({value for value in raw_boundaries if start_ms <= value <= end_ms})
    filtered: list[int] = [raw_boundaries[0]]
    for boundary in raw_boundaries[1:]:
        if boundary - filtered[-1] >= 800 or boundary == end_ms:
            filtered.append(boundary)
    if filtered[-1] != end_ms:
        filtered.append(end_ms)

    scenes = [
        _aggregate(samples, filtered[index], filtered[index + 1], analysis_height=height)
        for index in range(len(filtered) - 1)
        if filtered[index + 1] > filtered[index]
    ]
    # Small flashes and one-frame cuts are not useful layout changes. Fold them
    # into the longer neighbour instead of producing a two-frame composition.
    changed = True
    while changed and len(scenes) > 1:
        changed = False
        for index, scene in enumerate(scenes):
            if scene.duration_ms >= MIN_SCENE_MS:
                continue
            left = scenes[index - 1] if index > 0 else None
            right = scenes[index + 1] if index + 1 < len(scenes) else None
            target = left if left and (not right or left.duration_ms >= right.duration_ms) else right
            if target is None:
                continue
            target.start_ms = min(target.start_ms, scene.start_ms)
            target.end_ms = max(target.end_ms, scene.end_ms)
            scenes.pop(index)
            changed = True
            break

    warnings = list(dict.fromkeys(cut_warnings + sample_warnings))
    facecam_track = None
    if track_facecam:
        if not detector_available():
            detail = detector_error() or "the ONNX face detector is unavailable"
            warnings.append(
                f"Webcam-panel detection is unavailable ({detail}); the older skin-tone fallback was used. "
                "Review the face-cam region before export."
            )
        facecam_track = build_facecam_track(
            samples,
            start_ms,
            end_ms,
            overrides=face_cam_overrides or [],
            sample_fps=fps,
        )
        warnings.extend(facecam_track.warnings)
        if (
            facecam_track.ranges
            and all(item.state == "missing" for item in facecam_track.ranges)
            and any(float(sample.features.get("facecam_face_score", 0.0)) >= 0.5 for sample in samples)
        ):
            warnings.append(
                "Faces were detected, but no bordered webcam panel was reliable. "
                "This can be a full-screen camera or a borderless chroma-key cutout; "
                "neither is tracked as a separate stack panel automatically. Lock a webcam region if needed."
            )
        warnings = list(dict.fromkeys(warnings))
    text_heavy = any(
        scene.content_type in {"slide", "screen_recording"} for scene in scenes
    ) or any(scene.features.get("text_at_edges", 0.0) > 0.5 for scene in scenes)
    if text_heavy and height < TEXT_READABLE_HEIGHT:
        warnings.append(
            f"Text-heavy scenes were sampled at {height}p; small text may be unreadable at that size. "
            "A higher-resolution download is required to verify it."
        )
    sampled_width, sampled_height = frame_size or (None, height)
    analysis = SceneAnalysis(
        scenes=scenes,
        samples=samples,
        cuts=cuts,
        warnings=warnings,
        analysis={
            "sampled_width": sampled_width,
            "sampled_height": sampled_height,
            "scene_fps": round(fps, 3),
            "source_height": source_height,
            "evidence": "detail" if height >= TEXT_READABLE_HEIGHT else "low_resolution",
            "text_verified": height >= TEXT_READABLE_HEIGHT,
            "text_heavy": text_heavy,
            "facecam_dense_windows": len(dense_windows),
            "facecam_track_ranges": len(facecam_track.ranges) if facecam_track else 0,
            "facecam_detector": FACECAM_DETECTOR_VERSION if track_facecam else None,
            "facecam_detector_available": detector_available() if track_facecam else None,
            "facecam_analysis_height": facecam_height or None,
            "feature_height": height,
        },
        facecam_track=facecam_track,
    )
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(".partial")
        temporary.write_text(json.dumps(analysis.to_dict(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache_file)
    if frames_dir.name.startswith("frames-"):
        shutil.rmtree(frames_dir, ignore_errors=True)
    return analysis
