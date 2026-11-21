"""Bounded-memory frame streaming for the enhancement pass.

A video is never loaded into memory. FFmpeg decodes one frame at a time to
stdout, Python hands that frame to the engine, and a second FFmpeg process
encodes the result from stdin. Both processes can be terminated on cancel, and
a partial file is deleted by the caller.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterator

import numpy as np

from clipforge.media.process import require

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class FrameStreamError(RuntimeError):
    pass


class FrameReader:
    def __init__(
        self,
        source: Path,
        *,
        start_ms: int,
        end_ms: int,
        width: int,
        height: int,
        fps: float,
        cancel: threading.Event | None = None,
        denoise_filter: str | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.frame_bytes = width * height * 3
        self.cancel = cancel
        ffmpeg = require("ffmpeg")
        args = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start_ms / 1000:.3f}",
            "-t", f"{max(1, end_ms - start_ms) / 1000:.3f}",
            "-i", str(source),
            "-map", "0:v:0", "-an", "-sn", "-dn",
        ]
        filters = [f"fps={max(1.0, fps):.6f}"]
        if denoise_filter:
            filters.insert(0, denoise_filter)
        args += ["-vf", ",".join(filters)]
        args += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self._stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

    def __iter__(self) -> Iterator[np.ndarray]:
        assert self.process.stdout is not None
        while True:
            if self.cancel is not None and self.cancel.is_set():
                self.close()
                return
            chunk = self.process.stdout.read(self.frame_bytes)
            if not chunk:
                break
            if len(chunk) < self.frame_bytes:
                self.close()
                raise FrameStreamError("The source ended in the middle of a frame.")
            yield np.frombuffer(chunk, dtype=np.uint8).reshape(
                self.height, self.width, 3
            )

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def error_text(self) -> str:
        try:
            self._stderr.seek(0)
            return self._stderr.read().decode("utf-8", "replace")[-1200:]
        except (OSError, ValueError):
            return ""

    def __enter__(self) -> "FrameReader":
        return self

    def __exit__(self, *exc_info) -> None:
        # The output is validated by frame count, dimensions and duration after
        # the pass; raising here would mask the real exception from the caller.
        self.close()


class FrameWriter:
    def __init__(
        self,
        destination: Path,
        *,
        width: int,
        height: int,
        fps: float,
        crf: int = 12,
        preset: str = "veryfast",
    ) -> None:
        self.destination = destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = require("ffmpeg")
        args = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}", "-r", f"{max(1.0, fps):.6f}", "-i", "-",
            "-an", "-sn", "-dn",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-f", "mp4", str(destination),
        ]
        self._stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            creationflags=CREATE_NO_WINDOW,
        )

    def write(self, frame: np.ndarray) -> None:
        if self.process.stdin is None:
            raise FrameStreamError("The encoder input closed unexpectedly.")
        try:
            self.process.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
        except (BrokenPipeError, OSError) as exc:
            raise FrameStreamError(self.error_text() or "The encoder stopped early.") from exc

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)

    def abort(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.destination.unlink(missing_ok=True)

    def error_text(self) -> str:
        try:
            self._stderr.seek(0)
            return self._stderr.read().decode("utf-8", "replace")[-1200:]
        except (OSError, ValueError):
            return ""

    def __enter__(self) -> "FrameWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        if exc_info and exc_info[0] is not None:
            self.abort()
            return
        self.close()
        if self.process.returncode != 0:
            raise FrameStreamError(
                self.error_text() or f"The encoder exited with {self.process.returncode}."
            )
