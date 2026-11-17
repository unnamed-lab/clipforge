"""Candidate serialisation must match the shared API schema."""

from __future__ import annotations

from clipforge.db.repositories import serialise_candidate


def _row(boundary_json: str | None) -> dict:
    return {
        "id": "candidate-1",
        "start_ms": 0,
        "end_ms": 10_000,
        "overall": 70.0,
        "scores_json": "{}",
        "warnings_json": "[]",
        "hook_json": "{}",
        "brief_json": "{}",
        "boundary_json": boundary_json,
    }


def test_candidate_without_a_stored_boundary_returns_empty_lists():
    # given a row written before ending reviews existed
    row = _row(None)

    # when
    candidate = serialise_candidate(row)

    # then
    assert candidate["boundary"]["evidence"] == []
    assert candidate["boundary"]["alternatives"] == []


def test_candidate_with_an_empty_boundary_object_returns_empty_lists():
    # given a row whose boundary column holds an empty object
    row = _row("{}")

    # when
    candidate = serialise_candidate(row)

    # then
    assert candidate["boundary"]["evidence"] == []
    assert candidate["boundary"]["alternatives"] == []


def test_candidate_with_a_reviewed_boundary_keeps_its_evidence():
    # given a row with a completed ending review
    row = _row('{"suspicious": true, "evidence": ["Speech continues after the end."], "alternatives": []}')

    # when
    candidate = serialise_candidate(row)

    # then
    assert candidate["boundary"]["evidence"] == ["Speech continues after the end."]
    assert candidate["boundary"]["suspicious"] is True
