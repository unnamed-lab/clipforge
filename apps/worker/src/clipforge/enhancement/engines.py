"""Super-resolution engines behind one provider interface.

Two engines ship:

* ESPCN, loaded through OpenCV's DNN super-resolution module. It is a genuine
  convolutional super-resolution network, bundled with Clipforge, and fast
  enough to use on a CPU.
* Real-ESRGAN, loaded through ONNX Runtime from an optional downloaded model.

The interface is intentionally small: ``upscale(rgb) -> rgb`` at the model's
native scale. Denoising, blending, geometry and temporal handling belong to the
service, so a third engine only has to prove that it can upscale an array.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Protocol

import numpy as np

from clipforge.enhancement.settings import ModelSpec

logger = logging.getLogger(__name__)

BENCHMARK_VERSION = 1
ESPCN_TILE = 768
REALESRGAN_TILE = 256
TILE_OVERLAP = 16

try:  # OpenCV is a worker dependency, but a stripped install can omit it.
    import cv2
except ImportError:  # pragma: no cover - exercised only in stripped installs
    cv2 = None  # type: ignore[assignment]

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover - exercised only in stripped installs
    ort = None  # type: ignore[assignment]


class ModelUnavailable(RuntimeError):
    """The model or its runtime is not usable on this machine."""


class EnhancementEngine(Protocol):
    name: str
    device: str
    model: ModelSpec

    def upscale(self, rgb: np.ndarray) -> np.ndarray: ...


def _provider_plan() -> list[str]:
    if ort is None:
        return []
    available = set(ort.get_available_providers())
    preference = (os.environ.get("CLIPFORGE_ENHANCEMENT_DEVICE") or "auto").strip().lower()
    order = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    if preference in {"cpu", "cpu-only"}:
        order = ["CPUExecutionProvider"]
    elif preference in {"cuda", "gpu"}:
        order = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    elif preference in {"dml", "directml"}:
        order = ["DmlExecutionProvider", "CPUExecutionProvider"]
    plan = [provider for provider in order if provider in available]
    if "CPUExecutionProvider" not in plan and "CPUExecutionProvider" in available:
        plan.append("CPUExecutionProvider")
    return plan


def runtime_devices() -> dict:
    """What this process can actually use, without loading a model."""
    providers = _provider_plan()
    accelerated = any(
        provider in {"CUDAExecutionProvider", "DmlExecutionProvider"} for provider in providers
    )
    opencv_ok = bool(cv2 is not None and hasattr(cv2, "dnn_superres"))
    return {
        "providers": providers,
        "device": _device_label(providers) if providers else "CPU",
        "gpu_inference": accelerated,
        "onnxruntime": bool(ort is not None),
        "opencv_dnn_superres": opencv_ok,
        "cpu_fallback": True,
    }


def _device_label(providers: list[str]) -> str:
    for provider in providers:
        if provider == "CUDAExecutionProvider":
            return "NVIDIA GPU (CUDA)"
        if provider == "DmlExecutionProvider":
            return "GPU (DirectML)"
    return "CPU"


class EspcnEngine:
    """ESPCN x2/x4 through ``cv2.dnn_superres``.

    OpenCV's super-resolution wrapper expects BGR frames and returns BGR. The
    conversion is explicit here so callers can stay in RGB.
    """

    name = "opencv-espcn"

    def __init__(self, model: ModelSpec, path: Path) -> None:
        if cv2 is None:
            raise ModelUnavailable("OpenCV is not installed, so the ESPCN model cannot run.")
        if not hasattr(cv2, "dnn_superres"):
            raise ModelUnavailable(
                "This OpenCV build has no dnn_superres module. Install opencv-contrib-python-headless."
            )
        self.model = model
        self.path = path
        self.device = "CPU"
        self._sr = self._load()

    def _load(self):
        try:
            # OpenCV defaults to every logical CPU. On a 4-core/8-thread laptop
            # four workers measured faster than eight because the model stops
            # fighting itself for hyperthreads.
            default_threads = min(6, os.cpu_count() or 1)
            threads = int(os.environ.get("CLIPFORGE_OPENCV_THREADS", default_threads))
            cv2.setNumThreads(max(1, min(32, threads)))
            sr = cv2.dnn_superres.DnnSuperResImpl_create()
            sr.readModel(str(self.path))
            sr.setModel("espcn", int(self.model.scale))
            return sr
        except Exception as exc:  # pragma: no cover - depends on a broken install
            raise ModelUnavailable(f"The ESPCN model could not be loaded: {exc}") from exc

    def upscale(self, rgb: np.ndarray) -> np.ndarray:
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        if bgr.shape[0] * bgr.shape[1] > 2_000_000:
            bgr = _upscale_tiled(bgr, self._upscale_once, ESPCN_TILE, TILE_OVERLAP)
        else:
            bgr = self._upscale_once(bgr)
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def _upscale_once(self, bgr: np.ndarray) -> np.ndarray:
        return self._sr.upsample(bgr)


class RealEsrganEngine:
    """Real-ESRGAN RRDBNet through ONNX Runtime.

    The graph is dynamic in height and width, but memory grows with the output
    size, so the frame is tiled with overlap. Input is RGB float32 in 0–1, as
    documented by the exporter.
    """

    name = "onnx-realesrgan"

    def __init__(self, model: ModelSpec, path: Path) -> None:
        if ort is None:
            raise ModelUnavailable("ONNX Runtime is not installed, so Real-ESRGAN cannot run.")
        self.model = model
        self.path = path
        self.providers = _provider_plan()
        if not self.providers:
            raise ModelUnavailable("ONNX Runtime reports no usable execution provider.")
        self.device = _device_label(self.providers)
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = max(1, min(8, os.cpu_count() or 1))
        try:
            self._session = ort.InferenceSession(
                str(path), sess_options=options, providers=self.providers
            )
        except Exception as exc:  # pragma: no cover - depends on a broken install
            raise ModelUnavailable(f"The Real-ESRGAN model could not be loaded: {exc}") from exc
        self._input = self._session.get_inputs()[0].name
        self._output = self._session.get_outputs()[0].name

    def upscale(self, rgb: np.ndarray) -> np.ndarray:
        tile = REALESRGAN_TILE
        while True:
            try:
                return _upscale_tiled(rgb, self._upscale_once, tile, TILE_OVERLAP)
            except Exception as exc:
                message = str(exc).lower()
                if tile <= 128 or not any(
                    marker in message for marker in ("memory", "alloc", "bad_alloc", "out of")
                ):
                    raise
                tile //= 2
                logger.warning("Real-ESRGAN ran out of memory; retrying with %s px tiles", tile)

    def _upscale_once(self, rgb: np.ndarray) -> np.ndarray:
        tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
        output = self._session.run([self._output], {self._input: tensor})[0][0]
        output = np.clip(output.transpose(1, 2, 0), 0.0, 1.0) * 255.0
        return output.round().astype(np.uint8)


def _upscale_tiled(
    image: np.ndarray,
    upscale_once,
    tile: int,
    overlap: int,
) -> np.ndarray:
    """Run a fully convolutional model over overlapping tiles.

    Every output pixel is written exactly once. The overlap is extra context
    that is discarded after inference, so neighbouring tiles cannot produce a
    visible seam or a double-exposed edge.
    """
    if cv2 is None:  # pragma: no cover - only in a stripped install
        raise ModelUnavailable("OpenCV is required for tiled enhancement.")
    height, width = image.shape[:2]
    if height <= tile and width <= tile:
        return upscale_once(image)

    step = max(1, tile - overlap * 2)
    padded = cv2.copyMakeBorder(
        image, overlap, overlap, overlap, overlap, cv2.BORDER_REFLECT_101
    )
    out: np.ndarray | None = None
    scale: int | None = None
    for top in range(0, height, step):
        core_h = min(step, height - top)
        for left in range(0, width, step):
            core_w = min(step, width - left)
            piece = padded[top : top + tile, left : left + tile]
            enhanced = upscale_once(piece)
            if scale is None:
                scale = max(1, int(round(enhanced.shape[0] / tile)))
                out = np.zeros((height * scale, width * scale, 3), dtype=np.uint8)
            assert scale is not None and out is not None
            inner = enhanced[
                overlap * scale : (overlap + core_h) * scale,
                overlap * scale : (overlap + core_w) * scale,
            ]
            out[
                top * scale : (top + core_h) * scale,
                left * scale : (left + core_w) * scale,
            ] = inner
    assert out is not None
    return out


_ENGINES: dict[tuple[str, str], EnhancementEngine] = {}
_ENGINE_ERRORS: dict[tuple[str, str], str] = {}
_ENGINE_LOCK = threading.Lock()


def engine_for(
    model: ModelSpec,
    *,
    models_dir: Path,
    device: str = "auto",
) -> EnhancementEngine:
    path = model.bundled_path() if model.bundled else model.installed_path(models_dir)
    key = (str(path), device)
    with _ENGINE_LOCK:
        if key in _ENGINES:
            return _ENGINES[key]
        if key in _ENGINE_ERRORS:
            raise ModelUnavailable(_ENGINE_ERRORS[key])
        if not path.is_file() or path.stat().st_size < 1024:
            message = (
                f"The {model.label} model is not installed."
                if not model.bundled
                else f"The bundled {model.label} model is missing from this installation."
            )
            _ENGINE_ERRORS[key] = message
            raise ModelUnavailable(message)
        if model.size_bytes and path.stat().st_size != model.size_bytes:
            message = (
                f"The {model.label} model file is {path.stat().st_size} bytes; "
                f"expected {model.size_bytes}. Reinstall it."
            )
            _ENGINE_ERRORS[key] = message
            raise ModelUnavailable(message)
        try:
            engine = (
                EspcnEngine(model, path)
                if model.engine == "espcn"
                else RealEsrganEngine(model, path)
            )
        except ModelUnavailable as exc:
            _ENGINE_ERRORS[key] = str(exc)
            raise
        _ENGINES[key] = engine
        return engine


def reset_engines() -> None:
    """Test helper: drop loaded sessions and remembered errors."""
    with _ENGINE_LOCK:
        _ENGINES.clear()
        _ENGINE_ERRORS.clear()


def blend(ai: np.ndarray, original: np.ndarray, restoration: float) -> np.ndarray:
    """Mix the model result with a high-quality resize of the same frame."""
    strength = max(0.0, min(1.0, float(restoration)))
    if strength >= 0.999:
        return ai
    if strength <= 0.001:
        return original
    mixed = ai.astype(np.float32) * strength + original.astype(np.float32) * (1.0 - strength)
    return np.clip(mixed, 0, 255).round().astype(np.uint8)


def resize_to(rgb: np.ndarray, width: int, height: int) -> np.ndarray:
    if cv2 is None:  # pragma: no cover - only in a stripped install
        raise ModelUnavailable("OpenCV is required for resizing enhanced frames.")
    if rgb.shape[1] == width and rgb.shape[0] == height:
        return rgb
    return cv2.resize(rgb, (width, height), interpolation=cv2.INTER_LANCZOS4)


def _benchmark_path(models_dir: Path) -> Path:
    return models_dir / "enhancement" / "benchmarks.json"


def _read_benchmarks(models_dir: Path) -> dict:
    try:
        payload = json.loads(_benchmark_path(models_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record_benchmark(
    models_dir: Path,
    *,
    model: ModelSpec,
    device: str,
    seconds: float,
    input_pixels: int,
) -> dict:
    if seconds <= 0 or input_pixels <= 0:
        return {}
    entry = {
        "version": BENCHMARK_VERSION,
        "model": model.id,
        "model_version": model.version,
        "device": device,
        "seconds_per_input_megapixel": round(seconds / (input_pixels / 1_000_000), 4),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "measured on this machine; CNN compute is content-independent",
    }
    payload = _read_benchmarks(models_dir)
    payload[model.id] = entry
    path = _benchmark_path(models_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.part")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)
    return entry


def load_benchmark(models_dir: Path, model_id: str) -> dict | None:
    entry = _read_benchmarks(models_dir).get(model_id)
    return entry if isinstance(entry, dict) else None


def benchmark(model: ModelSpec, *, models_dir: Path) -> dict:
    """Measure one warm pass on a representative frame and persist the result.

    This runs only when the user installs a model, never on the job thread that
    is encoding a clip.
    """
    engine = engine_for(model, models_dir=models_dir)
    width, height = 480, 270
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    engine.upscale(frame)  # warm-up: kernels, allocation, lazy graph initialisation
    started = time.monotonic()
    engine.upscale(frame)
    seconds = time.monotonic() - started
    return record_benchmark(
        models_dir,
        model=model,
        device=engine.device,
        seconds=seconds,
        input_pixels=width * height,
    )
