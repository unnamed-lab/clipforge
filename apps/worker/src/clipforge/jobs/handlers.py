from __future__ import annotations

import logging
import time

from clipforge.db.repositories import Repositories
from clipforge.errors import JobFailed
from clipforge.jobs.queue import JobContext
from clipforge.media.importer import download_remote, progress_band
from clipforge.media.process import ProcessCancelled
from clipforge.pipeline.analysis import run_analysis
from clipforge.rendering.service import perform_render
from clipforge.security.urls import validate_and_resolve

logger = logging.getLogger(__name__)


def handle_analysis(ctx: JobContext) -> dict:
    payload = ctx.job.get("payload") or {}
    project_id = ctx.job.get("project_id") or payload.get("project_id")
    if not project_id:
        raise JobFailed("invalid_job", "This job is missing its project.")
    return run_analysis(
        ctx.repos,
        ctx.settings,
        ctx.storage,
        project_id=project_id,
        request=payload,
        on_stage=ctx.on_stage,
        should_cancel=ctx.should_cancel,
        cancel_event=ctx.cancel_event,
    )


def handle_render(ctx: JobContext) -> dict:
    payload = ctx.job.get("payload") or {}
    render_id = payload.get("render_id")
    if not render_id:
        raise JobFailed("invalid_job", "This job is missing its render.")
    ctx.repos.renders.mark_running(render_id)
    try:
        return perform_render(
            ctx.repos,
            ctx.settings,
            ctx.storage,
            render_id=render_id,
            on_stage=ctx.on_stage,
            cancel_event=ctx.cancel_event,
            should_cancel=ctx.should_cancel,
        )
    except ProcessCancelled:
        ctx.repos.renders.mark_cancelled(render_id)
        raise
    except JobFailed as exc:
        if exc.code == "cancelled":
            ctx.repos.renders.mark_cancelled(render_id)
        else:
            ctx.repos.renders.mark_failed(render_id, exc.code, exc.message)
        raise
    except Exception as exc:
        ctx.repos.renders.mark_failed(render_id, "render_failed", str(exc)[:400])
        raise


def handle_url_import(ctx: JobContext) -> dict:
    payload = ctx.job.get("payload") or {}
    project_id = ctx.job.get("project_id")
    url = payload.get("url")
    if not project_id or not url:
        raise JobFailed("invalid_job", "This job is missing its project or URL.")
    job_id = ctx.job["id"]
    ctx.on_stage("validating", 1, "Checking the link")
    validated = validate_and_resolve(url)

    stages: dict[str, dict] = {}
    last_write = {"at": 0.0}

    def report(stage: str, percent: float | None, message: str, detail: dict) -> None:
        # The first report of a stage must land even if the previous write was
        # milliseconds ago, otherwise a fast stage is invisible in the metrics.
        is_new_stage = stage not in stages
        stages[stage] = {**detail, "percent": None if percent is None else round(percent, 1)}
        # Progress text is throttled by the queue; metrics are only written on a
        # stage change or twice a second so a fast download does not hammer SQLite.
        now = time.monotonic()
        if is_new_stage or now - last_write["at"] >= 0.5:
            last_write["at"] = now
            try:
                ctx.repos.jobs.set_metrics(job_id, {"stages": stages})
            except Exception:  # telemetry must never fail the download
                logger.debug("Could not record download metrics", exc_info=True)
        ctx.on_stage(stage, progress_band(stage, percent), message)

    record, metrics = download_remote(
        ctx.repos,
        ctx.settings,
        ctx.storage,
        project_id=project_id,
        validated=validated,
        quality=payload.get("quality"),
        processing=payload.get("processing") or "balanced",
        proxy_height=payload.get("proxy_height"),
        on_progress=report,
        should_cancel=ctx.should_cancel,
        cancel_event=ctx.cancel_event,
    )
    try:
        ctx.repos.jobs.set_metrics(job_id, metrics)
    except Exception:
        logger.debug("Could not record final download metrics", exc_info=True)
    ctx.on_stage("done", 100, "Download complete")
    return {
        "source_id": record["id"],
        "duration_ms": record["duration_ms"],
        "has_audio": bool(record["has_audio"]),
        "has_video": bool(record.get("has_video", True)),
        "title": record["original_name"],
        "download": metrics.get("download"),
    }


def build_handlers(repos: Repositories) -> dict:
    return {
        "analysis": handle_analysis,
        "render": handle_render,
        "url_import": handle_url_import,
    }
