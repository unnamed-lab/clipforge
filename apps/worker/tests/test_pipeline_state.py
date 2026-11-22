"""Durable run state, checkpoint reuse and run idempotency."""

from __future__ import annotations

from dataclasses import replace

import pytest

from clipforge.errors import Conflict
from clipforge.pipeline.orchestrator import STAGES, _final_qa, create_run, pipeline_state
from clipforge.rendering.spec import build_spec


def _request() -> dict:
    return {"clip_count": 2, "allow_extra_discovery": False}


def test_failed_stages_are_reset_but_completed_checkpoints_survive(settings, repos, project):
    # given
    run = create_run(
        repos,
        settings,
        project_id=project["id"],
        request=_request(),
        provider={"provider": "none"},
    )
    repos.project_stages.start(run["id"], "SOURCE_VALIDATION")
    repos.project_stages.complete(run["id"], "SOURCE_VALIDATION", {"source_id": "s"})
    repos.project_stages.start(run["id"], "SOURCE_ANALYSIS")
    repos.project_stages.fail(run["id"], "SOURCE_ANALYSIS", code="boom", message="failed")

    # when
    repos.project_stages.reset_for_retry(run["id"])

    # then
    stages = {stage["name"]: stage for stage in repos.project_stages.list(run["id"])}
    assert stages["SOURCE_VALIDATION"]["status"] == "completed"
    assert stages["SOURCE_VALIDATION"]["output"] == {"source_id": "s"}
    assert stages["SOURCE_ANALYSIS"]["status"] == "pending"
    assert stages["SOURCE_ANALYSIS"]["error_code"] is None


def test_creating_a_run_while_one_is_active_returns_the_same_run(settings, repos, project):
    # given
    first = create_run(
        repos, settings, project_id=project["id"], request=_request(), provider={}
    )

    # when
    second = create_run(
        repos, settings, project_id=project["id"], request=_request(), provider={}
    )

    # then
    assert second["id"] == first["id"]
    assert len(repos.project_runs.list_for_project(project["id"])) == 1


def test_a_finished_run_does_not_block_a_new_one(settings, repos, project):
    # given
    first = create_run(
        repos, settings, project_id=project["id"], request=_request(), provider={}
    )
    repos.project_runs.set_status(first["id"], "completed", message="done")

    # when
    second = create_run(
        repos, settings, project_id=project["id"], request=_request(), provider={}
    )

    # then
    assert second["id"] != first["id"]
    assert len(repos.project_runs.list_for_project(project["id"])) == 2


def test_pipeline_state_exposes_real_stages_and_no_fake_percentages(settings, repos, project):
    # given
    run = create_run(
        repos, settings, project_id=project["id"], request=_request(), provider={}
    )
    repos.project_runs.start(run["id"])
    repos.project_stages.start(run["id"], "SOURCE_VALIDATION")
    repos.project_stages.progress(run["id"], "SOURCE_VALIDATION", progress=42, message="probing")

    # when
    state = pipeline_state(repos, project["id"])

    # then
    assert [stage["name"] for stage in state["stages"]][: len(STAGES)] == STAGES
    validation = next(stage for stage in state["stages"] if stage["name"] == "SOURCE_VALIDATION")
    assert validation["status"] == "running"
    assert validation["progress"] == 42
    assert validation["message"] == "probing"
    assert state["run"]["status"] == "running"
    assert state["counts"]["total"] == 0
    assert state["manifest"]["project"]["id"] == project["id"]


def test_cancelling_a_run_is_recorded_and_finished_runs_refuse_cancel(settings, repos, project):
    # given
    run = create_run(
        repos, settings, project_id=project["id"], request=_request(), provider={}
    )

    # when
    requested = repos.project_runs.request_cancel(run["id"])

    # then
    assert requested["cancel_requested"] is True
    repos.project_runs.set_status(run["id"], "cancelled", message="Cancelled")
    with pytest.raises(Conflict):
        repos.project_runs.request_cancel(run["id"])


def test_repair_limit_is_configurable_and_not_hardcoded(settings):
    configured = replace(settings, autoproduce_repair_limit=1)
    assert configured.autoproduce_repair_limit == 1
    assert settings.autoproduce_repair_limit == 2


def test_a_render_without_gate_evidence_is_not_reported_as_complete(settings, repos, project):
    # given a clip marked passed whose render has no persisted checks
    run = create_run(
        repos, settings, project_id=project["id"], request=_request(), provider={}
    )
    repos.project_stages.ensure(run["id"], STAGES)
    clips = repos.project_clips.replace(
        run["id"], project["id"], [{"candidate_id": None, "strategy": "chronological"}]
    )
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 4_000}],
        aspect="9:16",
        resolution=720,
        captions_enabled=False,
    )
    render = repos.renders.create(
        project_id=project["id"],
        job_id=None,
        candidate_id=None,
        spec=spec.model_dump(),
        status="completed",
        spec_hash="hash",
        source_identity="source",
    )
    repos.project_clips.update(clips[0]["id"], status="passed", render_id=render["id"])

    # when
    result = _final_qa(
        _ctx(repos, settings, project["id"]),
        run["id"],
    )

    # then
    assert result["passed"] == 0
    assert result["needs_review"] == 1
    assert repos.project_clips.get(clips[0]["id"])["status"] == "needs_review"
    assert repos.project_runs.get(run["id"])["status"] == "awaiting_review"


def test_a_clip_failed_in_configuration_keeps_the_run_partial(settings, repos, project):
    # given one failed clip and no valid exports
    run = create_run(
        repos, settings, project_id=project["id"], request=_request(), provider={}
    )
    clips = repos.project_clips.replace(
        run["id"], project["id"], [{"candidate_id": None, "strategy": "chronological"}]
    )
    repos.project_clips.update(clips[0]["id"], status="failed")

    # when
    result = _final_qa(_ctx(repos, settings, project["id"]), run["id"])

    # then
    assert result["failed"] == 1
    assert repos.project_runs.get(run["id"])["status"] == "failed"


def _ctx(repos, settings, project_id: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        repos=repos,
        settings=settings,
        job={"id": "job-test", "project_id": project_id},
        cancel_event=None,
        should_cancel=lambda: False,
        on_stage=lambda stage, progress, message: None,
        storage=None,
    )
