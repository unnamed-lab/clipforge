"""Content-aware composition, measured captions and ending review."""

from __future__ import annotations

from pathlib import Path

from clipforge.quality.boundaries import review_ending
from clipforge.ranking.sentences import Sentence
from clipforge.rendering.composition import decide_composition, fit_rect
from clipforge.rendering.ffmpeg import build_command
from clipforge.rendering.spec import (
    CAPTION_PRESETS,
    CaptionBand,
    CompositionDecision,
    RenderSpec,
    build_spec,
)
from clipforge.rendering.subtitles import build_captions
from clipforge.rendering.fonts import resolve_font, text_width
from clipforge.media.visual import Scene, SceneAnalysis


def _spec(**overrides) -> RenderSpec:
    payload = {
        "source_id": "src_1",
        "segments": [{"start_ms": 0, "end_ms": 10_000}],
        "aspect": "9:16",
        "resolution": 1080,
        "composition": "auto",
    }
    payload.update(overrides)
    return build_spec(**payload)


def _words() -> list[dict]:
    phrases = [
        "nothing in society is likely to surprise you when the evidence is this clear",
        "and that is why the next part of the argument matters so much to everyone",
    ]
    words = []
    cursor = 100
    for phrase in phrases:
        for token in phrase.split():
            words.append({"start_ms": cursor, "end_ms": cursor + 240, "word": token})
            cursor += 260
    return words


# --- geometry ----------------------------------------------------------------


def test_wide_source_is_contained_inside_a_vertical_frame():
    # given a 16:9 source and a 9:16 target
    target = 1080 / 1920
    source = 1920 / 1080

    # when
    rect = fit_rect(source, target)

    # then
    assert rect.width_pct == 1.0
    assert 0.31 < rect.height_pct < 0.32
    assert 0.34 < rect.y_pct < 0.35


def test_slide_scene_is_contained_instead_of_centre_cropped():
    # given a slide with content near both edges
    scene = Scene(start_ms=0, end_ms=8_000, content_type="slide", confidence=0.9, subject_x=0.5)
    spec = _spec()

    # when
    decision = decide_composition(scene, spec, 1920, 1080)

    # then
    assert decision.mode == "fit"
    assert decision.foreground is not None
    assert decision.foreground.width_pct == 1.0
    assert decision.caption_band.top_pct >= decision.foreground.y_pct + decision.foreground.height_pct


def test_talking_head_uses_a_crop_with_a_subject_focus():
    # given a centred speaker
    scene = Scene(start_ms=0, end_ms=8_000, content_type="talking_head", confidence=0.9, subject_x=0.58)
    spec = _spec()

    # when
    decision = decide_composition(scene, spec, 1920, 1080)

    # then
    assert decision.mode == "crop"
    assert decision.crop_x_pct == 58.0
    assert decision.foreground is None


def test_user_crop_focus_overrides_scene_subject():
    # given a manual crop focus
    scene = Scene(start_ms=0, end_ms=8_000, content_type="talking_head", confidence=0.9, subject_x=0.58)
    spec = _spec(crop_x_pct=22.0, crop_y_pct=40.0)

    # when
    decision = decide_composition(scene, spec, 1920, 1080)

    # then
    assert decision.crop_x_pct == 22.0
    assert decision.crop_y_pct == 40.0


def test_edl_separates_source_and_output_time():
    # given two source spans in non-contiguous source time
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 4_000}, {"start_ms": 5_000, "end_ms": 9_000}])
    spec.segments[0].composition = CompositionDecision(mode="fit")
    spec.segments[1].composition = CompositionDecision(mode="crop")

    # when
    edl = spec.to_edl()

    # then
    assert edl["version"] == 3
    first, second = edl["segments"]
    assert (first["source_start_ms"], first["source_end_ms"]) == (0, 4_000)
    assert (first["output_start_ms"], first["output_end_ms"]) == (0, 4_000)
    assert (second["source_start_ms"], second["source_end_ms"]) == (5_000, 9_000)
    assert (second["output_start_ms"], second["output_end_ms"]) == (4_000, 8_000)
    assert second["composition"]["mode"] == "crop"


def test_edl_is_readable_from_an_older_document():
    # given a version 2 document with no composition data
    payload = {
        "version": 2,
        "source_id": "src_1",
        "segments": [{"start_ms": 1_000, "end_ms": 3_000}],
    }

    # when
    spec = RenderSpec.model_validate(payload)

    # then
    assert spec.to_edl()["segments"][0]["composition"] is None


# --- captions ----------------------------------------------------------------


def test_captions_are_measured_and_never_exceed_the_safe_width():
    # given a long spoken passage and a narrow safe band
    spec = _spec(
        caption_overrides={
            "font_family": "Montserrat",
            "max_line_width_pct": 0.62,
            "max_words_per_line": 5,
            "max_lines": 2,
        }
    )

    # when
    captions = build_captions([(0, 10_000)], [_words()], spec, 1080, 1920)

    # then
    assert captions.report.used is True
    assert captions.report.max_line_width_px <= captions.report.max_line_width_limit_px + 1
    assert captions.report.max_line_width_px < 1080
    assert captions.report.bounds_ok is True
    assert "..." not in captions.ass.split("[Events]")[1]


def test_every_spoken_word_survives_caption_segmentation():
    # given
    words = _words()
    spec = _spec()

    # when
    captions = build_captions([(0, 10_000)], [words], spec, 1080, 1920)

    # then
    for word in words:
        assert word["word"] in captions.ass


def test_caption_font_registry_resolves_requested_faces():
    # given the documented substitutions
    cases = {
        "SF Pro Display": "Inter",
        "The Bold Font": "Archivo Black",
        "Helvetica": "Lato",
        "Impact": "Anton",
        "Bebas Neue": "Bebas Neue",
    }

    # when/then
    for requested, family in cases.items():
        selection = resolve_font(requested)
        assert selection.family == family
        assert selection.file.endswith((".ttf", ".otf"))
        assert selection.path.exists()
        assert text_width("W", selection, 96) > 0


def test_caption_presets_use_bundled_faces():
    for preset in CAPTION_PRESETS.values():
        assert preset["font_family"]


# --- per-segment FFmpeg chain ------------------------------------------------


def test_two_contained_segments_get_unique_filter_labels(tmp_path: Path):
    # given two segments that both need a blurred fill
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 4_000}, {"start_ms": 5_000, "end_ms": 9_000}])
    for segment in spec.segments:
        segment.composition = CompositionDecision(
            mode="fit",
            foreground=fit_rect(1920 / 1080, 9 / 16),
            caption_band=CaptionBand(top_pct=0.7, bottom_pct=0.95),
        )

    # when
    command = build_command(
        spec,
        source=tmp_path / "source.mp4",
        cwd=tmp_path,
        has_audio=True,
        source_fps=30.0,
        captions_name="captions.ass",
        source_width=1920,
        source_height=1080,
    )

    # then
    graph = command.args[command.args.index("-filter_complex") + 1]
    assert "[bgsrc_v0]" in graph
    assert "[bgsrc_v1]" in graph
    assert graph.count("[bg_v0]") == 2
    assert graph.count("[bg_v1]") == 2
    assert "ass=captions.ass:fontsdir=fonts" in graph
    assert graph.count("ass=") == 1


def test_crop_segment_uses_measured_x_and_y_offsets(tmp_path: Path):
    # given a crop focus at the left edge
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 4_000}])
    spec.segments[0].composition = CompositionDecision(mode="crop", crop_x_pct=0, crop_y_pct=100)

    # when
    command = build_command(
        spec,
        source=tmp_path / "source.mp4",
        cwd=tmp_path,
        has_audio=False,
        source_fps=30.0,
        captions_name=None,
        source_width=1920,
        source_height=1080,
    )

    # then
    graph = command.args[command.args.index("-filter_complex") + 1]
    assert "crop=1080:1920:(iw-ow)*0.0000:(ih-oh)*1.0000" in graph


# --- ending review -----------------------------------------------------------


def _sentence(index: int, start: int, end: int, text: str, terminator: bool = True) -> Sentence:
    return Sentence(index=index, start_ms=start, end_ms=end, text=text, ends_with_terminator=terminator)


def test_ending_review_flags_speech_that_continues_immediately(monkeypatch):
    # given a candidate that ends just before speech resumes and after a visual cut
    sentences = [
        _sentence(0, 0, 40_000, "This is the complete first thought."),
        _sentence(1, 41_000, 43_000, "And this is the thought that continues."),
    ]
    analysis = SceneAnalysis(
        scenes=[
            Scene(0, 40_000, "talking_head", 0.9, 0.5),
            Scene(40_000, 50_000, "slide", 0.9, 0.5),
        ],
        samples=[],
        cuts=[39_000],
        warnings=[],
    )
    monkeypatch.setattr(
        "clipforge.quality.boundaries.analyse_source_window",
        lambda *args, **kwargs: analysis,
    )

    # when
    review = review_ending(
        candidate_start_ms=0,
        candidate_end_ms=40_500,
        sentences=sentences,
        source_path=Path("source.mp4"),
        source_duration_ms=60_000,
    )

    # then
    assert review["suspicious"] is True
    assert any("Speech continues" in item for item in review["evidence"])
    assert any("scene cut" in item for item in review["evidence"])
    assert any(alternative["end_ms"] == 39_000 for alternative in review["alternatives"])


def test_ending_review_offers_to_trim_a_cut_that_starts_the_next_shot(monkeypatch):
    # given a cut 300ms before the out point that begins a different shot
    sentences = [
        _sentence(0, 0, 40_500, "This is a complete thought."),
        _sentence(1, 40_900, 42_000, "This belongs to the next segment."),
    ]
    analysis = SceneAnalysis(
        scenes=[
            Scene(0, 40_200, "talking_head", 0.9, 0.5),
            Scene(40_200, 50_000, "slide", 0.9, 0.5),
        ],
        samples=[],
        cuts=[40_200],
        warnings=[],
    )
    monkeypatch.setattr(
        "clipforge.quality.boundaries.analyse_source_window",
        lambda *args, **kwargs: analysis,
    )

    # when
    review = review_ending(
        candidate_start_ms=0,
        candidate_end_ms=40_500,
        sentences=sentences,
        source_path=Path("source.mp4"),
        source_duration_ms=60_000,
    )

    # then
    assert review["suspicious"] is True
    assert review["alternatives"][0]["end_ms"] == 40_200
    assert review["alternatives"][0]["label"] == "Trim the final cut"
    finish = [item for item in review["alternatives"] if item["label"] == "Finish the next sentence"]
    assert finish and finish[0]["confidence"] < 0.4


def test_ending_review_is_quiet_at_the_end_of_the_source():
    # given
    sentences = [_sentence(0, 0, 40_000, "This is the last thought.")]

    # when
    review = review_ending(
        candidate_start_ms=0,
        candidate_end_ms=40_000,
        sentences=sentences,
        source_duration_ms=40_000,
    )

    # then
    assert review["suspicious"] is False
    assert any("end of the source" in item for item in review["evidence"])


def test_ending_review_never_changes_the_candidate():
    # given
    sentences = [_sentence(0, 0, 40_000, "This is the last thought.")]

    # when
    review = review_ending(candidate_start_ms=0, candidate_end_ms=40_000, sentences=sentences)

    # then
    assert review["end_ms"] == 40_000
    assert review["reviewed"] == "pending"
    assert review["alternatives"][0]["source"] == "current"
