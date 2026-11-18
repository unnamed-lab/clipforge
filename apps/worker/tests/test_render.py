"""Render specification, FFmpeg argv and caption timing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clipforge.profiles.spec import CaptionPolicy, resolve_settings
from clipforge.rendering.ffmpeg import build_command
from clipforge.rendering.spec import (
    DEFAULT_CAPTION_ALIGNMENT,
    DEFAULT_CAPTION_MARGIN_V,
    DEFAULT_CAPTION_POSITION,
    DEFAULT_CAPTION_PRESET,
    RenderSpec,
    build_spec,
)
from clipforge.rendering.subtitles import build_ass


def make_spec(**overrides):
    payload = {
        "source_id": "src_1",
        "segments": [{"start_ms": 10_000, "end_ms": 40_000}],
        "aspect": "9:16",
        "resolution": 720,
        "framing": "crop",
        "caption_preset": "clean",
    }
    payload.update(overrides)
    return build_spec(**payload)


def make_command(spec, tmp_path, *, has_audio=True, captions_name=None):
    return build_command(
        spec,
        source=tmp_path / "source.mp4",
        cwd=tmp_path,
        has_audio=has_audio,
        source_fps=30.0,
        captions_name=captions_name,
    )


# --- spec --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("aspect", "resolution", "expected"),
    [
        ("9:16", 720, (720, 1280)),
        ("16:9", 1080, (1920, 1080)),
        ("1:1", 720, (720, 720)),
    ],
)
def test_aspect_and_resolution_map_to_known_dimensions(aspect, resolution, expected):
    # given / when
    spec = make_spec(aspect=aspect, resolution=resolution)

    # then
    assert spec.video.dimensions == expected


def test_total_duration_is_the_sum_of_segments():
    # given / when
    spec = make_spec(segments=[{"start_ms": 0, "end_ms": 5_000}, {"start_ms": 10_000, "end_ms": 22_500}])

    # then
    assert spec.total_duration_ms == 17_500


def test_editorial_narrative_roles_are_valid_render_segments():
    # given / when
    spec = make_spec(
        segments=[
            {"start_ms": 0, "end_ms": 4_000, "role": "context"},
            {"start_ms": 10_000, "end_ms": 20_000, "role": "main"},
            {"start_ms": 25_000, "end_ms": 35_000, "role": "development"},
            {"start_ms": 40_000, "end_ms": 52_000, "role": "payoff"},
        ]
    )

    # then
    assert [segment.role for segment in spec.segments] == ["context", "main", "development", "payoff"]


def test_new_caption_policy_defaults_to_clean_podcast_at_the_bottom():
    # given / when
    policy = CaptionPolicy()

    # then
    assert policy.preset == DEFAULT_CAPTION_PRESET == "clean"
    assert policy.position == DEFAULT_CAPTION_POSITION == "bottom"
    assert policy.alignment == DEFAULT_CAPTION_ALIGNMENT == "center"
    assert policy.margin_v == DEFAULT_CAPTION_MARGIN_V == 150
    assert policy.position_pct == 80.0


def test_saved_caption_choices_are_preserved_over_the_default():
    # given a profile that explicitly chose a different style and position
    profile = {
        "document": {
            "captions": {
                "preset": "large",
                "position": "top",
                "alignment": "left",
                "margin_v": 220,
            }
        }
    }

    # when
    resolved = resolve_settings(profile, None)

    # then
    captions = resolved["settings"]["captions"]
    assert captions["preset"] == "large"
    assert captions["position"] == "top"
    assert captions["alignment"] == "left"
    assert captions["margin_v"] == 220


def test_segment_end_must_follow_its_start():
    # given / when / then
    with pytest.raises(ValidationError):
        RenderSpec(
            source_id="src_1",
            segments=[{"start_ms": 10_000, "end_ms": 10_000}],
        )


def test_caption_colours_must_be_hex():
    # given / when / then
    with pytest.raises(ValidationError):
        make_spec(caption_overrides={"primary_color": "white"})


def test_wrong_aspect_ratio_is_rejected():
    # given / when / then
    with pytest.raises(ValidationError):
        make_spec(aspect="4:3")


def test_validate_against_rejects_a_segment_past_the_source():
    # given
    spec = make_spec()

    # when / then
    with pytest.raises(ValueError, match="past the end"):
        spec.validate_against(35_000)


def test_validate_against_rejects_a_very_short_output():
    # given
    spec = make_spec(segments=[{"start_ms": 0, "end_ms": 1_500}])

    # when / then
    with pytest.raises(ValueError, match="shorter than 2 seconds"):
        spec.validate_against(60_000)


def test_validate_against_accepts_a_valid_spec():
    # given
    spec = make_spec()

    # when / then
    spec.validate_against(60_000)


# --- ffmpeg argv -------------------------------------------------------------


def test_command_is_an_argv_array_with_a_relative_output(tmp_path):
    # given
    spec = make_spec()

    # when
    command = make_command(spec, tmp_path)

    # then
    assert isinstance(command.args, list)
    assert command.output_name == "out.mp4"
    assert command.args[-1] == "out.mp4"
    assert command.cwd == tmp_path
    assert not any("|" in argument or "&&" in argument for argument in command.args)


def test_single_segment_uses_the_crop_framing_chain(tmp_path):
    # given
    spec = make_spec()

    # when
    command = make_command(spec, tmp_path)
    graph = command.args[command.args.index("-filter_complex") + 1]

    # then
    assert "scale=720:1280:force_original_aspect_ratio=increase" in graph
    assert "crop=720:1280" in graph
    assert "concat" not in graph
    assert "[aout]" in graph
    assert "[vout]" in graph


def test_blur_framing_composes_a_blurred_background(tmp_path):
    # given
    spec = make_spec(framing="blur")

    # when
    command = make_command(spec, tmp_path)
    graph = command.args[command.args.index("-filter_complex") + 1]

    # then
    assert "gblur=sigma=32" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph


def test_multi_segment_command_concatenates_every_input(tmp_path):
    # given
    spec = make_spec(segments=[{"start_ms": 0, "end_ms": 5_000}, {"start_ms": 10_000, "end_ms": 15_000}])

    # when
    command = make_command(spec, tmp_path)
    graph = command.args[command.args.index("-filter_complex") + 1]

    # then
    assert command.args.count("-i") == 2
    assert "concat=n=2:v=1:a=1" in graph
    assert "ass=" not in graph


def test_captions_are_burned_with_the_ass_filter(tmp_path):
    # given
    spec = make_spec()

    # when
    command = make_command(spec, tmp_path, captions_name="captions.ass")

    # then
    assert "ass=captions.ass" in command.args[command.args.index("-filter_complex") + 1]


def test_missing_audio_gets_a_silent_track(tmp_path):
    # given
    spec = make_spec()

    # when
    command = make_command(spec, tmp_path, has_audio=False)
    graph = command.args[command.args.index("-filter_complex") + 1]

    # then
    assert any("anullsrc" in argument for argument in command.args)
    assert "[1:a]" in graph


# --- captions ----------------------------------------------------------------


def caption_spec(**overrides):
    return make_spec(**overrides)


def test_every_word_gets_one_dialogue_event():
    # given
    spec = caption_spec()
    words = [
        {"start_ms": 10_200, "end_ms": 10_600, "word": "One"},
        {"start_ms": 10_700, "end_ms": 11_100, "word": "two"},
        {"start_ms": 11_200, "end_ms": 11_600, "word": "three"},
    ]

    # when
    ass = build_ass([(10_000, 13_000)], [words], spec, 720, 1280)

    # then
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 3
    assert "One" in events[0]
    assert "three" in events[2]


def test_second_segment_captions_keep_their_timeline_offset():
    # given two five-second spans, each with one word one second in
    spec = caption_spec(segments=[{"start_ms": 0, "end_ms": 5_000}, {"start_ms": 20_000, "end_ms": 25_000}])
    spans = [(0, 5_000), (20_000, 25_000)]
    words = [
        [{"start_ms": 1_000, "end_ms": 2_000, "word": "First"}],
        [{"start_ms": 21_000, "end_ms": 22_000, "word": "Second"}],
    ]

    # when
    ass = build_ass(spans, words, spec, 720, 1280)

    # then
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert "0:00:01.00,0:00:02.00" in events[0]
    # the second clip starts at 5s on the assembled timeline
    assert "0:00:06.00,0:00:07.00" in events[1]


def test_custom_caption_position_emits_a_real_ass_position():
    # given an explicit vertical percentage
    spec = make_spec(caption_overrides={"position": "custom", "position_pct": 25})
    words = [{"start_ms": 500, "end_ms": 900, "word": "Custom"}]

    # when
    ass = build_ass([(0, 3_000)], [words], spec, 720, 1280)

    # then
    event = ass.split("[Events]")[1]
    assert "\\pos(" in event
    assert "\\an5" in event


def test_caption_text_is_escaped_for_ass():
    # given
    spec = caption_spec()
    words = [{"start_ms": 500, "end_ms": 900, "word": "a{b}c\\d"}]

    # when
    ass = build_ass([(0, 3_000)], [words], spec, 720, 1280)

    # then
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue:")][0]
    assert "{b}" not in dialogue
    assert "(b)c" in dialogue


def test_empty_word_lists_produce_no_events():
    # given
    spec = caption_spec()

    # when
    ass = build_ass([(0, 3_000)], [[]], spec, 720, 1280)

    # then
    assert not [line for line in ass.splitlines() if line.startswith("Dialogue:")]


# --- request overrides -------------------------------------------------------


def test_caption_overrides_only_apply_fields_the_client_sent():
    # given a profile caption config and a client that only picked a font
    from clipforge.api.models import RenderRequest
    from clipforge.api.routes_renders import _caption_overrides

    settings = {
        "captions": {
            "max_chars_per_line": 40,
            "shadow": 1.5,
            "background_opacity": 80,
            "highlight_words": False,
        }
    }
    payload = RenderRequest(candidate_id="can_1", captions={"font_family": "Bebas Neue"})

    # when
    overrides = _caption_overrides(settings, payload)

    # then
    assert overrides["font_family"] == "Bebas Neue"
    assert overrides["max_chars_per_line"] == 40
    assert overrides["background_opacity"] == 80
    assert overrides["highlight"] == "none"
    assert "margin_v" not in overrides
    assert "max_words_per_line" not in overrides
    assert "max_line_width_pct" not in overrides


def test_saved_caption_placement_is_used_when_the_client_does_not_override_it():
    # given a project that stored top-left captions with a custom inset
    from clipforge.api.models import RenderRequest
    from clipforge.api.routes_renders import _caption_overrides

    settings = {
        "captions": {
            "position": "top",
            "alignment": "left",
            "margin_v": 220,
        }
    }
    payload = RenderRequest(candidate_id="can_1", captions={"font_family": "Montserrat"})

    # when
    overrides = _caption_overrides(settings, payload)

    # then
    assert overrides["position"] == "top"
    assert overrides["alignment"] == "left"
    assert overrides["margin_v"] == 220


def test_client_caption_placement_overrides_the_saved_default():
    # given a saved bottom default and an explicit per-render choice
    from clipforge.api.models import RenderRequest
    from clipforge.api.routes_renders import _caption_overrides

    settings = {"captions": {"position": "bottom", "alignment": "center", "margin_v": 150}}
    payload = RenderRequest(candidate_id="can_1", captions={"position": "top", "alignment": "right"})

    # when
    overrides = _caption_overrides(settings, payload)

    # then
    assert overrides["position"] == "top"
    assert overrides["alignment"] == "right"
    assert overrides["margin_v"] == 150
