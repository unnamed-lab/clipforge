"""Download quality and processing-mode policy.

Every case here is a pure decision about metadata an extractor already
returned, so these tests need no network, no yt-dlp and no media file.
"""

from __future__ import annotations

import pytest

from clipforge.media.policy import (
    MAX_PROXY_HEIGHT,
    MIN_PROXY_HEIGHT,
    PolicyError,
    available_qualities,
    choose_format,
    collect_formats,
    download_options,
    estimate_display,
    normalise_quality,
    resolve_plan,
)


# given: a source that publishes 360p and 720p video-only streams plus one audio.
def _info(**overrides) -> dict:
    info = {
        "title": "A talk",
        "duration": 120.0,
        "formats": [
            {
                "format_id": "134",
                "height": 360,
                "width": 640,
                "fps": 30,
                "vcodec": "avc1.4d401e",
                "acodec": "none",
                "ext": "mp4",
                "tbr": 500,
                "filesize": 6_000_000,
            },
            {
                "format_id": "136",
                "height": 720,
                "width": 1280,
                "fps": 30,
                "vcodec": "avc1.4d401f",
                "acodec": "none",
                "ext": "mp4",
                "tbr": 1_800,
                "filesize": 20_000_000,
            },
            {
                "format_id": "140",
                "height": None,
                "width": None,
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "ext": "m4a",
                "abr": 128,
                "filesize": 2_000_000,
            },
        ],
    }
    info.update(overrides)
    return info


# --------------------------------------------------------------------------
# Processing modes
# --------------------------------------------------------------------------


def test_balanced_mode_is_the_default_and_downloads_720p():
    # when
    plan = resolve_plan()

    # then
    assert plan.mode == "balanced"
    assert plan.quality == "720p"
    assert plan.proxy_height == 480
    assert plan.scene_fps == 1.0
    assert plan.transcription_quality == "balanced"


def test_fast_mode_downloads_less_and_samples_less():
    # when
    plan = resolve_plan(mode="fast")

    # then
    assert plan.quality == "480p"
    assert plan.proxy_height == 360
    assert plan.scene_fps == 0.5
    # The user must be told what they give up in fast mode.
    assert any("text" in warning.lower() for warning in plan.warnings)


def test_high_mode_keeps_more_detail():
    # when
    plan = resolve_plan(mode="high")

    # then
    assert plan.quality == "1080p"
    assert plan.proxy_height == 720
    assert plan.transcription_quality == "best"


def test_unknown_mode_is_rejected_with_a_stable_code():
    # when / then
    with pytest.raises(PolicyError) as raised:
        resolve_plan(mode="turbo")
    assert raised.value.code == "invalid_processing_mode"


def test_custom_mode_needs_an_explicit_quality():
    # when / then
    with pytest.raises(PolicyError) as raised:
        resolve_plan(mode="custom")
    assert raised.value.code == "quality_required"


def test_custom_mode_honours_an_explicit_quality_and_proxy_height():
    # when
    plan = resolve_plan(mode="custom", quality="480p", proxy_height=240)

    # then
    assert plan.quality == "480p"
    assert plan.proxy_height == 240


def test_audio_only_is_rejected_outside_custom_mode():
    # when / then
    with pytest.raises(PolicyError) as raised:
        resolve_plan(mode="balanced", quality="audio")
    assert raised.value.code == "audio_only_not_allowed"


def test_audio_only_plan_downloads_no_proxy():
    # when
    plan = resolve_plan(mode="custom", quality="audio", proxy_height=480)

    # then
    assert plan.quality == "audio"
    assert plan.proxy_height is None
    assert plan.download_proxy is False


# --------------------------------------------------------------------------
# Proxy decisions
# --------------------------------------------------------------------------


def test_proxy_is_built_when_the_source_is_meaningfully_taller():
    # when
    plan = resolve_plan(mode="balanced", source_height=1080)

    # then
    assert plan.proxy_height == 480
    assert plan.download_proxy is True


def test_proxy_is_skipped_when_the_source_is_already_small():
    # when
    plan = resolve_plan(mode="balanced", source_height=480)

    # then
    assert plan.download_proxy is False
    assert any("proxy would add nothing" in warning for warning in plan.warnings)


def test_proxy_height_outside_the_supported_range_is_rejected():
    # when / then
    with pytest.raises(PolicyError) as raised:
        resolve_plan(mode="custom", quality="720p", proxy_height=MIN_PROXY_HEIGHT - 24)
    assert raised.value.code == "invalid_proxy_height"

    with pytest.raises(PolicyError):
        resolve_plan(mode="custom", quality="720p", proxy_height=MAX_PROXY_HEIGHT + 24)


def test_proxy_height_zero_disables_the_proxy():
    # when
    plan = resolve_plan(mode="custom", quality="720p", proxy_height=0, source_height=1080)

    # then
    assert plan.proxy_height is None
    assert plan.download_proxy is False


# --------------------------------------------------------------------------
# Format selection
# --------------------------------------------------------------------------


def test_quality_aliases_are_accepted():
    assert normalise_quality("4k") == "2160p"
    assert normalise_quality("1080") == "1080p"
    assert normalise_quality("audio-only") == "audio"


def test_unpublished_quality_falls_back_to_the_closest_published_height():
    # when
    choice = choose_format(_info(), "1080p")

    # then
    assert choice.requested_quality == "1080p"
    assert choice.height == 720
    # The explanation must say the request was not met.
    assert "not offered" in choice.explanation
    assert any("1080p is unavailable" in warning for warning in choice.warnings)


def test_exact_quality_is_reported_as_available():
    # when
    choice = choose_format(_info(), "720p")

    # then
    assert choice.height == 720
    assert choice.quality == "720p"
    assert "available" in choice.explanation


def test_lowest_and_best_pick_the_extremes():
    # when
    lowest = choose_format(_info(), "lowest")
    best = choose_format(_info(), "best")

    # then
    assert lowest.height == 360
    assert best.height == 720


def test_video_only_stream_is_paired_with_the_best_audio():
    # when
    choice = choose_format(_info(), "720p")

    # then
    assert choice.video is not None and choice.video.format_id == "136"
    assert choice.audio is not None and choice.audio.format_id == "140"
    # Only the required streams are fetched.
    assert choice.selector == "136+140"


def test_progressive_stream_is_used_when_no_separate_audio_exists():
    # given: a single stream that already carries audio
    info = _info(
        formats=[
            {
                "format_id": "18",
                "height": 360,
                "width": 640,
                "fps": 30,
                "vcodec": "avc1.42001e",
                "acodec": "mp4a.40.2",
                "ext": "mp4",
                "tbr": 700,
            }
        ]
    )

    # when
    choice = choose_format(info, "360p")

    # then
    assert choice.selector == "18"
    assert choice.audio is None


def test_source_without_audio_warns_that_speech_is_unavailable():
    # given
    info = _info(
        formats=[
            {
                "format_id": "137",
                "height": 1080,
                "width": 1920,
                "fps": 30,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "ext": "mp4",
                "tbr": 4_000,
            }
        ]
    )

    # when
    choice = choose_format(info, "1080p")

    # then
    assert choice.audio is None
    assert any("no audio stream" in warning for warning in choice.warnings)


def test_audio_only_choice_has_no_video_and_explains_the_trade_off():
    # when
    choice = choose_format(_info(), "audio")

    # then
    assert choice.has_video is False
    assert choice.video is None
    assert choice.audio is not None
    assert choice.height is None
    assert choice.selector == "140"
    assert any("cannot produce a video export" in warning for warning in choice.warnings)


def test_audio_only_is_rejected_when_the_source_has_no_audio():
    # given
    info = _info(
        formats=[
            {
                "format_id": "137",
                "height": 1080,
                "width": 1920,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "ext": "mp4",
            }
        ]
    )

    # when / then
    with pytest.raises(PolicyError) as raised:
        choose_format(info, "audio")
    assert raised.value.code == "audio_unavailable"


def test_source_without_video_cannot_satisfy_a_video_request():
    # given
    info = _info(
        formats=[
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "ext": "m4a",
                "abr": 128,
            }
        ]
    )

    # when / then
    with pytest.raises(PolicyError) as raised:
        choose_format(info, "720p")
    assert raised.value.code == "video_unavailable"


def test_collect_formats_ignores_entries_without_an_id():
    # given
    info = _info(formats=[{"height": 720, "vcodec": "h264"}, {"format_id": "18", "height": 360,
                                                                 "vcodec": "h264", "ext": "mp4"}])

    # when
    options = collect_formats(info)

    # then
    assert [option.format_id for option in options] == ["18"]


def test_available_qualities_ignores_audio_and_storyboard_sized_renditions():
    # given
    options = collect_formats(
        _info(
            formats=[
                {"format_id": "a", "vcodec": "none", "acodec": "mp4a", "abr": 128, "ext": "m4a"},
                {"format_id": "b", "height": 96, "vcodec": "h264", "ext": "mp4"},
                {"format_id": "c", "height": 360, "vcodec": "h264", "ext": "mp4"},
            ]
        )
    )

    # then
    assert available_qualities(options) == [360]


# --------------------------------------------------------------------------
# What the user is shown before downloading
# --------------------------------------------------------------------------


def test_download_options_marks_every_rung_and_its_closest_match():
    # when
    options = {entry["id"]: entry for entry in download_options(_info())}

    # then
    assert options["360p"]["available"] is True
    assert options["360p"]["closest_height"] == 360
    assert options["1080p"]["available"] is False
    assert options["1080p"]["fallback"] is True
    assert options["1080p"]["closest_height"] == 720
    assert options["best"]["closest_height"] == 720
    assert options["lowest"]["closest_height"] == 360


def test_download_options_reports_audio_availability():
    # when
    options = {entry["id"]: entry for entry in download_options(_info())}

    # then
    assert options["audio"]["available"] is True
    assert options["audio"]["estimated_bytes"] == 2_000_000


def test_download_options_estimates_are_labelled_approximate():
    # when
    options = download_options(_info())

    # then
    assert all(entry["approximate"] is True for entry in options)
    # Sized from the published filesize where available.
    assert options[4]["estimated_bytes"] is not None


def test_estimate_display_flags_the_download_as_an_estimate():
    # given
    choice = choose_format(_info(), "720p")

    # when
    display = estimate_display(choice)

    # then
    assert display["approximate"] is True
    assert display["height"] == 720
    assert display["video_format_id"] == "136"
    assert display["audio_format_id"] == "140"
