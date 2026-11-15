#!/usr/bin/env python
"""Generate a small, real sample video with real speech for local testing.

Why this exists: the pipeline must be verified against a file that actually
contains speech, not a synthetic tone. On Windows we use the built-in SAPI
voices (Microsoft David / Zira) to synthesise a short monologue, then mux it
with a moving gradient frame so the file is a genuine MP4 with an audio track.

    python scripts/make-sample.py
    python scripts/make-sample.py --voice "Microsoft Zira Desktop" --out data/samples/other.mp4

The output is deterministic enough to use as a test fixture, and the transcript
it produces is real Whisper output rather than a hand-written mock.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

LINES = [
    "Most people think a content strategy starts with a calendar. It does not.",
    "It starts with one uncomfortable question. What do you actually know that other people do not?",
    "We spent six months publishing three times a week and learned almost nothing.",
    "The truth is that consistency without a point of view is just noise on a schedule.",
    "Then we cut the schedule in half and doubled the depth of every episode.",
    "That was the first time someone wrote to us and said one sentence changed how they worked.",
    "So here is the test I use now. If the episode disappeared, would anyone lose a specific idea?",
    "If the answer is no, the episode was a rehearsal, not a piece of work.",
    "Keep the rehearsals. Just stop publishing them.",
    "And if you remember nothing else, remember this. A strong point of view beats a busy calendar, every single time.",
]

GAP_SECONDS = 0.9


def sapi_script(voice: str, rate: int, lines: list[str], out_dir: Path) -> str:
    def ps_quote(text: str) -> str:
        return text.replace("'", "''")

    parts = [
        "Add-Type -AssemblyName System.Speech",
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        f"$synth.SelectVoice('{ps_quote(voice)}')",
        f"$synth.Rate = {rate}",
    ]
    for index, line in enumerate(lines):
        target = out_dir / f"line-{index:02d}.wav"
        parts.append(f"$synth.SetOutputToWaveFile('{ps_quote(str(target))}')")
        parts.append(f"$synth.Speak('{ps_quote(line)}')")
    parts.append("$synth.Dispose()")
    return "\n".join(parts)


def synthesise(voice: str, rate: int, lines: list[str], out_dir: Path) -> list[Path]:
    if sys.platform != "win32":
        raise SystemExit(
            "This script uses Windows SAPI voices. On macOS or Linux, record any short clip with "
            "speech and import it instead, or install espeak-ng and adapt the synthesis step."
        )

    script = out_dir / "synthesise.ps1"
    script.write_text(sapi_script(voice, rate, lines, out_dir), encoding="utf-8")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "Speech synthesis failed. Is the voice installed?\n"
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )

    paths = [out_dir / f"line-{index:02d}.wav" for index in range(len(lines))]
    missing = [path.name for path in paths if not path.exists()]
    if missing:
        raise SystemExit(f"SAPI did not produce {', '.join(missing)}")
    return paths


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    return int(round(frames / rate * 1000))


def build_video(paths: list[Path], out_path: Path, *, width: int, height: int, fps: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is not on PATH.")

    total_ms = sum(wav_duration_ms(path) for path in paths) + int(GAP_SECONDS * 1000) * (len(paths) - 1)
    total_seconds = total_ms / 1000

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    filters: list[str] = []
    concat_inputs: list[str] = []
    input_index = 0

    for index, path in enumerate(paths):
        command.extend(["-i", str(path)])
        filters.append(f"[{input_index}:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=mono[a{index}]")
        concat_inputs.append(f"[a{index}]")
        input_index += 1
        if index != len(paths) - 1:
            filters.append(
                f"anullsrc=r=48000:cl=mono:d={GAP_SECONDS}[gap{index}]"
            )
            concat_inputs.append(f"[gap{index}]")

    filters.append(
        "".join(concat_inputs) + f"concat=n={len(concat_inputs)}:v=0:a=1[aout]"
    )

    background = (
        f"gradients=size={width}x{height}:rate={fps}:c0=0x0e1014:c1=0x1b2430:"
        f"x0=0:y0=0:x1={width}:y1={height}:duration={total_seconds:.3f},"
        f"format=yuv420p[vout]"
    )
    filters.insert(0, background)

    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(out_path),
        ]
    )

    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(f"ffmpeg failed:\n{completed.stderr.strip()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a real speech sample video.")
    parser.add_argument("--out", default="data/samples/clipforge-sample.mp4")
    parser.add_argument("--voice", default="Microsoft David Desktop")
    parser.add_argument("--rate", type=int, default=-1, help="SAPI rate, -10..10 (default -1)")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--lines", type=int, default=len(LINES), help="How many sentences to speak")
    args = parser.parse_args()

    lines = LINES[: max(1, min(args.lines, len(LINES)))]
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="clipforge-sample-") as raw_tmp:
        tmp = Path(raw_tmp)
        print(f"Synthesising {len(lines)} sentences with {args.voice}…")
        paths = synthesise(args.voice, args.rate, lines, tmp)
        print("Muxing video and speech with ffmpeg…")
        build_video(paths, out_path, width=args.width, height=args.height, fps=args.fps)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"Wrote {out_path} ({size_mb:.1f} MB)")
    print("Import it through the app, or POST it to /api/projects/{id}/sources/upload.")


if __name__ == "__main__":
    main()
