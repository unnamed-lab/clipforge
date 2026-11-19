"""A dropped connection parks a job instead of ending it.

The downloader already resumes its own bytes (see ``test_importer.py``); these
tests cover the queue side: what the database records while waiting, how a
restart treats an interrupted download, and how the retry budget ends.
"""

from __future__ import annotations

import pytest

from clipforge.errors import JobFailed
from clipforge.jobs import queue as queue_module
from clipforge.jobs.queue import JobQueue
from clipforge.storage.local import LocalStorage


@pytest.fixture()
def storage(settings) -> LocalStorage:
    return LocalStorage(settings.data_dir)


def _queue(repos, settings, storage, handler) -> JobQueue:
    return JobQueue(repos, settings, {"url_import": handler}, storage)


def _create(repos, project, job_type: str = "url_import", state: str = "queued") -> dict:
    return repos.jobs.create(
        project_id=project["id"],
        job_type=job_type,
        payload={"url": "https://example.com/video"},
        state=state,
    )


def _retryable(message: str = "The connection dropped during the download.") -> JobFailed:
    failure = JobFailed("url_download_failed", message)
    failure.retryable = True
    return failure


def test_parking_keeps_a_job_out_of_the_queue_until_it_is_due(repos, project):
    # given: a claimed download that hit a transient failure
    job = _create(repos, project)
    claimed = repos.jobs.claim_next()
    assert claimed["id"] == job["id"]

    # when
    parked = repos.jobs.retry_later(
        claimed["id"], delay_seconds=600, message="Resuming automatically in 10 minutes."
    )

    # then: it is alive, but not claimable yet
    assert parked["state"] == "waiting"
    assert parked["next_attempt_at"] is not None
    assert parked["error_code"] is None
    assert repos.jobs.claim_next() is None

    # when: its time arrives
    repos.jobs.retry_later(claimed["id"], delay_seconds=0, message="Resuming now.")
    again = repos.jobs.claim_next()

    # then: the same job runs again, one attempt further on
    assert again is not None
    assert again["id"] == claimed["id"]
    assert again["attempts"] == 2
    assert again["next_attempt_at"] is None


def test_a_parked_job_still_blocks_a_second_import(repos, project):
    # given
    job = _create(repos, project)
    repos.jobs.claim_next()
    repos.jobs.retry_later(job["id"], delay_seconds=600, message="Waiting.")

    # when
    active = repos.jobs.active_for(project["id"], "url_import")

    # then: the UI must not start a duplicate download alongside it
    assert active is not None
    assert active["id"] == job["id"]


def test_a_transient_failure_parks_the_job_instead_of_failing_it(
    repos, settings, storage, project
):
    # given: a handler that reports a recoverable failure
    def handler(context):
        raise _retryable()

    queue = _queue(repos, settings, storage, handler)
    job = _create(repos, project)

    # when
    queue._run(repos.jobs.claim_next())

    # then: the job waits instead of ending
    parked = repos.jobs.get(job["id"])
    assert parked["state"] == "waiting"
    assert parked["stage"] == "waiting"
    assert "Resuming automatically" in parked["message"]
    assert parked["next_attempt_at"] is not None
    assert parked["error_code"] is None
    assert parked["finished_at"] is None


def test_a_parked_job_finishes_on_its_next_attempt(repos, settings, storage, project):
    # given: the first attempt fails transiently, the second succeeds
    calls = {"count": 0}

    def handler(context):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _retryable()
        return {"ok": True}

    queue = _queue(repos, settings, storage, handler)
    job = _create(repos, project)
    queue._run(repos.jobs.claim_next())
    assert repos.jobs.get(job["id"])["state"] == "waiting"

    # when: the wait elapses and the queue picks the job up again
    repos.jobs.retry_later(job["id"], delay_seconds=0, message="Resuming now.")
    queue._run(repos.jobs.claim_next())

    # then
    finished = repos.jobs.get(job["id"])
    assert finished["state"] == "completed"
    assert finished["attempts"] == 2
    assert finished["result"] == {"ok": True}


def test_a_failure_that_never_heals_eventually_ends(
    repos, settings, storage, project, monkeypatch
):
    # given: a source that is not coming back
    monkeypatch.setattr(queue_module, "AUTOMATIC_RETRY_LIMIT", 2)

    def handler(context):
        raise _retryable()

    queue = _queue(repos, settings, storage, handler)
    job = _create(repos, project)

    # when: every attempt is made, waiting out each delay
    for _ in range(3):
        queue._run(repos.jobs.claim_next())
        if repos.jobs.get(job["id"])["state"] == "waiting":
            repos.jobs.retry_later(job["id"], delay_seconds=0, message="Resuming now.")

    # then: the budget runs out and the job fails with its real reason
    final = repos.jobs.get(job["id"])
    assert final["state"] == "failed"
    assert final["error_code"] == "url_download_failed"
    assert final["finished_at"] is not None


def test_a_parked_job_can_be_cancelled_without_waiting(repos, settings, storage, project):
    # given
    def handler(context):
        raise _retryable()

    queue = _queue(repos, settings, storage, handler)
    job = _create(repos, project)
    queue._run(repos.jobs.claim_next())

    # when
    queue.cancel(job["id"])

    # then
    assert repos.jobs.get(job["id"])["state"] == "cancelled"
    assert repos.jobs.claim_next() is None


def test_a_manual_retry_clears_the_wait(repos, project):
    # given
    job = _create(repos, project)
    repos.jobs.claim_next()
    repos.jobs.retry_later(job["id"], delay_seconds=600, message="Waiting.")

    # when
    requeued = repos.jobs.requeue(job["id"])

    # then
    assert requeued["state"] == "queued"
    assert requeued["next_attempt_at"] is None
    assert repos.jobs.claim_next()["id"] == job["id"]


def test_a_restart_resumes_downloads_and_fails_everything_else(repos, project):
    # given: what a crashed worker left behind
    download = _create(repos, project, job_type="url_import", state="running")
    render = _create(repos, project, job_type="render", state="running")
    cancelled = _create(repos, project, job_type="url_import", state="running")
    repos.jobs.request_cancel(cancelled["id"])

    # when
    counts = repos.jobs.recover_interrupted()

    # then
    assert counts == {"queued": 1, "failed": 1, "cancelled": 1}
    assert repos.jobs.get(download["id"])["state"] == "queued"
    assert repos.jobs.get(render["id"])["state"] == "failed"
    assert repos.jobs.get(render["id"])["error_code"] == "worker_restarted"
    assert repos.jobs.get(cancelled["id"])["state"] == "cancelled"
