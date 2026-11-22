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
from clipforge.storage.base import StorageBackend

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.4
PROGRESS_WRITE_INTERVAL_SECONDS = 0.35

# A transient failure parks the job instead of ending it. Waiting grows from a
# minute to half an hour, and stops after a handful of attempts so a source
# that is never coming back still fails instead of looping forever.
AUTOMATIC_RETRY_LIMIT = 6
RETRY_DELAY_BASE_SECONDS = 60
RETRY_DELAY_MAX_SECONDS = 30 * 60


def _describe_delay(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return "a minute" if minutes == 1 else f"{minutes} minutes"


@dataclass
class JobContext:
    repos: Repositories
    settings: Settings
    job: dict
    cancel_event: threading.Event
    should_cancel: Callable[[], bool]
    on_stage: Callable[[str, float, str], None]
    storage: StorageBackend


Handler = Callable[[JobContext], dict | None]


class JobQueue:
    def __init__(self, repos: Repositories, settings: Settings, handlers: dict[str, Handler],
                 storage: StorageBackend) -> None:
        self.repos = repos
        self.settings = settings
        self.handlers = handlers
        self.storage = storage
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._current_job_id: str | None = None
        self._current_cancel: threading.Event | None = None
        # Interruptions that must end as failed/stalled, not as a completion or
        # a user cancellation. The watchdog writes, the job thread settles.
        self._interruptions: dict[str, tuple[str, str]] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        recovered = self.repos.jobs.recover_interrupted()
        if recovered["queued"]:
            logger.warning(
                "Resuming %s interrupted job(s) after a restart", recovered["queued"]
            )
        if recovered["failed"]:
            logger.warning(
                "Marked %s interrupted job(s) as failed after restart", recovered["failed"]
            )
        if recovered["cancelled"]:
            logger.info("Settled %s cancelled job(s) after a restart", recovered["cancelled"])
        self._thread = threading.Thread(target=self._loop, name="clipforge-jobs", daemon=True)
        self._thread.start()
        if self.settings.pipeline_stall_seconds > 0:
            self._watchdog = threading.Thread(
                target=self._watch, name="clipforge-watchdog", daemon=True
            )
            self._watchdog.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        if self._current_cancel is not None:
            self._current_cancel.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._watchdog is not None:
            self._watchdog.join(timeout=min(timeout, 5))

    # -- public operations -------------------------------------------------

    def enqueue(self, *, project_id: str | None, job_type: str, payload: dict | None = None) -> dict:
        job = self.repos.jobs.create(project_id=project_id, job_type=job_type, payload=payload or {})
        self._wake.set()
        return job

    def cancel(self, job_id: str) -> dict:
        job = self.repos.jobs.request_cancel(job_id)
        if job["state"] == "waiting":
            # Nothing is running to notice the flag, so settle it now.
            return self.repos.jobs.cancel(job_id)
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
                    self.repos.jobs.heartbeat(job_id)
                except Exception:
                    logger.exception("Failed to write progress for job %s", job_id)

        def has_interrupted() -> bool:
            with self._lock:
                return job_id in self._interruptions

        context = JobContext(
            repos=self.repos,
            settings=self.settings,
            job=job,
            cancel_event=cancel_event,
            should_cancel=lambda: (
                cancel_event.is_set()
                or has_interrupted()
                or self.repos.jobs.is_cancel_requested(job_id)
                or self.repos.jobs.get(job_id)["state"] in {"failed", "cancelled"}
            ),
            on_stage=on_stage,
            storage=self.storage,
        )

        handler = self.handlers.get(job["type"])
        logger.info("Job %s (%s) starting", job_id, job["type"])
        try:
            if handler is None:
                raise JobFailed("unknown_job", "This job type is not supported by the worker.")
            result = handler(context) or {}
            interruption = self._take_interruption(job_id)
            if interruption is not None:
                code, message = interruption
                self.repos.jobs.fail(job_id, code=code, message=message)
                logger.warning("Job %s interrupted: %s", job_id, code)
            elif self.repos.jobs.get(job_id)["state"] in {"failed", "cancelled"}:
                logger.info("Job %s was settled externally; not marking it complete", job_id)
            elif context.should_cancel():
                self.repos.jobs.cancel(job_id)
            else:
                self.repos.jobs.complete(job_id, result)
                logger.info("Job %s completed", job_id)
        except ProcessCancelled:
            interruption = self._take_interruption(job_id)
            if interruption is not None:
                self.repos.jobs.fail(job_id, code=interruption[0], message=interruption[1])
            else:
                self.repos.jobs.cancel(job_id)
            logger.info("Job %s cancelled during a subprocess", job_id)
        except JobFailed as exc:
            interruption = self._take_interruption(job_id)
            if interruption is not None:
                self.repos.jobs.fail(job_id, code=interruption[0], message=interruption[1])
            elif exc.code == "cancelled" or context.should_cancel():
                self.repos.jobs.cancel(job_id)
            elif not self._park_if_retryable(job, exc):
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

    # -- liveness ----------------------------------------------------------

    def _take_interruption(self, job_id: str) -> tuple[str, str] | None:
        with self._lock:
            return self._interruptions.pop(job_id, None)

    def _mark_interrupted(self, job_id: str, code: str, message: str) -> None:
        with self._lock:
            if job_id in self._interruptions:
                return
            self._interruptions[job_id] = (code, message)
            is_current = self._current_job_id == job_id
            cancel = self._current_cancel
        if is_current and cancel is not None:
            cancel.set()
        try:
            # Persist the truth immediately. A worker process that is genuinely
            # wedged cannot be relied on to settle its own row.
            if self.repos.jobs.get(job_id)["state"] == "running":
                self.repos.jobs.fail(job_id, code=code, message=message)
        except Exception:
            logger.exception("Could not record the interruption for job %s", job_id)
        logger.warning("Job %s marked %s", job_id, code)

    def _watch(self) -> None:
        """Detect a dead worker loop without confusing slow work with a stall.

        Only jobs that opted into a deadline (the production pipeline) are
        monitored. A job that is slow but still writes progress keeps a fresh
        heartbeat and is never killed for exceeding an estimate.
        """
        interval = min(5.0, max(1.0, float(self.settings.pipeline_stall_seconds) / 6))
        while not self._stop.wait(interval):
            try:
                for job in self.repos.jobs.past_deadline():
                    self._mark_interrupted(
                        job["id"],
                        "deadline_exceeded",
                        "This job reached its configured maximum runtime. Completed work was kept.",
                    )
                for job in self.repos.jobs.stale_running(
                    older_than_seconds=self.settings.pipeline_stall_seconds
                ):
                    if not job.get("deadline_at"):
                        continue
                    self._mark_interrupted(
                        job["id"],
                        "stalled",
                        f"No progress was reported for {self.settings.pipeline_stall_seconds} seconds. "
                        "The job was stopped; completed work and previous exports were kept.",
                    )
            except Exception:
                logger.exception("Job watchdog failed to inspect running jobs")

    def _park_if_retryable(self, job: dict, exc: JobFailed) -> bool:
        """Wait out a transient failure without ending the job.

        Returns True when the job was parked, so the caller leaves it alone.
        """
        if not getattr(exc, "retryable", False):
            return False
        attempt = int(job.get("attempts") or 1)
        if attempt > AUTOMATIC_RETRY_LIMIT:
            return False
        delay = min(
            RETRY_DELAY_BASE_SECONDS * 2 ** (attempt - 1), RETRY_DELAY_MAX_SECONDS
        )
        self.repos.jobs.retry_later(
            job["id"],
            delay_seconds=delay,
            message=f"{exc.message} Resuming automatically in {_describe_delay(delay)}.",
        )
        logger.warning(
            "Job %s parked for %ss after a transient failure", job["id"], delay
        )
        return True
