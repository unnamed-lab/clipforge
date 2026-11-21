from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from clipforge import __version__
from clipforge.api import (
    routes_analysis,
    routes_discovery,
    routes_library,
    routes_packaging,
    routes_profiles,
    routes_project_settings,
    routes_projects,
    routes_renders,
    routes_sources,
    routes_system,
)
from clipforge.api.deps import AppState
from clipforge.config import Settings, load_settings
from clipforge.db.connection import Database
from clipforge.db.repositories import Repositories
from clipforge.errors import AppError
from clipforge.jobs.handlers import build_handlers
from clipforge.jobs.queue import JobQueue
from clipforge.logging_setup import configure_logging
from clipforge.storage import build_storage

logger = logging.getLogger(__name__)

PUBLIC_PATHS = {"/api/health"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    configure_logging(settings.logs_dir)

    database = Database(settings.db_path)
    database.initialise()
    repos = Repositories(database)
    storage = build_storage(settings)
    queue = JobQueue(repos, settings, build_handlers(repos), storage)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        queue.start()
        logger.info(
            "Clipforge worker %s ready on %s:%s (data: %s, storage: %s)",
            __version__, settings.host, settings.port, settings.data_dir, storage.name,
        )
        try:
            yield
        finally:
            try:
                queue.stop()
            finally:
                storage.close()

    app = FastAPI(
        title="Clipforge worker",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.clipforge = AppState(settings=settings, repos=repos, queue=queue, storage=storage)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "x-clipforge-token"],
        max_age=600,
    )

    @app.middleware("http")
    async def token_guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api") and path not in PUBLIC_PATHS:
            supplied = request.headers.get("x-clipforge-token", "")
            if not supplied or not hmac.compare_digest(supplied, settings.token):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"code": "unauthorized", "message": "Missing or invalid worker token"}},
                )
        return await call_next(request)

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {"msg": "Invalid request"}
        location = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        message = str(first.get("msg", "Invalid request"))
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": f"{location}: {message}" if location else message,
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "The worker hit an unexpected error. Check the worker log for details.",
                }
            },
        )

    app.include_router(routes_system.router)
    app.include_router(routes_profiles.router)
    app.include_router(routes_projects.router)
    app.include_router(routes_project_settings.router)
    app.include_router(routes_sources.router)
    app.include_router(routes_analysis.router)
    app.include_router(routes_discovery.router)
    app.include_router(routes_renders.router)
    app.include_router(routes_library.router)
    app.include_router(routes_packaging.router)
    return app
