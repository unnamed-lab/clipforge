"""Deterministic, CPU-only scene analysis for composition decisions.

This is deliberately not a vision model. It detects cuts with FFmpeg's scene
score, samples the window, and classifies each frame from cheap, explainable
features: luminance, edge density, colour spread, letterboxing and a skin-tone
centroid. The result is a scene list good enough to choose crop versus fit and
to warn about text near the edges. OCR, face detection and saliency models can
replace individual functions later; the composition contract stays the same.
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

from clipforge.media.process import ProcessFailed, require, run

logger = logging.getLogger(__name__)

SCENE_THRESHOLD = 0.28
MIN_SCENE_MS = 1_200
MAX_SAMPLES = 600
CACHE_VERSION = 1
_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


@dataclass
class FrameSample:
    time_ms: int
    content_type: str
    confidence: float
    subject_x: float
    features: dict

    def to_dict(self) -> dict:
        return {
            "time_ms": self.time_ms,
            "content_type": self.content_type,
            "confidence": round(self.confidence, 3),
            "subject_x": round(self.subject_x, 3),
            "features": {key: round(float(value), 4) for key, value in self.features.items()},
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

    def to_dict(self) -> dict:
        return {
            "version": CACHE_VERSION,
            "scenes": [scene.to_dict() for scene in self.scenes],
            "samples": [sample.to_dict() for sample in self.samples],
            "cuts": self.cuts,
            "warnings": self.warnings,
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
            )
            for item in payload.get("samples") or []
        ]
        return cls(scenes=scenes, samples=samples, cuts=[int(value) for value in payload.get("cuts") or []],
                   warnings=list(payload.get("warnings") or []))


def _cache_key(path: Path, start_ms: int, end_ms: int) -> str:
    try:
        stat = path.stat()
        identity = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        identity = str(path)
    raw = f"{CACHE_VERSION}|{identity}|{start_ms}|{end_ms}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


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


def _extract_samples(path: Path, start_ms: int, duration_ms: int, frames_dir: Path) -> tuple[list[FrameSample], list[str]]:
    warnings: list[str] = []
    frames_dir.mkdir(parents=True, exist_ok=True)
    for child in frames_dir.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)
    fps = 1.0 if duration_ms <= 300_000 else 0.5
    max_frames = min(MAX_SAMPLES, max(1, int(duration_ms / 1000 * fps) + 1))
    try:
        ffmpeg = require("ffmpeg")
        result = run(
            [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start_ms / 1000:.3f}", "-t", f"{max(0.2, duration_ms / 1000):.3f}",
                "-i", str(path), "-an",
                "-vf", f"fps={fps},scale=256:144",
                "-frames:v", str(max_frames), "-q:v", "4",
                str(frames_dir / "%05d.jpg"),
            ],
            timeout_seconds=180,
        )
    except (ProcessFailed, OSError) as exc:
        return [], [f"Frame sampling failed; composition will use a safe fallback: {exc}"]
    if result.returncode != 0:
        return [], ["Frame sampling failed; composition will use a safe fallback."]

    samples: list[FrameSample] = []
    for index, frame in enumerate(sorted(frames_dir.glob("*.jpg")), start=0):
        try:
            with Image.open(frame) as image:
                rgb = np.asarray(image.convert("RGB"))
        except OSError:
            continue
        time_ms = start_ms + int(round(index * 1000 / fps))
        features = frame_features(rgb)
        content_type, confidence = classify_features(features)
        samples.append(FrameSample(
            time_ms=min(start_ms + duration_ms - 1, time_ms),
            content_type=content_type,
            confidence=confidence,
            subject_x=float(features.get("skin_x", 0.5)),
            features=features,
        ))
    if not samples:
        warnings.append("No frames could be sampled; composition will use a safe fallback.")
    return samples, warnings


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

    red, green, blue = array[:, :, 0], array[:, :, 1], array[:, :, 2]
    skin = (red > 0.32) & (red > green * 1.04) & (green > blue * 0.72) & ((red - blue) > 0.035)
    skin_fraction = float(np.mean(skin))
    skin_x = 0.5
    if skin_fraction > 0.002:
        ys, xs = np.nonzero(skin)
        if len(xs):
            skin_x = float(np.median(xs) / max(1, width - 1))

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


def _aggregate(samples: list[FrameSample], start_ms: int, end_ms: int) -> Scene:
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
            "colorfulness", "letterboxed", "text_at_edges", "skin_fraction")
    for key in keys:
        features[key] = float(np.mean([sample.features.get(key, 0.0) for sample in inside]))
    warnings: list[str] = []
    if content_type == "unknown":
        warnings.append("The scene type could not be identified confidently.")
    if features.get("letterboxed", 0.0) > 0.5:
        warnings.append("The source already contains black bars; containing it avoids double-cropping.")
    if features.get("text_at_edges", 0.0) > 0.5:
        warnings.append("Information reaches the left/right edges; containing it protects the text.")
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


def analyse_source_window(
    path: Path,
    start_ms: int,
    end_ms: int,
    *,
    cache_dir: Path | None = None,
    source_duration_ms: int | None = None,
) -> SceneAnalysis:
    """Analyse one source window, caching the deterministic result on disk."""
    start_ms = max(0, int(start_ms))
    end_ms = max(start_ms + 500, int(end_ms))
    if source_duration_ms:
        end_ms = min(end_ms, int(source_duration_ms))
    duration_ms = max(500, end_ms - start_ms)

    key = _cache_key(path, start_ms, end_ms)
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
    samples, sample_warnings = _extract_samples(path, start_ms, duration_ms, frames_dir)

    raw_boundaries = [start_ms, *cuts, *_boundaries_from_samples(samples, start_ms, end_ms), end_ms]
    raw_boundaries = sorted({value for value in raw_boundaries if start_ms <= value <= end_ms})
    filtered: list[int] = [raw_boundaries[0]]
    for boundary in raw_boundaries[1:]:
        if boundary - filtered[-1] >= 800 or boundary == end_ms:
            filtered.append(boundary)
    if filtered[-1] != end_ms:
        filtered.append(end_ms)

    scenes = [
        _aggregate(samples, filtered[index], filtered[index + 1])
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
    analysis = SceneAnalysis(scenes=scenes, samples=samples, cuts=cuts, warnings=warnings)
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(".partial")
        temporary.write_text(json.dumps(analysis.to_dict(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache_file)
    if frames_dir.name.startswith("frames-"):
        shutil.rmtree(frames_dir, ignore_errors=True)
    return analysis
