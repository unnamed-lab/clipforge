"""Enhancement orchestration inside the existing render job.

Pipeline order is deliberate:

    source -> probe -> optional denoise -> AI upscale -> resize to an
    intermediate that composition can crop -> encode video-only intermediate
    -> composition -> captions/logo/audio -> validate

Enhancement runs *before* composition because composition is a geometric plan
in normalised source coordinates. Scaling the whole frame keeps every crop,
face-cam rectangle and caption band valid, and avoids re-running transcription,
moment discovery or face-cam tracking when only the enhancement strength
changes. Audio is never written into the intermediate; the final renderer reads
it from the original source, so synchronisation cannot drift through a second
encode.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from clipforge.config import Settings
from clipforge.enhancement.engines import (
    EnhancementEngine,
    ModelUnavailable,
    engine_for,
    load_benchmark,
    record_benchmark,
    resize_to,
)
from clipforge.enhancement.frames import FrameReader, FrameWriter, FrameStreamError
from clipforge.enhancement.settings import (
    ENHANCEMENT_PROFILES,
    ModelSpec,
    cache_key,
    denoise_filter,
    intermediate_dimensions,
    resolve_model,
)
from clipforge.errors import JobFailed
from clipforge.media.probe import probe
from clipforge.rendering.spec import RenderSpec
from clipforge.storage.workspace import ensure_space

logger = logging.getLogger(__name__)

INTERMEDIATE_CRF = 12
INTERMEDIATE_PRESET = "veryfast"
FAST_INTERMEDIATE_CRF = 16
FAST_INTERMEDIATE_PRESET = "ultrafast"
ENCODER_VERSION = "x264-intermediate-v2"
CACHE_LIMIT_BYTES = 2 * 1024 ** 3
CACHE_DIR_NAME = "enhancement"
MIN_CACHE_BYTES = 4096

Progress = Callable[[float, str], None]


@dataclass
class EnhancementPlan:
    profile: str
    uses_ai: bool
    target: tuple[int, int]
    source: tuple[int, int]
    model: ModelSpec | None
    intermediate: tuple[int, int] | None
    denoise: int
    restoration: float
    temporal: bool
    engine_label: str
    intermediate_crf: int = INTERMEDIATE_CRF
    intermediate_preset: str = INTERMEDIATE_PRESET
    warnings: list[str] = field(default_factory=list)
    fallback_reason: str | None = None

    @property
    def effective_scale(self) -> float:
        if self.intermediate is None:
            return max(
                self.target[0] / max(1, self.source[0]),
                self.target[1] / max(1, self.source[1]),
            )
        return max(
            self.intermediate[0] / max(1, self.source[0]),
            self.intermediate[1] / max(1, self.source[1]),
        )

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "uses_ai": self.uses_ai,
            "source": list(self.source),
            "target": list(self.target),
            "intermediate": list(self.intermediate) if self.intermediate else None,
            "model": self.model.id if self.model else None,
            "model_version": self.model.version if self.model else None,
            "engine": self.engine_label,
            "scale": round(self.effective_scale, 3),
            "denoise": self.denoise,
            "restoration": self.restoration,
            "temporal": self.temporal,
            "intermediate_crf": self.intermediate_crf,
            "intermediate_preset": self.intermediate_preset,
            "warnings": list(self.warnings),
            "fallback_reason": self.fallback_reason,
        }


@dataclass
class EnhancementResult:
    video_inputs: list[Path | None]
    report: dict
    warnings: list[str] = field(default_factory=list)
    frames: int = 0
    seconds: float = 0.0
    cached_segments: int = 0

    @property
    def applied_ai(self) -> bool:
        return bool(self.report.get("applied_model"))


def resolve_plan(
    spec: RenderSpec,
    *,
    source_width: int,
    source_height: int,
) -> EnhancementPlan:
    settings = spec.enhancement
    profile = settings.profile
    meta = ENHANCEMENT_PROFILES[profile]
    target = spec.output_dimensions
    warnings = list(meta["warnings"])
    denoise = int(settings.denoise)
    if profile == "clean" and denoise == 0:
        denoise = 30
    model: ModelSpec | None = None
    intermediate: tuple[int, int] | None = None
    engine_label = "FFmpeg lanczos"
    intermediate_crf = INTERMEDIATE_CRF
    intermediate_preset = INTERMEDIATE_PRESET
    if settings.uses_ai:
        model = resolve_model(
            settings,
            source_width=source_width,
            source_height=source_height,
            target_width=target[0],
            target_height=target[1],
        )
        fast = settings.profile == "ai_fast"
        intermediate = intermediate_dimensions(
            source_width=source_width,
            source_height=source_height,
            target_width=target[0],
            target_height=target[1],
            model=model,
            native=fast,
        )
        if fast:
            intermediate_crf = FAST_INTERMEDIATE_CRF
            intermediate_preset = FAST_INTERMEDIATE_PRESET
            if intermediate[0] < target[0] or intermediate[1] < target[1]:
                warnings.append(
                    f"Fast AI writes a {intermediate[0]}×{intermediate[1]} intermediate. "
                    f"Composition scales it to {target[0]}×{target[1]}, so fine detail may look "
                    "softer than AI Enhance. This is the time-saving trade-off."
                )
        engine_label = (
            f"{model.label} + fast lanczos"
            if fast
            else f"{model.label} + lanczos"
        )
        effective = max(
            intermediate[0] / max(1, source_width),
            intermediate[1] / max(1, source_height),
        )
        if effective < 1.05 and target[0] <= source_width and target[1] <= source_height:
            warnings.append(
                "The source already covers the target resolution. AI is running as restoration, "
                "not as an upscale."
            )
    return EnhancementPlan(
        profile=profile,
        uses_ai=settings.uses_ai,
        target=target,
        source=(source_width, source_height),
        model=model,
        intermediate=intermediate,
        denoise=denoise,
        restoration=float(settings.restoration),
        temporal=bool(settings.temporal_stabilize),
        engine_label=engine_label,
        intermediate_crf=intermediate_crf,
        intermediate_preset=intermediate_preset,
        warnings=warnings,
    )


def plan_is_available(plan: EnhancementPlan, settings: Settings) -> tuple[bool, str | None]:
    if not plan.uses_ai or plan.model is None:
        return True, None
    try:
        engine_for(plan.model, models_dir=settings.models_dir)
    except ModelUnavailable as exc:
        return False, str(exc)
    return True, None


def prepare_enhancement(
    spec: RenderSpec,
    *,
    source: dict,
    source_path: Path,
    settings: Settings,
    project_id: str,
    on_progress: Progress | None = None,
    cancel=None,
) -> EnhancementResult:
    source_width = int(source.get("width") or 0)
    source_height = int(source.get("height") or 0)
    if not source_width or not source_height:
        raise JobFailed(
            "enhancement_source_unknown",
            "The source resolution could not be read, so enhancement cannot be planned.",
        )
    plan = resolve_plan(spec, source_width=source_width, source_height=source_height)

    if not plan.uses_ai or plan.model is None:
        report = {
            **plan.to_dict(),
            "applied": plan.profile,
            "applied_model": None,
            "frames": 0,
            "seconds": 0.0,
            "cached_segments": 0,
        }
        return EnhancementResult(video_inputs=[None] * len(spec.segments), report=report)

    try:
        engine = engine_for(plan.model, models_dir=settings.models_dir)
    except ModelUnavailable as exc:
        if spec.enhancement.allow_fallback:
            message = (
                f"{exc} The clip was rendered with high-quality resizing"
                f"{' and denoising' if plan.denoise else ''} instead; no AI model ran."
            )
            report = {
                **plan.to_dict(),
                "applied": "conventional_fallback",
                "applied_model": None,
                "fallback_reason": str(exc),
                "frames": 0,
                "seconds": 0.0,
                "cached_segments": 0,
            }
            return EnhancementResult(
                video_inputs=[None] * len(spec.segments),
                report=report,
                warnings=[message],
            )
        raise JobFailed("enhancement_model_unavailable", str(exc)) from exc

    fps = _output_fps(spec, source.get("fps"))
    if fps <= 0:
        fps = 30.0
    cache_dir = settings.data_dir / "cache" / "projects" / project_id / CACHE_DIR_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    _remove_partials(cache_dir)

    frame_counts = [max(1, int(round(segment.duration_ms / 1000 * fps))) for segment in spec.segments]
    total_frames = sum(frame_counts)
    done_frames = 0
    started = time.monotonic()
    inputs: list[Path | None] = []
    cached_segments = 0
    benchmark_recorded = load_benchmark(settings.models_dir, plan.model.id) is not None

    for index, segment in enumerate(spec.segments):
        key = cache_key(
            source_identity=str(source.get("content_hash") or source.get("relative_path") or source["id"]),
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            settings=spec.enhancement,
            model=plan.model,
            intermediate=plan.intermediate or (source_width, source_height),
            fps=fps,
            encoder_version=(
                f"{ENCODER_VERSION}-crf{plan.intermediate_crf}-{plan.intermediate_preset}"
            ),
        )
        final = cache_dir / f"{key}.mp4"
        cached = _cached_segment(final, plan, segment, fps)
        if cached is None:
            required = (
                _estimate_intermediate_bytes(
                    plan, duration_ms=segment.duration_ms, fps=fps
                )
                + 32 * 1024 * 1024
            )
            ensure_space(settings, cache_dir, required_bytes=required)
            if on_progress:
                on_progress(
                    done_frames / max(1, total_frames),
                    f"Enhancing segment {index + 1} of {len(spec.segments)} with {plan.model.label}",
                )
            segment_started = time.monotonic()
            _enhance_segment(
                source_path=source_path,
                destination=final,
                plan=plan,
                engine=engine,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                fps=fps,
                expected_frames=frame_counts[index],
                cancel=cancel,
                on_frame=lambda count, i=index: _frame_progress(
                    on_progress, done_frames + count, total_frames, i, len(spec.segments), plan
                ),
            )
            if not benchmark_recorded and frame_counts[index] > 0:
                # The first real pass is a better measurement than a synthetic
                # frame: it includes denoise, blend, resize and streaming I/O.
                record_benchmark(
                    settings.models_dir,
                    model=plan.model,
                    device=engine.device,
                    seconds=(time.monotonic() - segment_started) / frame_counts[index],
                    input_pixels=source_width * source_height,
                )
                benchmark_recorded = True
        else:
            cached_segments += 1
        inputs.append(final)
        done_frames += frame_counts[index]
        if on_progress:
            on_progress(
                done_frames / max(1, total_frames),
                f"Enhanced segment {index + 1} of {len(spec.segments)}",
            )

    _prune_cache(cache_dir)
    elapsed = time.monotonic() - started
    report = {
        **plan.to_dict(),
        "applied": plan.profile,
        "applied_model": plan.model.id,
        "device": engine.device,
        "frames": total_frames,
        "seconds": round(elapsed, 1),
        "cached_segments": cached_segments,
    }
    return EnhancementResult(
        video_inputs=inputs,
        report=report,
        frames=total_frames,
        seconds=elapsed,
        cached_segments=cached_segments,
    )


def _frame_progress(
    callback: Progress | None,
    done: int,
    total: int,
    index: int,
    count: int,
    plan: EnhancementPlan,
) -> None:
    if callback is None:
        return
    if done % 5 and done != total:
        return
    callback(
        done / max(1, total),
        f"Enhancing frame {done} of {total} "
        f"(segment {index + 1} of {count}, {plan.model.label if plan.model else plan.engine_label})",
    )


def _output_fps(spec: RenderSpec, source_fps: float | None) -> float:
    """The frame rate the final composition will actually encode.

    Enhancing every source frame when the export drops half of them doubles
    both processing time and intermediate size. A 60 fps source rendered at
    30 fps must be sampled at 30 fps here, exactly as the final ``fps`` filter
    does. If the user keeps the source rate, this is that rate.
    """
    fps = float(spec.video.fps or source_fps or 30.0)
    if spec.encode.max_fps:
        fps = min(fps, float(spec.encode.max_fps))
    return max(15.0, min(60.0, fps))


def apply_frame(frame: np.ndarray, plan: EnhancementPlan, engine: EnhancementEngine) -> np.ndarray:
    """One frame through the same steps the streaming pass uses.

    The comparison preview calls this directly, which is how preview and export
    cannot drift apart when only the model or strength changes.
    """
    assert plan.intermediate is not None
    width, height = plan.intermediate
    ai = engine.upscale(frame)
    ai = resize_to(ai, width, height)
    if plan.restoration < 0.999:
        base = resize_to(frame, width, height)
        ai = _blend(ai, base, plan.restoration)
    return ai


def _blend(ai: np.ndarray, original: np.ndarray, strength: float) -> np.ndarray:
    strength = max(0.0, min(1.0, strength))
    if strength >= 0.999:
        return ai
    if strength <= 0.001:
        return original
    mixed = ai.astype(np.float32) * strength + original.astype(np.float32) * (1.0 - strength)
    return np.clip(mixed, 0, 255).round().astype(np.uint8)


def _enhance_segment(
    *,
    source_path: Path,
    destination: Path,
    plan: EnhancementPlan,
    engine: EnhancementEngine,
    start_ms: int,
    end_ms: int,
    fps: float,
    expected_frames: int,
    cancel,
    on_frame: Callable[[int], None] | None,
) -> None:
    assert plan.intermediate is not None
    width, height = plan.intermediate
    partial = destination.with_suffix(".part.mp4")
    partial.unlink(missing_ok=True)
    filter_text = denoise_filter(plan.denoise)
    last_error: Exception | None = None
    for attempt in range(1, 3):
        if cancel is not None and cancel.is_set():
            raise JobFailed("cancelled", "Cancelled")
        writer: FrameWriter | None = None
        reader: FrameReader | None = None
        count = 0
        try:
            reader = FrameReader(
                source_path,
                start_ms=start_ms,
                end_ms=end_ms,
                width=int(plan.source[0]),
                height=int(plan.source[1]),
                fps=fps,
                cancel=cancel,
                denoise_filter=filter_text,
            )
            writer = FrameWriter(
                partial,
                width=width,
                height=height,
                fps=fps,
                crf=plan.intermediate_crf,
                preset=plan.intermediate_preset,
            )
            previous_small: np.ndarray | None = None
            previous_ai: np.ndarray | None = None
            for frame in reader:
                if cancel is not None and cancel.is_set():
                    raise JobFailed("cancelled", "Cancelled")
                enhanced = apply_frame(frame, plan, engine)
                if plan.temporal:
                    small = _small_luma(frame)
                    if previous_small is not None and previous_ai is not None:
                        difference = float(np.abs(small - previous_small).mean())
                        if difference < 1.5:
                            enhanced = _blend(enhanced, previous_ai, 0.78)
                        elif difference > 18.0:
                            previous_ai = None
                    previous_small = small
                    previous_ai = enhanced
                writer.write(enhanced)
                count += 1
                if on_frame is not None:
                    on_frame(count)
            writer.close()
            writer = None
            reader.close()
            reader = None
            if count == 0:
                raise FrameStreamError("The source produced no frames for this segment.")
            if abs(count - expected_frames) > 1:
                raise FrameStreamError(
                    f"Enhanced {count} frames but expected about {expected_frames}; "
                    "timing would not be preserved."
                )
            info = probe(partial)
            if (info.width, info.height) != (width, height):
                raise FrameStreamError(
                    f"The intermediate is {info.width}x{info.height}; expected {width}x{height}."
                )
            frame_ms = 1000 / max(1.0, fps)
            if abs(info.duration_ms - (end_ms - start_ms)) > max(120, frame_ms * 1.5):
                raise FrameStreamError(
                    f"The intermediate is {info.duration_ms} ms; expected {end_ms - start_ms} ms."
                )
            if not info.has_video or info.has_audio:
                raise FrameStreamError("The intermediate must contain video only.")
            partial.replace(destination)
            _write_manifest(destination, plan, start_ms, end_ms, width, height, fps, count)
            return
        except JobFailed:
            if writer is not None:
                writer.abort()
            if reader is not None:
                reader.close()
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            last_error = exc
            if writer is not None:
                writer.abort()
            if reader is not None:
                reader.close()
            partial.unlink(missing_ok=True)
            logger.warning(
                "Enhancement attempt %s of 2 failed for %s-%s: %s",
                attempt,
                start_ms,
                end_ms,
                exc,
            )
            if cancel is not None and cancel.is_set():
                raise JobFailed("cancelled", "Cancelled") from exc
    message = (
        "The AI enhancement pass failed after two attempts. The original source is untouched. "
        f"Last error: {last_error}"
    )
    raise JobFailed("enhancement_failed", message) from last_error


def _cached_segment(
    path: Path,
    plan: EnhancementPlan,
    segment,
    fps: float,
) -> Path | None:
    if not path.is_file() or path.stat().st_size < MIN_CACHE_BYTES:
        return None
    manifest_path = path.with_suffix(".json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    expected = {
        "model": plan.model.id if plan.model else None,
        "model_version": plan.model.version if plan.model else None,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "width": plan.intermediate[0] if plan.intermediate else None,
        "height": plan.intermediate[1] if plan.intermediate else None,
        "restoration": round(plan.restoration, 4),
        "denoise": plan.denoise,
        "encoder": f"{ENCODER_VERSION}-crf{plan.intermediate_crf}-{plan.intermediate_preset}",
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            return None
    try:
        info = probe(path)
    except Exception:
        path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        return None
    duration = segment.end_ms - segment.start_ms
    frame_ms = 1000 / max(1.0, fps)
    if (
        (info.width, info.height) != (plan.intermediate)
        or abs(info.duration_ms - duration) > max(120, frame_ms * 1.5)
        or abs((info.fps or fps) - fps) > 0.6
        or info.has_audio
    ):
        path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        return None
    return path


def _write_manifest(
    path: Path,
    plan: EnhancementPlan,
    start_ms: int,
    end_ms: int,
    width: int,
    height: int,
    fps: float,
    frames: int,
) -> None:
    payload = {
        "version": 1,
        "model": plan.model.id if plan.model else None,
        "model_version": plan.model.version if plan.model else None,
        "profile": plan.profile,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "frames": frames,
        "restoration": round(plan.restoration, 4),
        "denoise": plan.denoise,
        "temporal": plan.temporal,
        "encoder": f"{ENCODER_VERSION}-crf{plan.intermediate_crf}-{plan.intermediate_preset}",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    manifest = path.with_suffix(".json")
    temporary = manifest.with_suffix(".json.part")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(manifest)


def _small_luma(frame: np.ndarray) -> np.ndarray:
    # A 1/16 sample is enough to tell "same shot" from "cut" without another
    # resize pass; the comparison is only used to decide whether to stabilise.
    return frame[::16, ::16, :3].astype(np.int16)


def _estimate_intermediate_bytes(
    plan: EnhancementPlan, *, duration_ms: int, fps: float
) -> int:
    if plan.intermediate is None:
        return 0
    width, height = plan.intermediate
    frames = max(1, int(round(duration_ms / 1000 * fps)))
    # CRF 12 on detailed footage lands near 0.10–0.16 bytes per pixel. Use the
    # upper end; the disk check is a guard, not a prediction.
    per_frame = int(width * height * 0.18)
    return per_frame * min(frames, 60_000) + 64 * 1024 * 1024


def _prune_cache(cache_dir: Path, limit: int = CACHE_LIMIT_BYTES) -> None:
    files = sorted(
        (path for path in cache_dir.glob("*.mp4") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    total = sum(path.stat().st_size for path in files)
    for path in files:
        if total <= limit:
            break
        total -= path.stat().st_size
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)


def _remove_partials(cache_dir: Path) -> None:
    for pattern in ("*.part.mp4", "*.json.part"):
        for path in cache_dir.glob(pattern):
            path.unlink(missing_ok=True)


def clear_project_cache(settings: Settings, project_id: str) -> None:
    shutil.rmtree(settings.data_dir / "cache" / "projects" / project_id / CACHE_DIR_NAME, ignore_errors=True)
