"""Compression presets, validation and size estimates.

Encoder probing is monkeypatched per test so the result never depends on which
FFmpeg build happens to be installed.
"""

from __future__ import annotations

import pytest

from clipforge.rendering import encode
from clipforge.rendering.encode import (
    EncodeSettings,
    bpp_for_crf,
    normalise_preset,
    resolve_encode,
    validate_encoders,
)


def test_defaults_to_the_balanced_preset():
    # when
    resolved = resolve_encode(None)

    # then
    assert resolved.preset == "balanced"
    assert resolved.crf == 23
    assert resolved.audio_bitrate_kbps == 128
    assert resolved.resolution_cap == 1080


@pytest.mark.parametrize(
    ("preset", "crf", "audio", "width", "fps"),
    [
        ("smallest", 28, 96, 720, 30),
        ("balanced", 23, 128, 1080, 30),
        ("higher", 18, 192, 1920, None),
    ],
)
def test_presets_are_distinct_and_ordered(preset, crf, audio, width, fps):
    # when
    resolved = resolve_encode(preset)

    # then
    assert (resolved.crf, resolved.audio_bitrate_kbps, resolved.resolution_cap, resolved.max_fps) == (
        crf,
        audio,
        width,
        fps,
    )


def test_aliases_map_onto_the_four_supported_presets():
    assert normalise_preset("small") == "smallest"
    assert normalise_preset("best") == "higher"
    assert normalise_preset("quality") == "higher"


def test_unknown_preset_names_every_choice():
    # when / then
    with pytest.raises(ValueError) as raised:
        normalise_preset("tiny")
    message = str(raised.value)
    assert "'tiny'" in message
    assert "smallest" in message and "custom" in message


def test_custom_accepts_valid_values():
    # when
    resolved = resolve_encode(
        "custom", {"crf": 20, "audio_bitrate_kbps": 160, "resolution_cap": 1440, "max_fps": 24}
    )

    # then
    assert resolved.preset == "custom"
    assert resolved.crf == 20
    assert resolved.resolution_cap == 1440


def test_custom_still_accepts_the_older_width_key():
    # given: a saved spec written before the field was renamed
    resolved = resolve_encode("custom", {"max_width": 1440})

    # then
    assert resolved.resolution_cap == 1440


@pytest.mark.parametrize(
    "custom",
    [
        {"crf": 60},
        {"crf": -1},
        {"audio_bitrate_kbps": 10},
        {"audio_bitrate_kbps": 999},
        {"resolution_cap": 100},
        {"max_fps": 5},
    ],
)
def test_custom_rejects_values_outside_the_encoder_range(custom):
    # when / then
    with pytest.raises(ValueError):
        resolve_encode("custom", custom)


def test_custom_error_names_the_offending_field():
    # when / then
    with pytest.raises(ValueError) as raised:
        resolve_encode("custom", {"crf": 99})
    assert "crf" in str(raised.value).lower()


def test_custom_ignores_a_client_supplied_preset_field():
    # when
    resolved = resolve_encode("custom", {"preset": "balanced", "crf": 30})

    # then
    assert resolved.preset == "custom"
    assert resolved.crf == 30


def test_resolution_cap_scales_the_short_edge_and_keeps_even_dimensions():
    # given: a 16:9 1080p frame in landscape
    settings = EncodeSettings(preset="custom", resolution_cap=720)

    # when
    width, height = settings.dimensions_for((1920, 1080))

    # then: "720p" caps the short edge, so the frame becomes 1280x720
    assert (width, height) == (1280, 720)
    assert width % 2 == 0 and height % 2 == 0
    assert abs(width / height - 16 / 9) < 0.01


def test_resolution_cap_treats_portrait_the_same_way():
    # given
    settings = EncodeSettings(preset="custom", resolution_cap=720)

    # when
    width, height = settings.dimensions_for((1080, 1920))

    # then: the short edge is the width in portrait, so 720x1280
    assert (width, height) == (720, 1280)


def test_resolution_cap_does_not_touch_a_frame_already_below_it():
    # given
    settings = EncodeSettings(preset="balanced", resolution_cap=1080)

    # when / then: both orientations that are already 1080p pass through
    assert settings.dimensions_for((1920, 1080)) == (1920, 1080)
    assert settings.dimensions_for((1080, 1920)) == (1080, 1920)


def test_estimate_grows_with_duration_and_bitrate():
    # given
    settings = resolve_encode("balanced")
    frame = (1080, 1920)

    # when
    short = settings.estimate_bytes(dimensions=frame, fps=30, duration_ms=10_000)
    long = settings.estimate_bytes(dimensions=frame, fps=30, duration_ms=60_000)
    smaller = resolve_encode("smallest").estimate_bytes(dimensions=frame, fps=30, duration_ms=60_000)

    # then
    assert 0 < short < long
    assert smaller < long


def test_estimate_never_returns_a_negative_size_for_an_empty_clip():
    # when
    estimate = resolve_encode("balanced").estimate_bytes(dimensions=(720, 1280), fps=30, duration_ms=0)

    # then
    assert estimate == 0


def test_bpp_falls_as_crf_rises():
    # when
    sharp = bpp_for_crf(18)
    soft = bpp_for_crf(28)

    # then
    assert sharp > soft > 0


def test_estimate_table_describes_every_preset():
    # when
    rows = encode.estimate_table()

    # then
    assert [row["id"] for row in rows] == ["smallest", "balanced", "higher"]
    assert all(row["label"] and row["note"] for row in rows)


def test_missing_codec_falls_back_to_h264_with_a_note(monkeypatch):
    # given: HEVC is not in this build, H.264 is
    monkeypatch.setattr(encode, "encoder_available", lambda codec: codec == "h264")
    settings = EncodeSettings(preset="higher", codec="hevc")

    # when
    resolved, note = validate_encoders(settings)

    # then
    assert resolved.codec == "h264"
    assert note is not None and "H.264" in note


def test_available_codec_is_left_alone(monkeypatch):
    # given
    monkeypatch.setattr(encode, "encoder_available", lambda codec: True)
    settings = EncodeSettings(preset="higher", codec="hevc")

    # when
    resolved, note = validate_encoders(settings)

    # then
    assert resolved.codec == "hevc"
    assert note is None


def test_unprobeable_ffmpeg_is_not_treated_as_missing(monkeypatch):
    # given: no encoder list at all, so absence is unproven
    monkeypatch.setattr(encode, "available_encoders", lambda: frozenset())

    # then
    assert encode.encoder_available("h264") is True
