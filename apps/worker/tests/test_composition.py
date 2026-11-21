"""Content-aware composition, measured captions and ending review."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from clipforge.media.visual import classify_features, frame_features
from clipforge.media.facecam import FaceCamTrack, TrackRange
from clipforge.quality.boundaries import review_ending
from clipforge.ranking.sentences import Sentence
from clipforge.rendering.composition import decide_composition, expand_composition, fit_rect
from clipforge.rendering import composition as composition_module
from clipforge.rendering.ffmpeg import build_command, build_preview_command, execute
from clipforge.rendering.spec import (
    CAPTION_PRESETS,
    CaptionBand,
    CompositionDecision,
    Rect,
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


def test_webcam_panel_crop_stays_inside_the_accepted_rect_and_off_the_gameplay_crop():
    # given a small upper-left webcam and a 9:16 stack
    scene = Scene(0, 5_000, "mixed", 0.8, 0.5)
    facecam = Rect(x_pct=0.01, y_pct=0.32, width_pct=0.20, height_pct=0.22)
    face = Rect(x_pct=0.09, y_pct=0.40, width_pct=0.03, height_pct=0.07)
    spec = _spec(composition="stack", face_cam_position="top", stack_split_pct=60)

    # when
    decision = decide_composition(
        scene, spec, 1280, 720,
        forced="stack",
        facecam_override=facecam,
        facecam_face_rect=face,
    )

    # then
    panel = decision.facecam_panel_rect
    assert panel is not None
    assert panel.x_pct >= facecam.x_pct - 1e-6
    assert panel.y_pct >= facecam.y_pct - 1e-6
    assert panel.x_pct + panel.width_pct <= facecam.x_pct + facecam.width_pct + 1e-6
    assert panel.y_pct + panel.height_pct <= facecam.y_pct + facecam.height_pct + 1e-6
    game = decision.gameplay_rect
    assert game is not None
    overlap_x = max(0.0, min(panel.x_pct + panel.width_pct, game.x_pct + game.width_pct) - max(panel.x_pct, game.x_pct))
    overlap_y = max(0.0, min(panel.y_pct + panel.height_pct, game.y_pct + game.height_pct) - max(panel.y_pct, game.y_pct))
    assert overlap_x * overlap_y == 0.0


# --- geometry ----------------------------------------------------------------


def test_evidence_fit_uses_a_neutral_background_and_warns_when_small():
    # given dense text in a 16:9 source going into a 9:16 frame
    scene = Scene(
        start_ms=0,
        end_ms=4_000,
        content_type="slide",
        confidence=0.9,
        subject_x=0.5,
        features={"edge_density": 0.12, "text_at_edges": 1.0},
    )

    # when
    decision = decide_composition(scene, _spec(), 1920, 1080)

    # then
    assert decision.mode == "fit"
    assert decision.background == "solid"
    assert any("readable" in warning for warning in decision.warnings)


def test_dark_text_document_is_not_mistaken_for_an_unknown_scene():
    # given a dark screenshot with text edges but almost no bright pixels
    features = {
        "edge_density": 0.053,
        "border_edge_density": 0.0,
        "bright_fraction": 0.006,
        "dark_fraction": 0.676,
        "text_at_edges": 0.0,
    }

    # when
    content_type, confidence = classify_features(features)

    # then
    assert content_type == "slide"
    assert confidence >= 0.6


def test_flat_bright_chart_is_not_mistaken_for_a_talking_head():
    # given a flat chart with solid colour blocks and a red/blue skin false positive
    features = {
        "edge_density": 0.012,
        "border_edge_density": 0.001,
        "bright_fraction": 0.466,
        "dark_fraction": 0.002,
        "colorfulness": 0.109,
        "std_luma": 0.245,
        "skin_fraction": 0.209,
        "text_at_edges": 0.0,
    }

    # when
    content_type, confidence = classify_features(features)

    # then
    assert content_type == "slide"
    assert confidence >= 0.6


def test_content_rect_ignores_empty_source_margins():
    # given a document-like panel inside a larger plain frame
    frame = np.full((144, 256, 3), 245, dtype=np.uint8)
    frame[20:120, 40:210] = 30
    frame[35:45, 55:195] = 245
    frame[60:70, 55:195] = 245
    frame[85:95, 55:195] = 245

    # when
    features = frame_features(frame)

    # then
    assert features["content_width"] < 0.95
    assert features["content_height"] < 0.95
    assert features["content_width"] * features["content_height"] > 0.45


def test_contained_evidence_uses_the_detected_content_rect():
    # given a slide whose detail sits inside plain margins
    scene = Scene(
        start_ms=0,
        end_ms=4_000,
        content_type="slide",
        confidence=0.9,
        subject_x=0.5,
        features={
            "edge_density": 0.12,
            "text_at_edges": 0.0,
            "content_x": 0.10,
            "content_y": 0.15,
            "content_width": 0.80,
            "content_height": 0.65,
        },
    )

    # when
    decision = decide_composition(scene, _spec(), 1920, 1080)

    # then
    assert decision.mode == "fit"
    assert decision.source_rect is not None
    assert decision.source_rect.width_pct == 0.8
    assert any("margins were cropped" in warning for warning in decision.warnings)


def test_cropped_talking_head_with_edge_text_gets_a_review_warning():
    # given a speaker scene with information touching the borders
    scene = Scene(
        start_ms=0,
        end_ms=4_000,
        content_type="talking_head",
        confidence=0.9,
        subject_x=0.5,
        features={"edge_density": 0.04, "text_at_edges": 1.0},
    )

    # when
    decision = decide_composition(scene, _spec(), 1920, 1080)

    # then
    assert decision.mode == "crop"
    assert any("cut-off text" in warning for warning in decision.warnings)


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


# --- gaming layouts ----------------------------------------------------------


def _facecam_features(**overrides) -> dict:
    base = {
        "facecam_present": 1.0,
        "facecam_x": 0.68,
        "facecam_y": 0.66,
        "facecam_width": 0.26,
        "facecam_height": 0.28,
        "facecam_confidence": 0.8,
    }
    base.update(overrides)
    return base


def test_frame_features_finds_a_skin_toned_corner_webcam():
    # given a dark game frame with a face-toned plate in the top-right corner
    frame = np.full((144, 256, 3), 24, dtype=np.uint8)
    frame[10:52, 190:246] = (176, 118, 92)

    # when
    features = frame_features(frame)

    # then
    assert features["facecam_present"] == 1.0
    assert features["facecam_x"] > 0.55
    assert features["facecam_y"] < 0.25
    assert features["facecam_width"] > 0.10
    assert features["facecam_confidence"] > 0.4


def test_full_frame_skin_does_not_become_a_facecam():
    # given a close-up speaker that fills the frame with skin
    frame = np.full((144, 256, 3), (176, 118, 92), dtype=np.uint8)

    # when
    features = frame_features(frame)

    # then
    assert features["facecam_present"] == 0.0


def test_stack_layout_places_the_detected_facecam_below_the_game():
    # given a scene with a detected corner webcam
    scene = Scene(
        start_ms=0,
        end_ms=8_000,
        content_type="unknown",
        confidence=0.6,
        subject_x=0.5,
        features=_facecam_features(),
    )

    # when
    decision = decide_composition(scene, _spec(composition="stack"), 1920, 1080)

    # then
    assert decision.mode == "stack"
    assert decision.facecam is not None
    assert decision.facecam.y_pct > 0.5
    assert decision.background == "solid"
    assert decision.caption_band.bottom_pct <= 0.60


def test_stack_layout_never_invents_a_facecam():
    # given no detected face-cam
    scene = Scene(
        start_ms=0,
        end_ms=8_000,
        content_type="unknown",
        confidence=0.6,
        subject_x=0.5,
        features={"facecam_present": 0.0},
    )

    # when
    decision = decide_composition(scene, _spec(composition="stack"), 1920, 1080)

    # then
    assert decision.mode == "crop"
    assert decision.facecam is None
    assert any("No face-cam" in warning for warning in decision.warnings)


def test_facecam_focus_crops_to_the_detected_plate():
    # given a detected corner webcam
    scene = Scene(
        start_ms=0,
        end_ms=8_000,
        content_type="talking_head",
        confidence=0.8,
        subject_x=0.2,
        features=_facecam_features(),
    )

    # when
    decision = decide_composition(scene, _spec(composition="facecam_focus"), 1920, 1080)

    # then
    assert decision.mode == "crop"
    assert decision.source_rect is not None
    assert decision.source_rect.x_pct == 0.68
    assert decision.source_rect.width_pct == 0.26


def test_facecam_focus_falls_back_when_no_plate_is_found():
    # given no detected face-cam
    scene = Scene(
        start_ms=0,
        end_ms=8_000,
        content_type="talking_head",
        confidence=0.8,
        subject_x=0.5,
        features={"facecam_present": 0.0},
    )

    # when
    decision = decide_composition(scene, _spec(composition="facecam_focus"), 1920, 1080)

    # then
    assert decision.mode == "crop"
    assert decision.source_rect is None
    assert any("No face-cam" in warning for warning in decision.warnings)


def test_forced_stack_analyses_once_and_applies_one_decision(monkeypatch, tmp_path: Path):
    # given two segments and a detected face-cam in the analysed window
    spec = _spec(
        segments=[{"start_ms": 0, "end_ms": 4_000}, {"start_ms": 5_000, "end_ms": 9_000}],
        composition="stack",
    )
    analysis = SceneAnalysis(
        scenes=[
            Scene(0, 9_000, "unknown", 0.5, 0.5, features=_facecam_features()),
        ],
        samples=[],
        cuts=[],
        warnings=[],
    )
    calls = {"count": 0}

    def fake_analyse(*args, **kwargs):
        calls["count"] += 1
        return analysis

    monkeypatch.setattr("clipforge.rendering.composition.analyse_source_window", fake_analyse)

    # when
    plan = expand_composition(spec, tmp_path / "source.mp4", 1920, 1080)

    # then
    assert calls["count"] == 1
    assert all(segment.composition.mode == "stack" for segment in plan.segments)
    assert all(segment.composition.facecam is not None for segment in plan.segments)


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


def test_portrait_captions_stay_above_platform_controls():
    # given a composition that wants captions low in a 9:16 frame
    spec = _spec(caption_overrides={"position": "bottom"})
    composition = CompositionDecision(
        mode="fit",
        caption_band=CaptionBand(top_pct=0.62, bottom_pct=0.95),
    )

    # when
    captions = build_captions([(0, 10_000)], [_words()], spec, 1080, 1920, [composition])

    # then
    assert captions.report.bands[0]["bottom_pct"] <= 0.82
    assert captions.report.bounds_ok is True


def test_landscape_captions_keep_the_wider_safe_band():
    # given a landscape export
    spec = _spec(aspect="16:9", caption_overrides={"position": "bottom"})

    # when
    captions = build_captions([(0, 10_000)], [_words()], spec, 1920, 1080)

    # then
    assert captions.report.bands[0]["bottom_pct"] == 0.90


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


def test_contained_evidence_crops_empty_margins_before_scaling(tmp_path: Path):
    # given a contained slide with a conservative content crop
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 4_000}])
    spec.segments[0].composition = CompositionDecision(
        mode="fit",
        background="solid",
        background_color="#0B0D11",
        foreground=fit_rect(1.9, 9 / 16),
        source_rect=Rect(x_pct=0.10, y_pct=0.15, width_pct=0.80, height_pct=0.65),
        caption_band=CaptionBand(top_pct=0.66, bottom_pct=0.82),
    )

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
    assert "crop=iw*0.8000:ih*0.6500:iw*0.1000:ih*0.1500" in graph
    assert "color=c=#0B0D11" in graph


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


def test_stack_chain_splits_the_game_and_the_facecam(tmp_path: Path):
    # given a stack decision with a detected corner webcam
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 4_000}], composition="stack")
    spec.segments[0].composition = CompositionDecision(
        mode="stack",
        background="solid",
        background_color="#0B0D11",
        facecam=Rect(x_pct=0.68, y_pct=0.66, width_pct=0.26, height_pct=0.28),
    )

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
    assert "split=2[gamesrc_v0][camsrc_v0]" in graph
    assert "crop=iw*0.2600:ih*0.2800:iw*0.6800:ih*0.6600" in graph
    # Two full-width panels stacked to the output height, in the requested order.
    assert "[game_v0][cam_v0]vstack=inputs=2[composed_v0]" in graph
    assert "scale=1080:1152" in graph
    assert "scale=1080:768" in graph
    assert "color=c=#0B0D11" not in graph


def test_facecam_focus_crops_the_source_before_filling(tmp_path: Path):
    # given a crop that zooms into a detected face-cam plate
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 4_000}], composition="facecam_focus")
    spec.segments[0].composition = CompositionDecision(
        mode="crop",
        source_rect=Rect(x_pct=0.68, y_pct=0.66, width_pct=0.26, height_pct=0.28),
    )

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
    assert "crop=iw*0.2600:ih*0.2800:iw*0.6800:ih*0.6600,scale=1080:1920" in graph


# --- tracked face-cam layouts ------------------------------------------------


def _facecam_scene(
    *,
    start_ms: int = 0,
    end_ms: int = 10_000,
    rect: tuple[float, float, float, float] = (0.70, 0.65, 0.25, 0.30),
    content_type: str = "mixed",
) -> Scene:
    return Scene(
        start_ms=start_ms,
        end_ms=end_ms,
        content_type=content_type,
        confidence=0.8,
        subject_x=0.5,
        features={
            "facecam_present": 1.0,
            "facecam_confidence": 0.9,
            "facecam_x": rect[0],
            "facecam_y": rect[1],
            "facecam_width": rect[2],
            "facecam_height": rect[3],
            "skin_fraction": 0.02,
            "content_x": 0.0,
            "content_y": 0.0,
            "content_width": 1.0,
            "content_height": 1.0,
        },
    )


def test_stack_panels_match_the_actual_layout_aspect_not_a_square():
    # given a 16:9 source stacked into a 9:16 output with a 55% gameplay panel
    spec = _spec(composition="stack", aspect="9:16", resolution=1080, stack_split_pct=55)

    # when
    decision = decide_composition(
        _facecam_scene(), spec, 1920, 1080, forced="stack", facecam_state="visible"
    )

    # then
    assert decision.mode == "stack"
    assert decision.gameplay_rect is not None
    assert decision.facecam_panel_rect is not None
    width, height = spec.video.dimensions
    game_aspect = width / (height * decision.stack_split_pct / 100)
    cam_aspect = width / (height * (1 - decision.stack_split_pct / 100))
    game_source_aspect = (
        decision.gameplay_rect.width_pct * 1920 / (decision.gameplay_rect.height_pct * 1080)
    )
    cam_source_aspect = (
        decision.facecam_panel_rect.width_pct
        * 1920
        / (decision.facecam_panel_rect.height_pct * 1080)
    )
    assert abs(game_source_aspect - game_aspect) < 0.02
    assert abs(cam_source_aspect - cam_aspect) < 0.02


def test_stack_facecam_position_changes_the_panel_order_and_caption_band():
    # given the same source and split with the face-cam above and below
    bottom_spec = _spec(composition="stack", stack_split_pct=60, face_cam_position="bottom")
    top_spec = _spec(composition="stack", stack_split_pct=60, face_cam_position="top")

    # when
    bottom = decide_composition(_facecam_scene(), bottom_spec, 1920, 1080, forced="stack")
    top = decide_composition(_facecam_scene(), top_spec, 1920, 1080, forced="stack")

    # then
    assert bottom.face_cam_position == "bottom"
    assert top.face_cam_position == "top"
    assert bottom.caption_band is not None and top.caption_band is not None
    assert bottom.caption_band.bottom_pct <= 0.60
    assert top.caption_band.top_pct >= 0.40
    assert bottom.caption_band.top_pct != top.caption_band.top_pct


def test_stack_without_a_face_cam_falls_back_and_explains_why():
    # given a source with no detectable webcam
    spec = _spec(composition="stack")
    scene = Scene(0, 10_000, "mixed", 0.8, 0.5, features={"facecam_present": 0.0})

    # when
    decision = decide_composition(scene, spec, 1920, 1080, forced="stack", facecam_state="missing")

    # then
    assert decision.mode == "crop"
    assert decision.facecam is None
    assert any("No face-cam was detected" in warning for warning in decision.warnings)


def test_adaptive_stack_is_opt_in_and_bounded():
    # given a large face-cam and the same split
    scene = _facecam_scene(rect=(0.55, 0.50, 0.40, 0.46))
    stable_spec = _spec(composition="stack", stack_split_pct=60, adaptive_stack=False)
    adaptive_spec = _spec(composition="stack", stack_split_pct=60, adaptive_stack=True)

    # when
    stable = decide_composition(scene, stable_spec, 1920, 1080, forced="stack")
    adaptive = decide_composition(scene, adaptive_spec, 1920, 1080, forced="stack")

    # then
    assert stable.stack_split_pct == 60
    assert 35 <= adaptive.stack_split_pct <= 80
    assert adaptive.stack_split_pct != stable.stack_split_pct


def test_gameplay_crop_records_protected_regions_and_stays_in_frame():
    # given HUD detail in the top-left and a face-cam bottom-right
    scene = _facecam_scene()
    scene.features.update(
        {
            "grid_edge_0": 0.30,
            "grid_edge_2": 0.18,
            "grid_edge_6": 0.12,
            "content_x": 0.05,
            "content_y": 0.05,
            "content_width": 0.90,
            "content_height": 0.80,
        }
    )
    spec = _spec(composition="stack", stack_split_pct=50)

    # when
    decision = decide_composition(scene, spec, 1920, 1080, forced="stack")

    # then
    assert decision.protected_regions
    assert decision.gameplay_rect is not None
    rect = decision.gameplay_rect
    assert 0 <= rect.x_pct <= 1
    assert 0 <= rect.y_pct <= 1
    assert rect.x_pct + rect.width_pct <= 1.0001
    assert rect.y_pct + rect.height_pct <= 1.0001


def _fake_track(ranges: list[TrackRange]) -> FaceCamTrack:
    return FaceCamTrack(observations=[], ranges=ranges, sample_fps=1.0)


def test_expand_composition_splits_a_segment_when_the_face_cam_disappears(monkeypatch):
    # given a ten second segment whose webcam disappears halfway through
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 10_000}], composition="stack")
    track = _fake_track(
        [
            TrackRange(0, 5_000, "visible", (0.70, 0.65, 0.25, 0.30), 0.9, "stable"),
            TrackRange(5_000, 10_000, "missing", None, 0.0, "gone"),
        ]
    )
    analysis = SceneAnalysis(
        scenes=[_facecam_scene(start_ms=0, end_ms=10_000)],
        samples=[],
        cuts=[],
        facecam_track=track,
    )
    monkeypatch.setattr(
        composition_module, "analyse_source_window", lambda *args, **kwargs: analysis
    )

    # when
    plan = expand_composition(spec, Path("source.mp4"), 1920, 1080)

    # then
    assert [(item.start_ms, item.end_ms) for item in plan.segments] == [(0, 5_000), (5_000, 10_000)]
    assert plan.segments[0].composition.mode == "stack"
    assert plan.segments[1].composition.mode == "crop"
    assert plan.facecam_track is not None
    assert len(plan.facecam_track.ranges) == 2
    assert any(
        "No face-cam was detected" in warning
        for warning in plan.segments[1].composition.warnings
    )


def test_expand_composition_keeps_a_manual_range_authoritative(monkeypatch):
    # given a user correction inside an otherwise tracked segment
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 6_000}], composition="stack")
    track = _fake_track(
        [
            TrackRange(0, 2_000, "visible", (0.70, 0.65, 0.25, 0.30), 0.9, "stable"),
            TrackRange(2_000, 4_000, "manual", (0.05, 0.05, 0.20, 0.20), 1.0, "user moved it"),
            TrackRange(4_000, 6_000, "visible", (0.70, 0.65, 0.25, 0.30), 0.9, "stable"),
        ]
    )
    analysis = SceneAnalysis(
        scenes=[_facecam_scene(start_ms=0, end_ms=6_000)],
        samples=[],
        cuts=[],
        facecam_track=track,
    )
    monkeypatch.setattr(
        composition_module, "analyse_source_window", lambda *args, **kwargs: analysis
    )

    # when
    plan = expand_composition(spec, Path("source.mp4"), 1920, 1080)

    # then
    manual = [
        item for item in plan.segments
        if item.composition is not None and item.composition.facecam_state == "manual"
    ]
    assert len(manual) == 1
    assert (manual[0].start_ms, manual[0].end_ms) == (2_000, 4_000)
    assert manual[0].composition.facecam is not None
    assert manual[0].composition.facecam.x_pct == 0.05


def test_stack_chain_uses_canonical_panel_rects_and_top_order(tmp_path: Path):
    # given a stack decision with explicit panel geometry and a top face-cam
    spec = _spec(segments=[{"start_ms": 0, "end_ms": 4_000}], composition="stack")
    spec.segments[0].composition = CompositionDecision(
        mode="stack",
        background="solid",
        background_color="#0B0D11",
        facecam=Rect(x_pct=0.70, y_pct=0.65, width_pct=0.25, height_pct=0.30),
        face_cam_position="top",
        stack_split_pct=55,
        gameplay_rect=Rect(x_pct=0.10, y_pct=0.0, width_pct=0.60, height_pct=1.0),
        facecam_panel_rect=Rect(x_pct=0.66, y_pct=0.60, width_pct=0.30, height_pct=0.40),
    )

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
    assert "crop=iw*0.6000:ih*1.0000:iw*0.1000:ih*0.0000" in graph
    assert "crop=iw*0.3000:ih*0.4000:iw*0.6600:ih*0.6000" in graph
    assert "[cam_v0][game_v0]vstack=inputs=2[composed_v0]" in graph
    assert "force_original_aspect_ratio=increase,crop=1080:1056" in graph


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


REAL_FACECAM_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "data/projects/732a882663654a8b9399a12aebe53a2a/source"
    / "660f9c3aa87c4fe4ae19b12897861a1f.mov"
)


@pytest.mark.skipif(not REAL_FACECAM_SOURCE.exists(), reason="real face-cam fixture is not present")
def test_rendered_stack_uses_the_accepted_webcam_pixels(tmp_path):
    # given a real source with a small bordered webcam in the upper-left
    source = REAL_FACECAM_SOURCE.resolve()
    spec = _spec(
        segments=[{"start_ms": 55_000, "end_ms": 80_000}],
        composition="stack",
        face_cam_position="top",
        stack_split_pct=60,
        captions_enabled=False,
        captions_burn_in=False,
        captions_sidecar=False,
    )
    plan = expand_composition(
        spec, source, 1280, 720,
        source_duration_ms=7_200_000,
        cache_dir=tmp_path / "analysis",
        analysis_height=144,
    )
    spec.segments = plan.segments
    decision = plan.segments[0].composition
    assert decision is not None and decision.facecam is not None
    assert decision.facecam_panel_rect is not None

    # when the same preview path the UI uses renders a real frame
    work = tmp_path / "preview"
    work.mkdir()
    command = build_preview_command(
        spec, segment=plan.segments[0], source=source, cwd=work,
        at_ms=70_000, source_width=1280, source_height=720,
    )
    execute(command, timeout_seconds=120)
    rendered = np.asarray(Image.open(work / command.output_name).convert("RGB"), dtype=np.float32)

    # and the source frame at that time is extracted for comparison
    source_frame = tmp_path / "source.jpg"
    subprocess.run(
        [
            "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", "70.000", "-i", str(source), "-frames:v", "1",
            "-vf", "scale=1280:720", "-q:v", "2", str(source_frame),
        ],
        check=True,
    )
    source_rgb = Image.open(source_frame).convert("RGB")
    panel = decision.facecam_panel_rect
    expected = source_rgb.crop((
        int(panel.x_pct * 1280),
        int(panel.y_pct * 720),
        int((panel.x_pct + panel.width_pct) * 1280),
        int((panel.y_pct + panel.height_pct) * 720),
    )).resize((rendered.shape[1], 768), Image.Resampling.LANCZOS)
    expected_rgb = np.asarray(expected, dtype=np.float32)
    top = rendered[:768]
    bottom = rendered[1152:]
    top_error = float(np.mean(np.abs(top - expected_rgb)))
    cross_error = float(np.mean(np.abs(top - bottom)))

    # then the top panel is the accepted webcam rectangle, not the game panel
    assert top_error < 18.0
    assert cross_error > top_error * 2.0
