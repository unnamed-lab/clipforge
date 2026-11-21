"""Optional video enhancement: conventional resizing, denoising and AI upscaling.

The package is deliberately separate from ``rendering``: rendering owns the
composition graph, while this package owns the pixels that go into it. It has
one provider interface so a new engine can be added without changing the editor
or the render service.
"""

from clipforge.enhancement.settings import (
    DEFAULT_MODEL_ID,
    ENHANCEMENT_PROFILES,
    MODELS,
    EnhancementProfile,
    EnhancementSettings,
    ModelSpec,
    available_models,
    cache_key,
    intermediate_dimensions,
    model_by_id,
    resolve_model,
)

__all__ = [
    "DEFAULT_MODEL_ID",
    "ENHANCEMENT_PROFILES",
    "MODELS",
    "EnhancementProfile",
    "EnhancementSettings",
    "ModelSpec",
    "available_models",
    "cache_key",
    "intermediate_dimensions",
    "model_by_id",
    "resolve_model",
]
