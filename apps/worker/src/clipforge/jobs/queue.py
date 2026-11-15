"""Persistent local job queue.

One consumer thread, FIFO, SQLite-backed. Jobs are claimed with a conditional UPDATE
so a second process (or a double click) cannot run the same job twice. Cancellation is
cooperative between stages and preemptive inside FFmpeg.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.errors import AppError, JobFailed
from clipforge.media.process import ProcessCancelled

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.4
PROGRESS_WRITE_INTERVAL_SECONDS = 0.35


@dataclass
class JobContext:
    repos: Repositories
    settings: Settings
    job: dict
    cancel_event: threading.Event
    should_cancel: Callable[[], bool]
    on_stage: Callable[[str, float, str], None]


Handler = Callable[[JobContext], dict | None]


class JobQueue:
    def __init__(self, repos: Repositories, settings: Settings, handlers: dict[str, Handler]) -> None:
        self.repos = repos
        self.settings = settings
        self.handlers = handlers
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._current_job_id: str | None = None
        self._current_cancel: threading.Event | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        orphans = self.repos.jobs.mark_orphans()
        if orphans:
            logger.warning("Marked %s interrupted job(s) as failed after restart", orphans)
        self._thread = threading.Thread(target=self._loop, name="clipforge-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        if self._current_cancel is not None:
            self._current_cancel.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- public operations -------------------------------------------------

    def enqueue(self, *, project_id: str | None, job_type: str, payload: dict | None = None) -> dict:
        job = self.repos.jobs.create(project_id=project_id, job_type=job_type, payload=payload or {})
        self._wake.set()
        return job

    def cancel(self, job_id: str) -> dict:
        job = self.repos.jobs.request_cancel(job_id)
        with self._lock:
            if self._current_job_id == job_id and self._current_cancel is not None:
                self._current_cancel.set()
        return job

    def retry(self, job_id: str) -> dict:
        job = self.repos.jobs.get(job_id)
        active = self.repos.jobs.active_for(job["project_id"], job["type"]) if job["project_id"] else None
        if active and active["id"] != job_id:
            return active
        requeued = self.repos.jobs.requeue(job_id)
        self._wake.set()
        return requeued

    # -- internals ---------------------------------------------------------

    def _loop(self) -> None:
        logger.info("Job queue online")
        while not self._stop.is_set():
            job = None
            try:
                job = self.repos.jobs.claim_next()
            except Exception:
                logger.exception("Failed to claim a job")
            if job is None:
                self._wake.wait(POLL_INTERVAL_SECONDS)
                self._wake.clear()
                continue
            self._run(job)

    def _run(self, job: dict) -> None:
        job_id = job["id"]
        cancel_event = threading.Event()
        with self._lock:
            self._current_job_id = job_id
            self._current_cancel = cancel_event

        last_write = {"time": 0.0, "stage": ""}

        def on_stage(stage: str, progress: float, message: str) -> None:
            if self.repos.jobs.is_cancel_requested(job_id):
                cancel_event.set()
            now = time.monotonic()
            if stage != last_write["stage"] or now - last_write["time"] >= PROGRESS_WRITE_INTERVAL_SECONDS:
                last_write["time"] = now
                last_write["stage"] = stage
                try:
                    self.repos.jobs.update_progress(job_id, stage=stage, progress=progress, message=message)
                except Exception:
                    logger.exception("Failed to write progress for job %s", job_id)

        context = JobContext(
            repos=self.repos,
            settings=self.settings,
            job=job,
            cancel_event=cancel_event,
            should_cancel=lambda: cancel_event.is_set() or self.repos.jobs.is_cancel_requested(job_id),
            on_stage=on_stage,
        )

        handler = self.handlers.get(job["type"])
        logger.info("Job %s (%s) starting", job_id, job["type"])
        try:
            if handler is None:
                raise JobFailed("unknown_job", "This job type is not supported by the worker.")
            result = handler(context) or {}
            if context.should_cancel():
                self.repos.jobs.cancel(job_id)
            else:
                self.repos.jobs.complete(job_id, result)
                logger.info("Job %s completed", job_id)
        except ProcessCancelled:
            self.repos.jobs.cancel(job_id)
            logger.info("Job %s cancelled during a subprocess", job_id)
        except JobFailed as exc:
            if exc.code == "cancelled" or context.should_cancel():
                self.repos.jobs.cancel(job_id)
            else:
                self.repos.jobs.fail(job_id, code=exc.code, message=exc.message)
                logger.warning("Job %s failed: %s (%s)", job_id, exc.message, exc.code)
        except AppError as exc:
            self.repos.jobs.fail(job_id, code=exc.code, message=exc.message)
            logger.warning("Job %s failed: %s (%s)", job_id, exc.message, exc.code)
        except Exception as exc:  # pragma: no cover - unexpected paths
            logger.error("Job %s crashed: %s\n%s", job_id, exc, traceback.format_exc())
            self.repos.jobs.fail(
                job_id,
                code="internal_error",
                message="Something went wrong inside the worker. The original video is untouched; retry the job.",
            )
        finally:
            with self._lock:
                self._current_job_id = None
                self._current_cancel = None
