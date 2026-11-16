from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.jobs.queue import JobQueue
from clipforge.storage.base import StorageBackend


@dataclass
class AppState:
    settings: Settings
    repos: Repositories
    queue: JobQueue
    storage: StorageBackend


def get_state(request: Request) -> AppState:
    return request.app.state.clipforge
