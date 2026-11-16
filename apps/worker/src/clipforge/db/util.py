"""Small row helpers shared by the repository modules."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


def loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
