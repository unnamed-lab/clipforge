from __future__ import annotations

import logging

from clipforge.db.repositories import Repositories
from clipforge.errors import JobFailed
from clipforge.jobs.queue import JobContext
from clipforge.media.importer import download_remote
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
    ctx.on_stage("validating", 2, "Checking the link")
    validated = validate_and_resolve(url)
    ctx.on_stage("downloading", 5, "Downloading the video")
    record = download_remote(
        ctx.repos,
        ctx.settings,
        project_id=project_id,
        validated=validated,
        on_progress=lambda percent, message: ctx.on_stage("downloading", 5 + percent * 0.9, message),
        should_cancel=ctx.should_cancel,
    )
    ctx.on_stage("done", 100, "Download complete")
    return {
        "source_id": record["id"],
        "duration_ms": record["duration_ms"],
        "has_audio": bool(record["has_audio"]),
        "title": record["original_name"],
    }


def build_handlers(repos: Repositories) -> dict:
    return {
        "analysis": handle_analysis,
        "render": handle_render,
        "url_import": handle_url_import,
    }
