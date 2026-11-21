from __future__ import annotations

from fastapi import APIRouter, Request

from clipforge.api.deps import get_state
from clipforge.enhancement.install import remove_model
from clipforge.enhancement.engines import load_benchmark, runtime_devices
from clipforge.enhancement.settings import available_models, model_by_id
from clipforge.errors import ValidationFailed

router = APIRouter(prefix="/api/enhancement", tags=["enhancement"])


def _models(settings) -> list[dict]:
    rows = available_models(settings.models_dir)
    for row in rows:
        row["benchmark"] = load_benchmark(settings.models_dir, row["id"])
    return rows


@router.get("/models")
def list_models(request: Request) -> dict:
    state = get_state(request)
    return {
        "models": _models(state.settings),
        "devices": runtime_devices(),
        "download_requires_consent": True,
    }


@router.post("/models/{model_id}/install")
def install(model_id: str, request: Request) -> dict:
    """Queue a one-time weight download. No user media is involved."""
    state = get_state(request)
    try:
        model = model_by_id(model_id)
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="invalid_enhancement_model") from exc
    if model.bundled:
        return {"model": model_id, "installed": True, "job": None, "bundled": True}
    if model.installed_path(state.settings.models_dir).is_file():
        return {"model": model_id, "installed": True, "job": None, "bundled": False}
    job = state.queue.enqueue(
        project_id=None,
        job_type="model_install",
        payload={"model_id": model_id},
    )
    return {"model": model_id, "installed": False, "job": job, "bundled": False}


@router.delete("/models/{model_id}")
def uninstall(model_id: str, request: Request) -> dict:
    state = get_state(request)
    try:
        return remove_model(model_id, state.settings)
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="invalid_enhancement_model") from exc


@router.get("/benchmarks")
def benchmarks(request: Request) -> dict:
    state = get_state(request)
    return {
        "benchmarks": [
            {"model": row["id"], "benchmark": load_benchmark(state.settings.models_dir, row["id"])}
            for row in available_models(state.settings.models_dir)
        ],
        "devices": runtime_devices(),
    }
