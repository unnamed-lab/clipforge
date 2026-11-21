"""Enhancement settings, model registry and deterministic geometry.

Everything here is pure: no model is loaded, no file is downloaded and no
FFmpeg process is started. That keeps the rules testable and lets the API
describe exactly what a profile will do before a render is queued.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

EnhancementProfile = Literal["standard", "clean", "ai", "ai_plus"]
ModelId = Literal["auto", "espcn-x2", "espcn-x4", "realesrgan-x2"]

DEFAULT_MODEL_ID = "espcn-x2"
MAX_INTERMEDIATE_PIXELS = 24_000_000
MAX_INTERMEDIATE_EDGE = 7680

ENHANCEMENT_PROFILES: dict[str, dict] = {
    "standard": {
        "label": "Standard",
        "summary": "High-quality resizing only. No denoising and no AI model.",
        "engine": "FFmpeg lanczos",
        "default_denoise": 0,
        "default_restoration": 1.0,
        "warnings": [],
    },
    "clean": {
        "label": "Clean",
        "summary": "Conservative denoising before a high-quality resize. Preserves natural texture.",
        "engine": "FFmpeg hqdn3d + lanczos",
        "default_denoise": 30,
        "default_restoration": 1.0,
        "warnings": [
            "Denoising trades a little fine texture for fewer compression artefacts. It does not add detail.",
        ],
    },
    "ai": {
        "label": "AI Enhance",
        "summary": "A tested super-resolution model at a conservative blend, then a high-quality resize.",
        "engine": "ESPCN / Real-ESRGAN",
        "default_denoise": 15,
        "default_restoration": 0.90,
        "warnings": [
            "AI upscaling can invent plausible texture. It cannot restore detail the source never captured.",
            "Frame rate is kept unchanged; this profile does not interpolate frames.",
        ],
    },
    "ai_plus": {
        "label": "AI Enhance+",
        "summary": "Full model strength with static-frame stabilisation to reduce flicker on HUD and text.",
        "engine": "ESPCN / Real-ESRGAN",
        "default_denoise": 30,
        "default_restoration": 1.0,
        "warnings": [
            "AI Enhance+ can alter fine detail and faces. Compare a frame before exporting a full clip.",
            "Full-strength AI is slower than AI Enhance, especially on CPU-only machines.",
            "Frame rate is kept unchanged; this profile does not interpolate frames.",
        ],
    },
}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    engine: Literal["espcn", "realesrgan"]
    scale: int
    filename: str
    version: str
    licence: str
    licence_url: str
    size_bytes: int
    sha256: str | None
    download_url: str | None
    bundled: bool
    notes: str

    def bundled_path(self) -> Path:
        return Path(__file__).with_name("models") / self.filename

    def installed_path(self, models_dir: Path) -> Path:
        return models_dir / "enhancement" / self.filename


MODELS: dict[str, ModelSpec] = {
    "espcn-x2": ModelSpec(
        id="espcn-x2",
        label="ESPCN 2× (bundled, CPU-friendly)",
        engine="espcn",
        scale=2,
        filename="ESPCN_x2.pb",
        version="tf-espcn-2x-2019",
        licence="Apache-2.0",
        licence_url="https://github.com/fannymonori/TF-ESPCN/blob/master/LICENSE",
        size_bytes=86_446,
        sha256=None,
        download_url=None,
        bundled=True,
        notes=(
            "Real-time single-image super-resolution (Shi et al., 2016), trained by the "
            "OpenCV GSoC project. Fast enough for short clips on a 4-core CPU; less "
            "aggressive than Real-ESRGAN."
        ),
    ),
    "espcn-x4": ModelSpec(
        id="espcn-x4",
        label="ESPCN 4× (bundled, CPU-friendly)",
        engine="espcn",
        scale=4,
        filename="ESPCN_x4.pb",
        version="tf-espcn-4x-2019",
        licence="Apache-2.0",
        licence_url="https://github.com/fannymonori/TF-ESPCN/blob/master/LICENSE",
        size_bytes=100_323,
        sha256=None,
        download_url=None,
        bundled=True,
        notes=(
            "The 4× ESPCN model. Useful when a landscape source is composed into a "
            "portrait frame and the composition needs more than 2× on the source."
        ),
    ),
    "realesrgan-x2": ModelSpec(
        id="realesrgan-x2",
        label="Real-ESRGAN 2× (optional, slow on CPU)",
        engine="realesrgan",
        scale=2,
        filename="real_esrgan_x2.onnx",
        version="realesrgan-x2plus-onnx-2024",
        licence="BSD-3-Clause",
        licence_url="https://github.com/xinntao/Real-ESRGAN/blob/master/LICENSE",
        size_bytes=67_073_434,
        sha256="7115ba92e8a1bfa63d68558ef006ef3d91273a068d321b1439f8bb1c9179002c",
        download_url=(
            "https://huggingface.co/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x2.onnx"
        ),
        bundled=False,
        notes=(
            "1:1 ONNX export of the official RealESRGAN_x2plus.pth weights by SceneWorks. "
            "Exact interface documented by the exporter. Roughly 100× slower than ESPCN "
            "on CPU, so it is offered for short clips and GPU runtimes."
        ),
    ),
}


class EnhancementSettings(BaseModel):
    """What the user chose. Defaults are the honest non-AI path."""

    profile: EnhancementProfile = "standard"
    model_id: ModelId = "auto"
    # 0–100. Denoising runs before the model or the resize.
    denoise: int = Field(default=0, ge=0, le=100)
    # Share of the model result kept in the blend. 1.0 is the raw model.
    restoration: float = Field(default=1.0, ge=0.0, le=1.0)
    # A missing model must never silently turn into a fake AI claim. When
    # fallback is allowed the render proceeds on the conventional path and the
    # report says exactly that; when it is refused the job fails with a clear
    # message.
    allow_fallback: bool = True
    # Static-frame averaging only. There is no motion interpolation.
    temporal_stabilize: bool = False
    # The UI asks for a comparison before a full AI Enhance+ export when it can.
    compare_first: bool = False

    @model_validator(mode="after")
    def _profile_defaults(self) -> "EnhancementSettings":
        meta = ENHANCEMENT_PROFILES[self.profile]
        if "denoise" not in self.model_fields_set:
            self.denoise = int(meta["default_denoise"])
        if "restoration" not in self.model_fields_set:
            self.restoration = float(meta["default_restoration"])
        if self.profile == "standard":
            self.denoise = 0
            self.restoration = 1.0
        if "temporal_stabilize" not in self.model_fields_set:
            self.temporal_stabilize = self.profile == "ai_plus"
        if self.profile in {"ai", "ai_plus"} and self.restoration <= 0:
            raise ValueError("AI profiles need a restoration strength above zero")
        return self

    @property
    def uses_ai(self) -> bool:
        return self.profile in {"ai", "ai_plus"}

    def to_report(self) -> dict:
        return self.model_dump()


def model_by_id(model_id: str) -> ModelSpec:
    try:
        return MODELS[model_id]
    except KeyError as exc:
        raise ValueError(
            f"'{model_id}' is not an enhancement model. Choose one of: {', '.join(MODELS)}."
        ) from exc


def resolve_model(
    settings: EnhancementSettings,
    *,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> ModelSpec:
    """Pick the model for a profile when the user left the choice on auto.

    ``auto`` prefers the smallest model that covers the needed scale. A
    landscape source composed into a portrait frame can need more than 2×, so
    the 4× model is chosen there rather than upscaling a 2× result again.
    """
    if settings.model_id != "auto":
        return model_by_id(settings.model_id)
    needed = max(
        target_width / max(1, source_width),
        target_height / max(1, source_height),
    )
    if settings.profile == "ai_plus" and needed > 1.35:
        return MODELS["espcn-x4"]
    return MODELS["espcn-x2"] if needed <= 2.25 else MODELS["espcn-x4"]


def intermediate_dimensions(
    *,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
    model: ModelSpec,
) -> tuple[int, int]:
    """Full-frame intermediate size that needs no further upscale in composition.

    The intermediate keeps the source aspect ratio. Composition crops and fits
    it exactly as it would the source, so every normalised rectangle stays
    valid. The model's native output is resized once to this size; the
    composition then scales down or crops, never upscales beyond it.
    """
    needed = max(
        target_width / max(1, source_width),
        target_height / max(1, source_height),
    )
    factor = max(float(model.scale), needed)
    width = _even(source_width * factor)
    height = _even(source_height * factor)
    edge_scale = min(
        1.0,
        MAX_INTERMEDIATE_EDGE / max(1, width),
        MAX_INTERMEDIATE_EDGE / max(1, height),
    )
    pixel_scale = min(1.0, (MAX_INTERMEDIATE_PIXELS / max(1, width * height)) ** 0.5)
    shrink = min(edge_scale, pixel_scale)
    if shrink < 1.0:
        width = _even(width * shrink)
        height = _even(height * shrink)
    return max(2, width), max(2, height)


def denoise_filter(strength: int) -> str | None:
    """Map 0–100 onto a conservative hqdn3d filter.

    The defaults are deliberately below the values that erase skin pores or
    fabric weave. 30 is about ``hqdn3d=2:2.4:5:6``.
    """
    if strength <= 0:
        return None
    s = max(0, min(100, int(strength))) / 100.0
    luma = 0.5 + 5.0 * s
    chroma = luma * 1.2
    temporal = 2.0 + 10.0 * s
    return f"hqdn3d={luma:.2f}:{chroma:.2f}:{temporal:.2f}:{temporal * 1.2:.2f}"


def cache_key(
    *,
    source_identity: str,
    start_ms: int,
    end_ms: int,
    settings: EnhancementSettings,
    model: ModelSpec,
    intermediate: tuple[int, int],
    fps: float | None,
    encoder_version: str,
) -> str:
    """Identity of one enhanced segment.

    Every input that can change pixels or timing is present. Project id is
    deliberately absent: it is not a pixel input, and using it would make the
    same work repeat after a project copy.
    """
    payload = {
        "version": 1,
        "source": source_identity,
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "profile": settings.profile,
        "model": model.id,
        "model_version": model.version,
        "model_sha": model.sha256,
        "denoise": int(settings.denoise),
        "restoration": round(float(settings.restoration), 4),
        "temporal": bool(settings.temporal_stabilize),
        "intermediate": list(intermediate),
        "fps": round(float(fps or 0), 3),
        "encoder": encoder_version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def available_models(models_dir: Path) -> list[dict]:
    rows = []
    for model in MODELS.values():
        path = model.bundled_path() if model.bundled else model.installed_path(models_dir)
        installed = path.is_file() and path.stat().st_size > 1024
        rows.append(
            {
                "id": model.id,
                "label": model.label,
                "engine": model.engine,
                "scale": model.scale,
                "version": model.version,
                "licence": model.licence,
                "licence_url": model.licence_url,
                "size_bytes": model.size_bytes,
                "installed": installed,
                "bundled": model.bundled,
                "downloadable": not model.bundled,
                "download_url": model.download_url,
                "sha256": model.sha256,
                "notes": model.notes,
            }
        )
    return rows


def _even(value: float) -> int:
    rounded = max(2, int(round(value)))
    return rounded + (rounded % 2)
