"""Read model for the discovery screen.

Counts are kept separate on purpose. A discovered moment, a displayed moment, a
selected moment and an exported clip are four different things; adding them
together is how a UI ends up claiming work that never happened.
"""

from __future__ import annotations

from clipforge.db.repositories import Repositories
from clipforge.discovery.coverage import build_coverage, coverage_summary

ACTIVE_JOB_STATES = {"queued", "running", "waiting"}


def build_discovery_state(repos: Repositories, project_id: str, source: dict | None = None) -> dict:
    source = source if source is not None else repos.sources.find_optional(project_id)
    runs = repos.runs.list_for_project(project_id)
    candidates = repos.candidates.list_for_project(project_id)
    renders = repos.renders.list_for_project(project_id)

    converted_ids = {
        render.get("candidate_id")
        for render in renders
        if render.get("status") == "completed" and render.get("candidate_id")
    }
    accepted = sum(1 for candidate in candidates if candidate.get("decision") == "accepted")

    batches = []
    for run in runs:
        run_candidates = [candidate for candidate in candidates if candidate.get("analysis_run_id") == run["id"]]
        discovery = run.get("discovery") or {}
        batches.append(
            {
                "id": run["id"],
                "kind": run.get("kind") or "initial",
                "status": run.get("status"),
                "created_at": run.get("created_at"),
                "candidate_count": len(run_candidates),
                "requested": discovery.get("requested"),
                "found": discovery.get("found"),
                "reason": discovery.get("reason"),
                "exhausted": discovery.get("exhausted"),
                "criteria": discovery.get("criteria") or {},
                "criteria_key": discovery.get("criteria_key"),
                "coverage": discovery.get("coverage") or {},
                "warnings": discovery.get("warnings") or run.get("warnings") or [],
                "converted": sum(1 for candidate in run_candidates if candidate["id"] in converted_ids),
            }
        )

    duration_ms = int(source["duration_ms"]) if source else 0
    coverage = {}
    if runs:
        coverage = (runs[-1].get("discovery") or {}).get("coverage") or {}
    if not coverage and duration_ms:
        coverage = coverage_summary(build_coverage(duration_ms, candidates))

    active_job = repos.jobs.active_for(project_id, "discovery")
    if active_job is None:
        active_job = repos.jobs.active_for(project_id, "analysis")

    last_additional = next(
        (batch for batch in reversed(batches) if batch["kind"] == "additional"),
        None,
    )
    return {
        "batches": batches,
        "counts": {
            "total_discovered": len(candidates),
            "accepted": accepted,
            "converted": len([candidate for candidate in candidates if candidate["id"] in converted_ids]),
            "batches": len(batches),
        },
        "coverage": coverage,
        "active_job": active_job,
        "generating": bool(active_job and active_job.get("state") in ACTIVE_JOB_STATES),
        "last_result": last_additional,
    }
