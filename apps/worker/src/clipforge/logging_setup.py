from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_clipforge_configured", False):
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        logs_dir / "worker.log", maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("faster_whisper").setLevel(logging.INFO)
    root._clipforge_configured = True  # type: ignore[attr-defined]
