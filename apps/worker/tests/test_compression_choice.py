"""Where a render's compression choice comes from.

A saved project default must survive untouched, an explicit choice in the render
request must win, and anything the encoder would reject must come back as a
sentence rather than a stack trace.
"""

from __future__ import annotations

import pytest

from clipforge.api.models import RenderEstimateRequest, RenderRequest
from clipforge.api.routes_renders import _compression_payload, _resolve_compression
from clipforge.errors import ValidationFailed
from clipforge.rendering.encode import estimate_table


def _request(**overrides) -> RenderRequest:
    return RenderRequest(candidate_id="candidate-1", **overrides)


def test_without_any_choice_the_balanced_preset_applies():
    # given
    payload = _request()

    # when
    settings = _compression_payload({}, payload, payload.model_fields_set)

    # then
    assert settings.preset == "balanced"


def test_a_saved_project_default_is_used_when_the_request_is_silent():
    # given
    payload = _request()
    project_settings = {"export": {"compression": {"preset": "smallest"}}}

    # when
    settings = _compression_payload(project_settings, payload, payload.model_fields_set)

    # then
    assert settings.preset == "smallest"
    assert settings.crf == 28


def test_an_explicit_request_beats_the_saved_default():
    # given
    payload = _request(compression={"preset": "higher"})
    project_settings = {"export": {"compression": {"preset": "smallest"}}}

    # when
    settings = _compression_payload(project_settings, payload, payload.model_fields_set)

    # then
    assert settings.preset == "higher"


def test_an_explicit_null_clears_the_saved_default():
    # given: the user chose to stop overriding the project preference
    payload = _request(compression=None)

    # when
    settings = _compression_payload(
        {"export": {"compression": {"preset": "smallest"}}}, payload, payload.model_fields_set
    )

    # then
    assert settings.preset == "balanced"


@pytest.mark.parametrize(
    "raw",
    [
        {"preset": "custom", "crf": 99},
        {"preset": "custom", "resolution_cap": 10},
        {"preset": "custom", "audio_bitrate_kbps": 1000},
        {"preset": "nonsense"},
        "nonsense",
        [1, 2, 3],
    ],
)
def test_invalid_compression_is_explained_in_a_sentence(raw):
    # when / then
    with pytest.raises(ValidationFailed) as raised:
        _resolve_compression(raw)
    assert raised.value.code == "invalid_compression"
    assert raised.value.message
    assert "Traceback" not in raised.value.message


def test_a_string_preset_is_accepted():
    # when
    settings = _resolve_compression("smallest")

    # then
    assert settings.preset == "smallest"


def test_the_estimate_request_carries_the_same_choice():
    # given
    payload = RenderEstimateRequest(duration_ms=30_000, compression={"preset": "higher"})

    # when
    settings = _resolve_compression(payload.compression)

    # then
    assert settings.preset == "higher"


def test_estimate_dimensions_follow_the_resolution_cap():
    # given: landscape 1080p, smallest preset
    settings = _resolve_compression("smallest")

    # when
    width, height = settings.dimensions_for((1920, 1080))

    # then
    assert (width, height) == (1280, 720)
    assert settings.estimate_bytes(dimensions=(1920, 1080), fps=30, duration_ms=10_000) > 0


def test_estimate_table_marks_every_row_approximate():
    # when
    rows = estimate_table()

    # then
    assert {row["id"] for row in rows} == {"smallest", "balanced", "higher"}
    assert all(row["resolution_cap"] for row in rows)
    assert all(row["audio_bitrate_kbps"] for row in rows)
