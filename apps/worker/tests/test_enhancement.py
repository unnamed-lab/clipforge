"""Enhancement profiles, geometry, cache identity and real rendered frames.

Unit tests never load a model. Integration tests run the bundled ESPCN model and
FFmpeg on a generated clip, so a passing suite proves the renderer is connected
to the enhancement path rather than only validating settings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipforge.enhancement.engines import ModelUnavailable, reset_engines
from clipforge.enhancement.service import prepare_enhancement
from clipforge.enhancement.settings import (
    MODELS,
    EnhancementSettings,
    cache_key,
    intermediate_dimensions,
    resolve_model,
)
from clipforge.errors import JobFailed
from clipforge.media.process import require
from clipforge.media.probe import probe
from clipforge.rendering.ffmpeg import build_command, build_image_preview_command, execute
from clipforge.rendering.spec import CompositionDecision, build_spec, normalise_target


# --- profiles and geometry ----------------------------------------------------


def test_ai_profile_applies_conservative_defaults():
    # given / when
    settings = EnhancementSettings(profile="ai")

    # then
    assert settings.denoise == 15
    assert settings.restoration == 0.9
    assert settings.uses_ai is True


def test_ai_plus_defaults_to_static_frame_stabilisation():
    # given / when
    settings = EnhancementSettings(profile="ai_plus")

    # then
    assert settings.temporal_stabilize is True
    assert settings.restoration == 1.0


def test_standard_forces_a_no_ai_no_denoise_profile():
    # given / when
    settings = EnhancementSettings(profile="standard", denoise=90, restoration=0.2)

    # then
    assert settings.denoise == 0
    assert settings.restoration == 1.0
    assert settings.uses_ai is False


def test_invalid_model_id_is_rejected_by_the_schema():
    # when / then
    with pytest.raises(ValueError):
        EnhancementSettings(profile="ai", model_id="magic-upscaler")


@pytest.mark.parametrize(
    ("aspect", "expected"),
    [
        ("16:9", (1920, 1080)),
        ("9:16", (1080, 1920)),
        ("1:1", (1080, 1080)),
    ],
)
def test_target_1080_keeps_the_aspect_without_stretching(aspect, expected):
    # given / when
    target = normalise_target(
        aspect=aspect,
        resolution=1080,
        resolution_mode="class",
        source_width=1280,
        source_height=720,
    )
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 3000}],
        aspect=aspect,
        resolution=target["resolution"],
        resolution_mode=target["resolution_mode"],
        custom_dimensions=target["custom_dimensions"],
    )

    # then
    assert spec.output_dimensions == expected
    assert abs(expected[0] / expected[1] - {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1}[aspect]) < 0.01


def test_preserve_source_keeps_the_source_short_side():
    # given
    landscape = normalise_target(
        aspect="16:9",
        resolution=1080,
        resolution_mode="source",
        source_width=1280,
        source_height=720,
    )
    portrait = normalise_target(
        aspect="9:16",
        resolution=1080,
        resolution_mode="source",
        source_width=720,
        source_height=1280,
    )

    # then
    assert landscape["custom_dimensions"] == (1280, 720)
    assert portrait["custom_dimensions"] == (720, 1280)


def test_custom_dimensions_must_match_the_frame():
    # when / then: a 1920x1080 size cannot be squeezed into 9:16
    with pytest.raises(ValueError, match="do not match"):
        normalise_target(
            aspect="9:16",
            resolution=1080,
            resolution_mode="custom",
            source_width=1920,
            source_height=1080,
            custom_width=1920,
            custom_height=1080,
        )


def test_custom_dimensions_must_be_even():
    # when / then
    with pytest.raises(ValueError, match="even"):
        normalise_target(
            aspect="16:9",
            resolution=1080,
            resolution_mode="custom",
            source_width=1920,
            source_height=1080,
            custom_width=1921,
            custom_height=1080,
        )


def test_intermediate_covers_the_composition_without_a_second_upscale():
    # given: a 720p source composed into a portrait 1080x1920 frame needs 2.67x
    model = MODELS["espcn-x2"]

    # when
    width, height = intermediate_dimensions(
        source_width=1280,
        source_height=720,
        target_width=1080,
        target_height=1920,
        model=model,
    )

    # then: the intermediate is at least as tall as the output and keeps 16:9
    assert height >= 1920
    assert abs(width / height - 16 / 9) < 0.01


def test_auto_model_uses_the_4x_model_when_2x_cannot_cover_the_frame():
    # given
    settings = EnhancementSettings(profile="ai", model_id="auto")

    # when
    model = resolve_model(
        settings,
        source_width=1280,
        source_height=720,
        target_width=1920,
        target_height=1080,
    )
    portrait_model = resolve_model(
        settings,
        source_width=1280,
        source_height=720,
        target_width=1080,
        target_height=1920,
    )

    # then
    assert model.id == "espcn-x2"
    assert portrait_model.id == "espcn-x4"


# --- cache identity -----------------------------------------------------------


def _cache_key(**overrides):
    values = {
        "source_identity": "sha256:source",
        "start_ms": 1000,
        "end_ms": 3000,
        "settings": EnhancementSettings(profile="ai", denoise=15, restoration=0.9),
        "model": MODELS["espcn-x2"],
        "intermediate": (2560, 1440),
        "fps": 30.0,
        "encoder_version": "v1",
    }
    values.update(overrides)
    return cache_key(**values)


def test_cache_key_is_stable_for_identical_work():
    assert _cache_key() == _cache_key()


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_identity": "sha256:other-source"},
        {"start_ms": 2000},
        {"end_ms": 4000},
        {"settings": EnhancementSettings(profile="ai", denoise=40, restoration=0.9)},
        {"settings": EnhancementSettings(profile="ai", denoise=15, restoration=0.5)},
        {"model": MODELS["espcn-x4"]},
        {"intermediate": (3840, 2160)},
        {"fps": 60.0},
        {"encoder_version": "v2"},
    ],
)
def test_cache_key_changes_when_a_pixel_or_timing_input_changes(overrides):
    assert _cache_key(**overrides) != _cache_key()


def test_project_id_is_not_an_input_to_cache_identity():
    # A project copy should not be forced to redo identical enhancement work.
    import inspect

    assert "project_id" not in inspect.signature(cache_key).parameters


# --- commands -----------------------------------------------------------------


def test_enhanced_input_keeps_audio_on_the_original_source(tmp_path):
    # given
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 10_000, "end_ms": 12_000}],
        aspect="16:9",
        resolution=1080,
        enhancement=EnhancementSettings(profile="ai"),
    )
    enhanced = tmp_path / "enhanced.mp4"

    # when
    command = build_command(
        spec,
        source=tmp_path / "source.mp4",
        cwd=tmp_path,
        has_audio=True,
        source_fps=30.0,
        captions_name="captions.ass",
        source_width=1280,
        source_height=720,
        video_sources=[enhanced],
    )
    graph = command.args[command.args.index("-filter_complex") + 1]

    # then
    assert command.args.count("-i") == 2
    assert str(enhanced) in command.args
    assert "[0:v]" in graph
    assert "[1:a]" in graph
    assert "hqdn3d" not in graph  # the intermediate was already denoised
    assert "ass=captions.ass:fontsdir=fonts" in graph
    assert "scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos" in graph


def test_clean_profile_denoises_the_source_once(tmp_path):
    # given
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 2000}],
        aspect="16:9",
        resolution=1080,
        enhancement=EnhancementSettings(profile="clean", denoise=30),
    )

    # when
    command = build_command(
        spec,
        source=tmp_path / "source.mp4",
        cwd=tmp_path,
        has_audio=True,
        source_fps=30.0,
        captions_name=None,
        source_width=1280,
        source_height=720,
    )
    graph = command.args[command.args.index("-filter_complex") + 1]

    # then
    assert graph.count("hqdn3d") == 1


def test_preview_and_export_share_the_same_framing_chain(tmp_path):
    # given
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 2000}],
        aspect="9:16",
        resolution=1080,
        composition="crop",
    )
    spec.segments[0].composition = CompositionDecision(mode="crop", crop_x_pct=40, crop_y_pct=45)

    # when
    export = build_command(
        spec,
        source=tmp_path / "source.mp4",
        cwd=tmp_path,
        has_audio=True,
        source_fps=30.0,
        captions_name=None,
        source_width=1280,
        source_height=720,
    )
    preview = build_image_preview_command(
        spec,
        image=tmp_path / "frame.png",
        cwd=tmp_path,
        segment=spec.segments[0],
        source_width=1280,
        source_height=720,
    )

    # then
    export_graph = export.args[export.args.index("-filter_complex") + 1]
    preview_graph = preview.args[preview.args.index("-filter_complex") + 1]
    chain = "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos"
    assert chain in export_graph
    assert chain in preview_graph
    assert "crop=1080:1920:(iw-ow)*0.4000:(ih-oh)*0.4500" in export_graph
    assert "crop=1080:1920:(iw-ow)*0.4000:(ih-oh)*0.4500" in preview_graph


# --- fallback and cancellation ------------------------------------------------


def test_missing_model_falls_back_and_says_no_ai_ran(tmp_path, settings, monkeypatch):
    # given
    from clipforge.enhancement import service as service_module

    def unavailable(*_args, **_kwargs):
        raise ModelUnavailable("The model is not installed.")

    monkeypatch.setattr(service_module, "engine_for", unavailable)
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 1000}],
        aspect="16:9",
        resolution=1080,
        enhancement=EnhancementSettings(profile="ai", allow_fallback=True),
    )

    # when
    result = prepare_enhancement(
        spec,
        source={
            "id": "src",
            "width": 1280,
            "height": 720,
            "fps": 30.0,
            "content_hash": "sha",
            "relative_path": "projects/x/source/x.mp4",
            "duration_ms": 2000,
            "has_audio": True,
        },
        source_path=tmp_path / "source.mp4",
        settings=settings,
        project_id="a" * 32,
    )

    # then
    assert result.video_inputs == [None]
    assert result.report["applied_model"] is None
    assert result.report["applied"] == "conventional_fallback"
    assert any("no AI model ran" in warning for warning in result.warnings)


def test_missing_model_fails_when_fallback_is_refused(tmp_path, settings, monkeypatch):
    # given
    from clipforge.enhancement import service as service_module

    def unavailable(*_args, **_kwargs):
        raise ModelUnavailable("The model is not installed.")

    monkeypatch.setattr(service_module, "engine_for", unavailable)
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 1000}],
        aspect="16:9",
        resolution=1080,
        enhancement=EnhancementSettings(profile="ai", allow_fallback=False),
    )

    # when / then
    with pytest.raises(JobFailed) as raised:
        prepare_enhancement(
            spec,
            source={
                "id": "src",
                "width": 1280,
                "height": 720,
                "fps": 30.0,
                "content_hash": "sha",
                "relative_path": "projects/x/source/x.mp4",
                "duration_ms": 2000,
                "has_audio": True,
            },
            source_path=tmp_path / "source.mp4",
            settings=settings,
            project_id="a" * 32,
        )
    assert raised.value.code == "enhancement_model_unavailable"


def test_cancellation_before_the_pass_leaves_no_intermediate(tmp_path, settings):
    # given
    import threading

    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 1000}],
        aspect="16:9",
        resolution=1080,
        enhancement=EnhancementSettings(profile="ai"),
    )
    cancel = threading.Event()
    cancel.set()

    # when / then
    with pytest.raises(JobFailed) as raised:
        prepare_enhancement(
            spec,
            source={
                "id": "src",
                "width": 1280,
                "height": 720,
                "fps": 30.0,
                "content_hash": "sha",
                "relative_path": "projects/x/source/x.mp4",
                "duration_ms": 2000,
                "has_audio": True,
            },
            source_path=tmp_path / "source.mp4",
            settings=settings,
            project_id="a" * 32,
            cancel=cancel,
        )
    assert raised.value.code == "cancelled"


# --- real FFmpeg / model integration ------------------------------------------


def _make_clip(path: Path, *, width: int = 320, height: int = 180, duration: float = 2.0) -> None:
    ffmpeg = require("ffmpeg")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=30:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(path),
        ],
        check=True,
        timeout=120,
    )


def _frame_rgb(path: Path, *, at: float, width: int, height: int):
    import numpy as np

    ffmpeg = require("ffmpeg")
    seek = ["-ss", f"{at:.3f}"] if at > 0 else []
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            *seek, "-i", str(path), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return np.frombuffer(result.stdout[: width * height * 3], dtype=np.uint8).reshape(
        height, width, 3
    )


def _render(spec, source: Path, work: Path, *, video_sources=None, source_width=None, source_height=None):
    command = build_command(
        spec,
        source=source,
        cwd=work,
        has_audio=True,
        source_fps=30.0,
        captions_name=None,
        source_width=source_width,
        source_height=source_height,
        video_sources=video_sources,
    )
    execute(command, timeout_seconds=300)
    return work / "out.mp4"


@pytest.mark.integration
def test_standard_render_upscales_landscape_to_1080_with_audio(tmp_path):
    # given
    source = tmp_path / "source.mp4"
    _make_clip(source)
    work = tmp_path / "work"
    work.mkdir()
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 2000}],
        aspect="16:9",
        resolution=1080,
        captions_enabled=False,
        enhancement=EnhancementSettings(profile="standard"),
    )

    # when
    output = _render(spec, source, work, source_width=320, source_height=180)

    # then
    info = probe(output)
    assert (info.width, info.height) == (1920, 1080)
    assert info.has_audio is True
    assert abs(info.duration_ms - 2000) < 80
    assert info.fps and abs(info.fps - 30) < 0.1
    assert info.frame_count and abs(info.frame_count - 60) <= 2

    # Preview and export really draw the same frame through the same chain.
    import numpy as np

    still = tmp_path / "still.png"
    ffmpeg = require("ffmpeg")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "0.5", "-i", str(source), "-frames:v", "1", str(still),
        ],
        check=True,
        timeout=60,
    )
    preview_command = build_image_preview_command(
        spec,
        image=still,
        cwd=work,
        segment=spec.segments[0],
        source_width=320,
        source_height=180,
        output_name="parity.jpg",
    )
    execute(preview_command, timeout_seconds=120)
    preview_frame = _frame_rgb(work / "parity.jpg", at=0, width=1920, height=1080).astype(np.int16)
    export_frame = _frame_rgb(output, at=0.5, width=1920, height=1080).astype(np.int16)
    assert float(np.abs(preview_frame - export_frame).mean()) < 8.0


@pytest.mark.integration
def test_portrait_source_is_never_stretched_into_landscape(tmp_path):
    # given
    source = tmp_path / "portrait.mp4"
    _make_clip(source, width=180, height=320)
    work = tmp_path / "work"
    work.mkdir()
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 2000}],
        aspect="9:16",
        resolution=1080,
        captions_enabled=False,
    )

    # when
    output = _render(spec, source, work, source_width=180, source_height=320)

    # then
    info = probe(output)
    assert (info.width, info.height) == (1080, 1920)


@pytest.mark.integration
def test_bundled_model_really_runs_and_its_output_reaches_the_encoder(tmp_path, settings):
    # given a short, small clip so the model stays quick on CPU
    source = tmp_path / "ai-source.mp4"
    _make_clip(source, width=160, height=90)
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 1000}],
        aspect="16:9",
        resolution=720,
        resolution_mode="custom",
        custom_dimensions=(320, 180),
        captions_enabled=False,
        enhancement=EnhancementSettings(profile="ai", denoise=0, restoration=1.0),
    )
    reset_engines()

    # when
    result = prepare_enhancement(
        spec,
        source={
            "id": "src",
            "width": 160,
            "height": 90,
            "fps": 30.0,
            "content_hash": "integration-source",
            "relative_path": "projects/x/source/x.mp4",
            "duration_ms": 2000,
            "has_audio": True,
        },
        source_path=source,
        settings=settings,
        project_id="b" * 32,
    )
    work = tmp_path / "work"
    work.mkdir()
    output = _render(
        spec,
        source,
        work,
        video_sources=result.video_inputs,
        source_width=160,
        source_height=90,
    )

    # then
    assert result.report["applied_model"] == "espcn-x2"
    assert result.report["intermediate"] == [320, 180]
    assert result.video_inputs[0] is not None and result.video_inputs[0].exists()
    intermediate = probe(result.video_inputs[0])
    assert (intermediate.width, intermediate.height) == (320, 180)
    assert intermediate.has_audio is False
    final = probe(output)
    assert (final.width, final.height) == (320, 180)
    assert final.has_audio is True
    assert abs(final.duration_ms - 1000) < 80

    # The enhanced pixels really reached the encoder: compare the same output
    # frame against a conventional resize of the same source.
    import numpy as np

    standard_spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 1000}],
        aspect="16:9",
        resolution=720,
        resolution_mode="custom",
        custom_dimensions=(320, 180),
        captions_enabled=False,
        enhancement=EnhancementSettings(profile="standard"),
    )
    standard_work = tmp_path / "standard-work"
    standard_work.mkdir()
    standard_output = _render(
        standard_spec,
        source,
        standard_work,
        source_width=160,
        source_height=90,
    )
    enhanced_frame = _frame_rgb(output, at=0.5, width=320, height=180).astype(np.int16)
    standard_frame = _frame_rgb(standard_output, at=0.5, width=320, height=180).astype(np.int16)
    assert float(enhanced_frame.std()) > 5.0
    assert float(np.abs(enhanced_frame - standard_frame).mean()) > 2.0


@pytest.mark.integration
def test_enhancement_samples_at_the_output_frame_rate_not_the_source_rate(tmp_path, settings):
    # given a 60 fps source rendered at 30 fps
    source = tmp_path / "sixty.mp4"
    ffmpeg = require("ffmpeg")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=60:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(source),
        ],
        check=True,
        timeout=120,
    )
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 1000}],
        aspect="16:9",
        resolution=720,
        resolution_mode="custom",
        custom_dimensions=(320, 180),
        captions_enabled=False,
        enhancement=EnhancementSettings(profile="ai", denoise=0),
        fps=30,
    )
    reset_engines()

    # when
    result = prepare_enhancement(
        spec,
        source={
            "id": "src",
            "width": 160,
            "height": 90,
            "fps": 60.0,
            "content_hash": "sixty-fps",
            "relative_path": "projects/x/source/x.mp4",
            "duration_ms": 1000,
            "has_audio": True,
        },
        source_path=source,
        settings=settings,
        project_id="9" * 32,
    )

    # then: 30 frames, not 60, and the same rate as the final render
    intermediate = probe(result.video_inputs[0])
    assert intermediate.frame_count == 30
    assert intermediate.fps and abs(intermediate.fps - 30) < 0.1


@pytest.mark.integration
def test_second_identical_pass_uses_the_cached_intermediate(tmp_path, settings):
    # given
    source = tmp_path / "cache-source.mp4"
    _make_clip(source, width=160, height=90)
    spec = build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 1000}],
        aspect="16:9",
        resolution=720,
        resolution_mode="custom",
        custom_dimensions=(320, 180),
        captions_enabled=False,
        enhancement=EnhancementSettings(profile="ai", denoise=0),
    )
    payload = {
        "id": "src",
        "width": 160,
        "height": 90,
        "fps": 30.0,
        "content_hash": "cache-source",
        "relative_path": "projects/x/source/x.mp4",
        "duration_ms": 2000,
        "has_audio": True,
    }
    reset_engines()

    # when
    first = prepare_enhancement(
        spec, source=payload, source_path=source, settings=settings, project_id="c" * 32
    )
    second = prepare_enhancement(
        spec, source=payload, source_path=source, settings=settings, project_id="c" * 32
    )

    # then
    assert first.report["cached_segments"] == 0
    assert second.report["cached_segments"] == 1
    assert first.video_inputs == second.video_inputs
