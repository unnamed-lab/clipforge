"""Repeatable render benchmarks.

The suite measures warm wall time, cold start, FFmpeg CPU time and peak RSS for
the scenarios that matter to Clipforge. It is deliberately separate from pytest:
a benchmark is evidence, not a correctness test, and CI hardware is noisy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image, ImageDraw

from clipforge.enhancement.service import prepare_enhancement
from clipforge.enhancement.settings import EnhancementSettings
from clipforge.media.process import require
from clipforge.media.probe import probe
from clipforge.rendering.ffmpeg import build_command
from clipforge.rendering.fonts import copy_fonts, resolve_font
from clipforge.rendering.spec import (
    CompositionDecision,
    Rect,
    RenderSpec,
    build_spec,
)
from clipforge.rendering.subtitles import build_captions

BENCH_RE = re.compile(
    r"bench: utime=([0-9.]+)s stime=([0-9.]+)s rtime=([0-9.]+)s"
)
MAXRSS_RE = re.compile(r"maxrss=([0-9]+)KiB")


@dataclass
class Scenario:
    name: str
    spec: RenderSpec
    captions: bool = False
    logo: bool = False
    ai: bool = False
    description: str = ""


@dataclass
class RunResult:
    wall_seconds: float
    cpu_seconds: float
    max_rss_bytes: int
    returncode: int
    stderr_tail: str


@dataclass
class ScenarioResult:
    name: str
    description: str
    output_frames: int
    cold_seconds: float
    warm_seconds: float
    median_ms_per_frame: float
    p95_ms_per_frame: float | None
    decode_ms_per_frame: float | None
    filter_ms_per_frame: float | None
    cpu_cores_used: float
    peak_rss_bytes: int
    output_width: int
    output_height: int
    output_size_bytes: int
    has_audio: bool
    audio_duration_delta_ms: int | None
    caption_build_ms: float | None
    enhancement: dict | None
    enhancement_ms_per_frame: float | None
    ai_total_ms_per_frame: float | None
    target_ms_per_frame: float
    target_met: bool
    valid: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "output_frames": self.output_frames,
            "cold_seconds": round(self.cold_seconds, 3),
            "warm_seconds": round(self.warm_seconds, 3),
            "median_ms_per_frame": round(self.median_ms_per_frame, 2),
            "p95_ms_per_frame": self.p95_ms_per_frame,
            "decode_ms_per_frame": self.decode_ms_per_frame,
            "filter_ms_per_frame": self.filter_ms_per_frame,
            "cpu_cores_used": round(self.cpu_cores_used, 2),
            "peak_rss_bytes": self.peak_rss_bytes,
            "output_width": self.output_width,
            "output_height": self.output_height,
            "output_size_bytes": self.output_size_bytes,
            "has_audio": self.has_audio,
            "audio_duration_delta_ms": self.audio_duration_delta_ms,
            "caption_build_ms": self.caption_build_ms,
            "enhancement": self.enhancement,
            "enhancement_ms_per_frame": self.enhancement_ms_per_frame,
            "ai_total_ms_per_frame": self.ai_total_ms_per_frame,
            "target_ms_per_frame": self.target_ms_per_frame,
            "target_met": self.target_met,
            "valid": self.valid,
            "warnings": self.warnings,
        }


def generate_source(path: Path, *, duration: float = 10.0, width: int = 1280, height: int = 720) -> None:
    if path.exists():
        return
    ffmpeg = require("ffmpeg")
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=30:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(path),
        ],
        check=True,
        timeout=300,
    )


def _logo(path: Path) -> None:
    if path.exists():
        return
    image = Image.new("RGBA", (240, 96), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 239, 95), radius=16, fill=(15, 23, 42, 220))
    draw.text((16, 30), "CLIPFORGE", fill=(255, 255, 255, 255))
    image.save(path)


def _words(duration_ms: int) -> list[dict]:
    words = []
    count = max(4, duration_ms // 250)
    for index in range(count):
        start = index * 250
        words.append({"start_ms": start, "end_ms": start + 220, "word": f"word{index}"})
    return words


def _common_spec(duration_ms: int, **kwargs) -> RenderSpec:
    return build_spec(
        source_id="benchmark",
        segments=[{"start_ms": 0, "end_ms": duration_ms}],
        aspect=kwargs.pop("aspect", "16:9"),
        resolution=kwargs.pop("resolution", 1080),
        captions_enabled=kwargs.pop("captions_enabled", False),
        **kwargs,
    )


def build_scenarios(duration_ms: int) -> list[Scenario]:
    scenarios: list[Scenario] = []

    scenarios.append(
        Scenario(
            "standard_landscape_1080p",
            _common_spec(duration_ms),
            description="1280x720 -> 1920x1080, software x264, no captions",
        )
    )
    scenarios.append(
        Scenario(
            "standard_portrait_1080p",
            _common_spec(duration_ms, aspect="9:16"),
            description="1280x720 -> 1080x1920 portrait crop",
        )
    )
    gameplay = _common_spec(duration_ms, composition="crop")
    gameplay.segments[0].composition = CompositionDecision(mode="crop", crop_x_pct=50, crop_y_pct=50)
    scenarios.append(
        Scenario("gameplay_only_1080p", gameplay, description="Gameplay crop, no webcam panel")
    )

    stack = _common_spec(duration_ms, aspect="9:16", composition="stack")
    stack.segments[0].composition = CompositionDecision(
        mode="stack",
        gameplay_rect=Rect(x_pct=0.0, y_pct=0.0, width_pct=1.0, height_pct=0.62),
        facecam_panel_rect=Rect(x_pct=0.68, y_pct=0.62, width_pct=0.30, height_pct=0.36),
        facecam=Rect(x_pct=0.72, y_pct=0.10, width_pct=0.22, height_pct=0.22),
        face_cam_position="bottom",
        stack_split_pct=62,
        background="solid",
    )
    scenarios.append(
        Scenario(
            "gameplay_stack_webcam_1080p",
            stack,
            description="62% gameplay + 38% webcam panel, one source",
        )
    )

    captioned = _common_spec(duration_ms, aspect="9:16", captions_enabled=True)
    scenarios.append(
        Scenario(
            "captions_branded_1080p",
            captioned,
            captions=True,
            logo=True,
            description="Portrait crop with burned-in word captions and logo",
        )
    )

    complex_spec = _common_spec(
        duration_ms,
        aspect="9:16",
        composition="stack",
        captions_enabled=True,
        audio={"normalize": True},
    )
    complex_spec.segments[0].composition = stack.segments[0].composition
    scenarios.append(
        Scenario(
            "complex_overlays_1080p",
            complex_spec,
            captions=True,
            logo=True,
            description="Stack, captions, logo and loudness normalisation",
        )
    )

    scenarios.append(
        Scenario(
            "conventional_resize_720_1080",
            _common_spec(duration_ms),
            description="Conventional 720p -> 1080p resize for comparison",
        )
    )

    ai_spec = _common_spec(
        min(duration_ms, 2000),
        aspect="9:16",
        enhancement=EnhancementSettings(profile="ai_fast"),
        fps=30,
    )
    scenarios.append(
        Scenario(
            "ai_fast_1080p",
            ai_spec,
            ai=True,
            description="ESPCN 2x Fast AI, 720p -> 1080x1920",
        )
    )
    return scenarios


def _with_benchmark(args: list[str]) -> list[str]:
    updated = list(args)
    if "-loglevel" in updated:
        updated[updated.index("-loglevel") + 1] = "info"
    if "-benchmark" not in updated:
        # Global option, before the output path.
        updated.insert(len(updated) - 1, "-benchmark")
    return updated


def _run(args: list[str], cwd: Path, timeout: float = 1800) -> RunResult:
    started = time.perf_counter()
    process = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    wall = time.perf_counter() - started
    stderr = process.stderr or ""
    bench = BENCH_RE.search(stderr)
    rss = MAXRSS_RE.search(stderr)
    cpu = float(bench.group(1)) + float(bench.group(2)) if bench else 0.0
    return RunResult(
        wall_seconds=wall,
        cpu_seconds=cpu,
        max_rss_bytes=int(rss.group(1)) * 1024 if rss else 0,
        returncode=process.returncode,
        stderr_tail=stderr[-600:],
    )


def _filter_only_args(command_args: list[str]) -> list[str] | None:
    try:
        index = command_args.index("-filter_complex")
    except ValueError:
        return None
    base = command_args[: index + 2]
    return _with_benchmark(base + ["-map", "[vout]", "-map", "[aout]", "-f", "null", "-"])


def _decode_only_args(source: Path, duration_ms: int) -> list[str]:
    ffmpeg = require("ffmpeg")
    return _with_benchmark(
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info",
            "-t", f"{duration_ms / 1000:.3f}", "-i", str(source),
            "-an", "-f", "null", "-",
        ]
    )


def run_scenario(
    scenario: Scenario,
    *,
    source: Path,
    logos: Path,
    work_root: Path,
    repeats: int = 2,
) -> ScenarioResult:
    work = work_root / scenario.name
    work.mkdir(parents=True, exist_ok=True)
    spec = scenario.spec
    duration_ms = spec.total_duration_ms
    source_info = probe(source)
    source_fps = source_info.fps or 30.0
    target_fps = float(spec.video.fps or source_fps or 30.0)
    if spec.encode.max_fps:
        target_fps = min(target_fps, float(spec.encode.max_fps))
    target_fps = max(15.0, min(60.0, target_fps))
    output_frames = max(1, int(round(duration_ms / 1000 * target_fps)))

    caption_ms = None
    captions_name = None
    if scenario.captions:
        started = time.perf_counter()
        words = _words(duration_ms)
        result = build_captions(
            [(segment.start_ms, segment.end_ms) for segment in spec.segments],
            [words],
            spec,
            spec.output_dimensions[0],
            spec.output_dimensions[1],
            [segment.composition for segment in spec.segments],
        )
        (work / "captions.ass").write_text(result.ass, encoding="utf-8")
        copy_fonts(
            work / "fonts",
            resolve_font(spec.captions.font_family, weight=spec.captions.weight, bold=spec.captions.bold),
        )
        captions_name = "captions.ass"
        caption_ms = round((time.perf_counter() - started) * 1000, 3)

    if scenario.logo:
        (work / "logo.png").write_bytes(logos.read_bytes())
        spec.logo.enabled = True
        spec.logo.asset_name = "logo.png"
        spec.logo.position = "top-right"
        spec.logo.width_pct = 16

    enhancement_report = None
    if scenario.ai:
        if spec.enhancement.profile != "ai_fast":
            spec.enhancement = EnhancementSettings(profile="ai_fast")
        # A benchmark must measure the pass, not reuse a previous run's cache.
        import shutil

        shutil.rmtree(work_root / "data" / "cache", ignore_errors=True)
        started = time.perf_counter()
        enhancement = prepare_enhancement(
            spec,
            source={
                "id": "benchmark",
                "width": source_info.width or 1280,
                "height": source_info.height or 720,
                "fps": source_fps,
                "content_hash": "benchmark-source",
                "relative_path": "projects/benchmark/source/source.mp4",
                "duration_ms": duration_ms,
                "has_audio": True,
            },
            source_path=source,
            settings=replace(_settings(), data_dir=work_root / "data"),
            project_id="b" * 32,
            cancel=None,
        )
        enhancement_report = {
            **enhancement.report,
            "wall_seconds": round(time.perf_counter() - started, 3),
        }
        # The final command must still read audio from the original; the
        # intermediate is video-only.
        video_sources = enhancement.video_inputs
    else:
        video_sources = None

    command = build_command(
        spec,
        source=source,
        cwd=work,
        has_audio=True,
        source_fps=source_fps,
        captions_name=captions_name,
        source_width=source_info.width,
        source_height=source_info.height,
        video_sources=video_sources,
    )
    args = _with_benchmark(command.args)
    runs = [_run(args, work) for _ in range(repeats + 1)]
    if runs[0].returncode != 0:
        raise RuntimeError(f"{scenario.name} failed: {runs[0].stderr_tail}")
    warm = runs[1:] or runs
    warm_walls = [run.wall_seconds for run in warm]
    warm_seconds = statistics.median(warm_walls)
    ms_values = sorted(run.wall_seconds * 1000 / output_frames for run in warm)
    p95 = ms_values[-1] if len(ms_values) < 5 else ms_values[int(len(ms_values) * 0.95)]
    cpu_cores = (
        statistics.median([run.cpu_seconds / max(0.001, run.wall_seconds) for run in warm])
        if warm else 0.0
    )

    decode_ms = filter_ms = None
    decode_args = _decode_only_args(source, duration_ms)
    filter_args = _filter_only_args(command.args)
    if filter_args:
        filter_run = _run(filter_args, work)
        if filter_run.returncode == 0:
            filter_ms = round(filter_run.wall_seconds * 1000 / output_frames, 2)
    decode_run = _run(decode_args, work)
    if decode_run.returncode == 0:
        decode_ms = round(decode_run.wall_seconds * 1000 / output_frames, 2)

    output = work / "out.mp4"
    info = probe(output)
    audio_delta = None
    if info.has_audio:
        audio_delta = abs(info.duration_ms - duration_ms)
    valid = (
        (info.width, info.height) == spec.output_dimensions
        and abs(info.duration_ms - duration_ms) <= max(250, duration_ms * 0.03)
        and info.has_audio
    )
    enhancement_ms = None
    ai_total_ms = None
    if enhancement_report and enhancement_report.get("applied_model"):
        enhancement_ms = round(
            float(enhancement_report.get("seconds", 0)) * 1000 / output_frames, 2
        )
        ai_total_ms = round(
            (float(enhancement_report.get("seconds", 0)) + warm_seconds) * 1000 / output_frames,
            2,
        )

    warnings = []
    if not valid:
        warnings.append("Output dimensions, duration or audio did not match the scenario")
    if scenario.ai and enhancement_report and not enhancement_report.get("applied_model"):
        warnings.append("AI was requested but no model ran")

    return ScenarioResult(
        name=scenario.name,
        description=scenario.description,
        output_frames=output_frames,
        cold_seconds=runs[0].wall_seconds,
        warm_seconds=warm_seconds,
        median_ms_per_frame=statistics.median(ms_values),
        p95_ms_per_frame=None if len(ms_values) < 3 else round(p95, 2),
        decode_ms_per_frame=decode_ms,
        filter_ms_per_frame=filter_ms,
        cpu_cores_used=cpu_cores,
        peak_rss_bytes=max(run.max_rss_bytes for run in runs),
        output_width=info.width or 0,
        output_height=info.height or 0,
        output_size_bytes=info.size_bytes,
        has_audio=info.has_audio,
        audio_duration_delta_ms=audio_delta,
        caption_build_ms=caption_ms,
        enhancement=enhancement_report,
        enhancement_ms_per_frame=enhancement_ms,
        ai_total_ms_per_frame=ai_total_ms,
        target_ms_per_frame=5.0,
        target_met=statistics.median(ms_values) <= 5.0,
        valid=valid,
        warnings=warnings,
    )


def _settings():
    from clipforge.config import load_settings

    return load_settings()


def environment_report() -> dict:
    from clipforge.api.routes_system import _binary_version

    try:
        gpu = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        gpu_text = (gpu.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        gpu_text = None
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "ffmpeg": _binary_version("ffmpeg"),
        "ffprobe": _binary_version("ffprobe"),
        "gpu": gpu_text,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clipforge render benchmark")
    parser.add_argument("--source", type=Path, help="Optional real 1280x720 source clip")
    parser.add_argument("--duration", type=float, default=10.0, help="Synthetic source seconds")
    parser.add_argument("--repeat", type=int, default=2, help="Warm runs per scenario")
    parser.add_argument("--out", type=Path, default=Path("data/benchmark/render-results.json"))
    parser.add_argument("--only", default="", help="Comma-separated scenario names")
    parser.add_argument("--save-baseline", type=Path, help="Write this run as a baseline")
    parser.add_argument("--check", type=Path, help="Compare against a baseline JSON")
    parser.add_argument("--tolerance", type=float, default=0.25, help="Allowed regression fraction")
    args = parser.parse_args(argv)

    duration_ms = int(args.duration * 1000)
    base = Path("data/benchmark").resolve()
    base.mkdir(parents=True, exist_ok=True)
    source = (args.source or (base / f"synthetic-720p30-{args.duration:g}s.mp4")).resolve()
    if args.source is None:
        generate_source(source, duration=args.duration)
    logo = base / "logo.png"
    _logo(logo)
    work_root = (base / "runs").resolve()
    scenarios = build_scenarios(duration_ms)
    selected = {name.strip() for name in args.only.split(",") if name.strip()}
    if selected:
        scenarios = [scenario for scenario in scenarios if scenario.name in selected]

    results = []
    for scenario in scenarios:
        print(f"running {scenario.name} ...", flush=True)
        result = run_scenario(scenario, source=source, logos=logo, work_root=work_root, repeats=args.repeat)
        results.append(result)
        print(
            f"  warm {result.warm_seconds:.2f}s "
            f"({result.median_ms_per_frame:.2f} ms/frame), "
            f"cold {result.cold_seconds:.2f}s, valid={result.valid}",
            flush=True,
        )

    payload = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": environment_report(),
        "workload": {
            "source": str(source),
            "duration_seconds": args.duration,
            "output": "H.264/AAC, software x264, veryfast CRF 23 (spec encode defaults)",
            "target_ms_per_frame": 5.0,
        },
        "results": [result.to_dict() for result in results],
    }
    args.out = args.out.resolve()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.save_baseline:
        args.save_baseline = args.save_baseline.resolve()
        args.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.save_baseline.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")

    if args.check:
        baseline = json.loads(args.check.read_text(encoding="utf-8"))
        failures = []
        baseline_results = {row["name"]: row for row in baseline.get("results", [])}
        for result in results:
            old = baseline_results.get(result.name)
            if not old:
                continue
            limit = old["median_ms_per_frame"] * (1 + args.tolerance)
            if result.median_ms_per_frame > limit:
                failures.append(
                    f"{result.name}: {result.median_ms_per_frame:.2f} ms/frame "
                    f"(baseline {old['median_ms_per_frame']:.2f}, limit {limit:.2f})"
                )
            if not result.valid:
                failures.append(f"{result.name}: invalid output")
        if failures:
            print("REGRESSION:")
            for failure in failures:
                print("  -", failure)
            return 1
        print("performance and validity within tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
