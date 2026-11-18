"""Candidate serialisation must match the shared API schema."""

from __future__ import annotations

from clipforge.db.repositories import serialise_candidate


def _row(boundary_json: str | None, packaging_json: str | None = None) -> dict:
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
        "packaging_json": packaging_json,
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


def test_candidate_without_stored_packaging_returns_schema_defaults():
    # given a row written before packaging existed
    row = _row(None, packaging_json=None)

    # when
    candidate = serialise_candidate(row)

    # then
    assert candidate["packaging"]["saved_trends"] == []
    assert candidate["packaging"]["dismissed_trend_ids"] == []
    assert candidate["packaging"]["manual_trends"] == []


def test_candidate_with_legacy_packaging_returns_newer_list_fields():
    # given a document stored before saved trends existed
    row = _row(None, packaging_json='{"platform": "tiktok", "metadata": {"titles": []}}')

    # when
    candidate = serialise_candidate(row)

    # then
    assert candidate["packaging"]["platform"] == "tiktok"
    assert candidate["packaging"]["saved_trends"] == []
