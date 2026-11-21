from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "6"

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT EXISTS",
# so startup applies each one only when it is missing.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "projects": {
        "profile_id": "TEXT",
        "overrides_json": "TEXT NOT NULL DEFAULT '{}'",
        "brief_json": "TEXT NOT NULL DEFAULT '{}'",
        "library_source_id": "TEXT",
    },
    "analysis_runs": {
        "brief_json": "TEXT NOT NULL DEFAULT '{}'",
        "kind": "TEXT NOT NULL DEFAULT 'initial'",
        "superseded": "INTEGER NOT NULL DEFAULT 0",
        "discovery_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "candidates": {
        "hook_json": "TEXT NOT NULL DEFAULT '{}'",
        "brief_json": "TEXT NOT NULL DEFAULT '{}'",
        "summary": "TEXT NOT NULL DEFAULT ''",
        "boundary_json": "TEXT NOT NULL DEFAULT '{}'",
        "editorial_json": "TEXT NOT NULL DEFAULT '{}'",
        "duration_json": "TEXT NOT NULL DEFAULT '{}'",
        "packaging_json": "TEXT NOT NULL DEFAULT '{}'",
        "moment_type": "TEXT NOT NULL DEFAULT ''",
        "speaker": "TEXT",
    },
    "renders": {
        "kind": "TEXT NOT NULL DEFAULT 'export'",
        "profile_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        "edit_spec_id": "TEXT",
        "subtitle_path": "TEXT",
        "review_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "sources": {
        "has_video": "INTEGER NOT NULL DEFAULT 1",
        "content_hash": "TEXT",
        "download_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "jobs": {
        "metrics_json": "TEXT NOT NULL DEFAULT '{}'",
        "next_attempt_at": "TEXT",
    },
}



def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_after(seconds: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    """Thin SQLite wrapper. One connection per operation keeps thread-safety simple."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 8000")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialise(self) -> None:
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        with self.session() as conn:
            conn.executescript(schema)
            self._apply_column_migrations(conn)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )

    @staticmethod
    def _apply_column_migrations(conn: sqlite3.Connection) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
