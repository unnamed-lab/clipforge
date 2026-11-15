from __future__ import annotations

import os
import sys

import uvicorn

from clipforge.config import load_settings


def main() -> None:
    settings = load_settings()
    reload_enabled = os.environ.get("CLIPFORGE_RELOAD", "").strip().lower() in {"1", "true", "yes"}
    if reload_enabled:
        uvicorn.run(
            "clipforge.api.app:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            reload=True,
            reload_dirs=[str(os.path.dirname(os.path.dirname(__file__)))],
            log_level="info",
        )
    else:
        from clipforge.api.app import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
