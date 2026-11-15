"""Process execution helpers.

All external programs are launched with argument arrays — never through a shell.
Long-running processes support progress reporting and cooperative cancellation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from clipforge.errors import DependencyMissing


class ProcessCancelled(Exception):
    pass


class ProcessTimeout(Exception):
    pass


class ProcessFailed(Exception):
    def __init__(self, program: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{program} exited with code {returncode}")
        self.program = program
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float = 0.0
    extra: dict = field(default_factory=dict)


def which(program: str) -> str | None:
    return shutil.which(program)


def require(program: str) -> str:
    resolved = which(program)
    if not resolved:
        raise DependencyMissing(
            f"{program} was not found on PATH. Install FFmpeg and restart the worker.",
            code="ffmpeg_missing",
        )
    return resolved


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 120,
    cancel: threading.Event | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
    stdin_data: bytes | None = None,
) -> ProcessResult:
    started = time.monotonic()
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    process = subprocess.Popen(  # noqa: S603 - argv list, shell disabled
        list(args),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        text=stdin_data is None,
        encoding=None if stdin_data is not None else "utf-8",
        errors=None if stdin_data is not None else "replace",
        creationflags=creationflags,
        env={**os.environ, **(env or {})},
    )

    stderr_tail: deque[str] = deque(maxlen=200)
    stdout_lines: list[str] = []

    def drain(stream, sink: Callable[[str], None]) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                sink(line.rstrip("\r\n"))
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def stderr_sink(line: str) -> None:
        stderr_tail.append(line)

    def stdout_sink(line: str) -> None:
        stdout_lines.append(line)
        if on_stdout_line:
            on_stdout_line(line)

    threads = [
        threading.Thread(target=drain, args=(process.stderr, stderr_sink), daemon=True),
        threading.Thread(target=drain, args=(process.stdout, stdout_sink), daemon=True),
    ]
    for thread in threads:
        thread.start()

    if stdin_data is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin_data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    try:
        while True:
            if cancel is not None and cancel.is_set():
                _terminate(process)
                raise ProcessCancelled()
            if time.monotonic() - started > timeout_seconds:
                _terminate(process)
                raise ProcessTimeout()
            try:
                process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        for thread in threads:
            thread.join(timeout=2)

    stderr = "\n".join(stderr_tail)
    return ProcessResult(
        returncode=process.returncode,
        stdout="\n".join(stdout_lines),
        stderr=stderr,
        duration_seconds=time.monotonic() - started,
    )


def _terminate(process: subprocess.Popen) -> None:
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
