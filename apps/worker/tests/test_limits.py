"""Timestamp and span validation."""

from __future__ import annotations

import pytest

from clipforge.security.limits import MAX_CLIP_MS, clamp_timestamp, validate_span


# given a source that is two minutes long
DURATION_MS = 120_000


def test_accepts_an_ordered_span_inside_the_source():
    # given / when
    start, end = validate_span(10_000, 40_000, DURATION_MS)

    # then
    assert (start, end) == (10_000, 40_000)


def test_rounds_fractional_input_to_milliseconds():
    # given / when
    start, end = validate_span(10_000.4, 40_000.6, DURATION_MS)

    # then
    assert (start, end) == (10_000, 40_001)


def test_rejects_a_negative_start():
    # given / when / then
    with pytest.raises(ValueError, match="negative"):
        validate_span(-1, 30_000, DURATION_MS)


def test_rejects_an_end_before_the_start():
    # given / when / then
    with pytest.raises(ValueError, match="after the start"):
        validate_span(30_000, 30_000, DURATION_MS)


def test_rejects_a_span_past_the_source():
    # given / when / then
    with pytest.raises(ValueError, match="past the end"):
        validate_span(100_000, DURATION_MS + 1, DURATION_MS)


def test_rejects_a_span_shorter_than_the_minimum():
    # given / when / then
    with pytest.raises(ValueError, match="at least 2 seconds"):
        validate_span(10_000, 11_000, DURATION_MS)


def test_rejects_a_span_longer_than_the_maximum():
    # given / when / then
    with pytest.raises(ValueError, match="longer than"):
        validate_span(0, MAX_CLIP_MS + 1, MAX_CLIP_MS + 10_000)


def test_clamp_timestamp_keeps_values_inside_the_source():
    # given / when / then
    assert clamp_timestamp(-40, DURATION_MS) == 0
    assert clamp_timestamp(50, DURATION_MS) == 50
    assert clamp_timestamp(DURATION_MS + 500, DURATION_MS) == DURATION_MS
