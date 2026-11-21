"""One-time download of optional model weights.

The default AI model is bundled; this path exists only for the larger optional
Real-ESRGAN export. Nothing here touches a user's video, and the download is
started only by an explicit user action. The file is verified against a pinned
SHA-256 before it is moved into the model cache.
"""

from __future__ import annotations

import hashlib
import logging
import time

import httpx

from clipforge.config import Settings
from clipforge.enhancement.engines import benchmark
from clipforge.enhancement.settings import MODELS, ModelSpec, model_by_id
from clipforge.errors import JobFailed

logger = logging.getLogger(__name__)

CHUNK_BYTES = 1024 * 1024


def installed(model: ModelSpec, settings: Settings) -> bool:
    path = model.bundled_path() if model.bundled else model.installed_path(settings.models_dir)
    return path.is_file() and path.stat().st_size == model.size_bytes


def install_model(
    model_id: str,
    *,
    settings: Settings,
    on_progress=None,
    should_cancel=None,
) -> dict:
    model = model_by_id(model_id)
    if model.bundled:
        return {
            "model": model.id,
            "installed": True,
            "downloaded": False,
            "reason": "This model ships with Clipforge.",
        }
    if not model.download_url or not model.sha256:
        raise JobFailed(
            "enhancement_model_invalid",
            f"{model.label} has no verified download source configured.",
        )
    destination = model.installed_path(settings.models_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if installed(model, settings):
        return {
            "model": model.id,
            "installed": True,
            "downloaded": False,
            "path": str(destination),
        }

    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    digest = hashlib.sha256()
    written = 0
    started = time.monotonic()
    try:
        with httpx.stream("GET", model.download_url, follow_redirects=True, timeout=120.0) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or model.size_bytes)
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    if should_cancel is not None and should_cancel():
                        raise JobFailed("cancelled", "Cancelled")
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(
                            min(99.0, written / max(1, total) * 100),
                            f"Downloading {model.label}: {written / (1024 ** 2):.1f} "
                            f"of {total / (1024 ** 2):.1f} MB",
                        )
    except JobFailed:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise JobFailed(
            "enhancement_model_download_failed",
            f"{model.label} could not be downloaded. The existing models are unchanged. {exc}",
        ) from exc

    if written != model.size_bytes:
        partial.unlink(missing_ok=True)
        raise JobFailed(
            "enhancement_model_download_invalid",
            f"{model.label} downloaded {written} bytes; expected {model.size_bytes}.",
        )
    actual = digest.hexdigest()
    if actual != model.sha256:
        partial.unlink(missing_ok=True)
        raise JobFailed(
            "enhancement_model_download_invalid",
            f"{model.label} failed its SHA-256 check and was discarded.",
        )
    partial.replace(destination)
    report = {
        "model": model.id,
        "installed": True,
        "downloaded": True,
        "path": str(destination),
        "size_bytes": written,
        "seconds": round(time.monotonic() - started, 1),
        "sha256": actual,
        "licence": model.licence,
        "licence_url": model.licence_url,
    }
    if on_progress:
        on_progress(99.5, "Verifying the model on this machine")
    try:
        report["benchmark"] = benchmark(model, models_dir=settings.models_dir)
    except Exception as exc:  # a benchmark is telemetry, not installation
        logger.warning("The model installed but its benchmark failed: %s", exc)
        report["benchmark"] = None
    if on_progress:
        on_progress(100.0, f"{model.label} is ready")
    return report


def remove_model(model_id: str, settings: Settings) -> dict:
    model = model_by_id(model_id)
    if model.bundled:
        raise JobFailed("enhancement_model_bundled", "Bundled models cannot be removed.")
    path = model.installed_path(settings.models_dir)
    path.unlink(missing_ok=True)
    path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)
    return {"model": model.id, "installed": False}


def known_model_ids() -> list[str]:
    return list(MODELS)
