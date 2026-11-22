"""Durable state for one autonomous production run.

The run, its stage checkpoints and its per-clip state are separate rows so a
restart can reuse every completed stage. Nothing here renders or analyses; the
orchestrator owns that and this module only persists what happened.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from clipforge.db.connection import Database, utc_after, utc_now
from clipforge.db.util import loads as _loads
from clipforge.db.util import new_id
from clipforge.errors import Conflict, NotFound

ACTIVE_RUN_STATES = ("queued", "running", "retrying")
FINISHED_RUN_STATES = ("completed", "partially_completed", "cancelled", "failed")


def _decode_run(row: dict) -> dict:
    data = dict(row)
    data["request"] = _loads(data.pop("request_json", None), {})
    data["settings"] = _loads(data.pop("settings_json", None), {})
    data["provider"] = _loads(data.pop("provider_json", None), {})
    data["cancel_requested"] = bool(data.get("cancel_requested"))
    data["progress"] = round(float(data.get("progress") or 0), 1)
    return data


def _decode_stage(row: dict) -> dict:
    data = dict(row)
    data["output"] = _loads(data.pop("output_json", None), {})
    data["progress"] = round(float(data.get("progress") or 0), 1)
    return data


def _decode_clip(row: dict) -> dict:
    data = dict(row)
    data["findings"] = _loads(data.pop("findings_json", None), [])
    data["history"] = _loads(data.pop("history_json", None), [])
    data["plan"] = _loads(data.pop("plan_json", None), {})
    data["review"] = _loads(data.pop("review_json", None), {})
    return data


class ProjectRunRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        project_id: str,
        request: dict,
        settings: dict,
        provider: dict,
        deadline_seconds: float,
    ) -> dict:
        run_id = new_id()
        now = utc_now()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO project_runs(id, project_id, status, current_stage, progress, message,"
                " request_json, settings_json, provider_json, created_at, updated_at, deadline_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    project_id,
                    "queued",
                    None,
                    0,
                    "Waiting to start",
                    json.dumps(request or {}),
                    json.dumps(settings or {}),
                    json.dumps(provider or {}),
                    now,
                    now,
                    utc_after(max(60.0, deadline_seconds)),
                ),
            )
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM project_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFound("Production run not found")
        return _decode_run(dict(row))

    def latest(self, project_id: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM project_runs WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return _decode_run(dict(row)) if row else None

    def list_for_project(self, project_id: str, limit: int = 10) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM project_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [_decode_run(dict(row)) for row in rows]

    def active_for(self, project_id: str) -> dict | None:
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATES)
        with self.db.session() as conn:
            row = conn.execute(
                f"SELECT * FROM project_runs WHERE project_id = ? AND status IN ({placeholders})"
                " ORDER BY created_at DESC LIMIT 1",
                (project_id, *ACTIVE_RUN_STATES),
            ).fetchone()
        return _decode_run(dict(row)) if row else None

    def start(self, run_id: str) -> dict:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_runs SET status = 'running', started_at = COALESCE(started_at, ?),"
                " heartbeat_at = ?, updated_at = ? WHERE id = ?",
                (utc_now(), utc_now(), utc_now(), run_id),
            )
        return self.get(run_id)

    def progress(self, run_id: str, *, progress: float, message: str,
                 stage: str | None = None) -> None:
        assignments = ["progress = ?", "message = ?", "heartbeat_at = ?", "updated_at = ?"]
        values: list[Any] = [
            max(0.0, min(100.0, round(progress, 1))),
            message[:300],
            utc_now(),
            utc_now(),
        ]
        if stage is not None:
            assignments.insert(0, "current_stage = ?")
            values.insert(0, stage)
        values.append(run_id)
        with self.db.session() as conn:
            conn.execute(
                f"UPDATE project_runs SET {', '.join(assignments)} WHERE id = ?", values
            )

    def set_status(self, run_id: str, status: str, *, message: str | None = None,
                   error_code: str | None = None, error_message: str | None = None) -> dict:
        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, utc_now()]
        if message is not None:
            assignments.append("message = ?")
            values.append(message[:300])
        if error_code is not None:
            assignments.append("error_code = ?")
            values.append(error_code)
        if error_message is not None:
            assignments.append("error_message = ?")
            values.append(error_message[:500])
        if status in FINISHED_RUN_STATES:
            assignments.append("finished_at = ?")
            values.append(utc_now())
        elif status in ACTIVE_RUN_STATES:
            assignments.append("finished_at = NULL")
        values.append(run_id)
        with self.db.session() as conn:
            conn.execute(f"UPDATE project_runs SET {', '.join(assignments)} WHERE id = ?", values)
        return self.get(run_id)

    def heartbeat(self, run_id: str) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_runs SET heartbeat_at = ? WHERE id = ?", (utc_now(), run_id)
            )

    def request_cancel(self, run_id: str) -> dict:
        run = self.get(run_id)
        if run["status"] in FINISHED_RUN_STATES:
            raise Conflict("That production run has already finished")
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_runs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                (utc_now(), run_id),
            )
        return self.get(run_id)

    def is_cancel_requested(self, run_id: str) -> bool:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM project_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def cancellable(self, run_id: str) -> bool:
        return self.get(run_id)["status"] not in FINISHED_RUN_STATES

    def mark_resumed(self, run_id: str) -> None:
        """Clear the liveness markers a crash left behind, keeping completed work."""
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_runs SET status = 'retrying', message = 'Resuming after a worker restart',"
                " heartbeat_at = NULL, updated_at = ? WHERE id = ? AND status = 'running'",
                (utc_now(), run_id),
            )

    def set_provider(self, run_id: str, provider: dict) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_runs SET provider_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(provider or {}), utc_now(), run_id),
            )

    def attach_job(self, run_id: str, job_id: str) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_runs SET job_id = ?, updated_at = ? WHERE id = ?",
                (job_id, utc_now(), run_id),
            )


class ProjectStageRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def ensure(self, run_id: str, names: Iterable[str]) -> list[dict]:
        now = utc_now()
        with self.db.session() as conn:
            for name in names:
                conn.execute(
                    "INSERT OR IGNORE INTO project_run_stages(run_id, name, status, updated_at)"
                    " VALUES(?,?,?,?)",
                    (run_id, name, "pending", now),
                )
        return self.list(run_id)

    def list(self, run_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM project_run_stages WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [_decode_stage(dict(row)) for row in rows]

    def get(self, run_id: str, name: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM project_run_stages WHERE run_id = ? AND name = ?", (run_id, name)
            ).fetchone()
        return _decode_stage(dict(row)) if row else None

    def start(self, run_id: str, name: str, *, attempt: int | None = None) -> dict:
        with self.db.session() as conn:
            if attempt is None:
                conn.execute(
                    "UPDATE project_run_stages SET status = 'running', message = 'Running',"
                    " started_at = COALESCE(started_at, ?), updated_at = ?, attempt = attempt + 1,"
                    " error_code = NULL, error_message = NULL WHERE run_id = ? AND name = ?",
                    (utc_now(), utc_now(), run_id, name),
                )
            else:
                conn.execute(
                    "UPDATE project_run_stages SET status = 'running', message = 'Running',"
                    " started_at = COALESCE(started_at, ?), updated_at = ?, attempt = ?,"
                    " error_code = NULL, error_message = NULL WHERE run_id = ? AND name = ?",
                    (utc_now(), utc_now(), attempt, run_id, name),
                )
        stage = self.get(run_id, name)
        assert stage is not None
        return stage

    def progress(self, run_id: str, name: str, *, progress: float, message: str) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_run_stages SET progress = ?, message = ?, updated_at = ?"
                " WHERE run_id = ? AND name = ?",
                (
                    max(0.0, min(100.0, round(progress, 1))),
                    message[:300],
                    utc_now(),
                    run_id,
                    name,
                ),
            )

    def complete(self, run_id: str, name: str, output: dict | None = None,
                 *, message: str = "Completed") -> dict:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_run_stages SET status = 'completed', progress = 100, message = ?,"
                " output_json = ?, finished_at = ?, updated_at = ? WHERE run_id = ? AND name = ?",
                (message[:300], json.dumps(output or {}), utc_now(), utc_now(), run_id, name),
            )
        stage = self.get(run_id, name)
        assert stage is not None
        return stage

    def fail(self, run_id: str, name: str, *, code: str, message: str) -> dict:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_run_stages SET status = 'failed', message = ?, error_code = ?,"
                " error_message = ?, finished_at = ?, updated_at = ? WHERE run_id = ? AND name = ?",
                (message[:300], code, message[:500], utc_now(), utc_now(), run_id, name),
            )
        stage = self.get(run_id, name)
        assert stage is not None
        return stage

    def skip(self, run_id: str, name: str, *, message: str = "Skipped") -> dict:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_run_stages SET status = 'skipped', progress = 100, message = ?,"
                " finished_at = ?, updated_at = ? WHERE run_id = ? AND name = ?",
                (message[:300], utc_now(), utc_now(), run_id, name),
            )
        stage = self.get(run_id, name)
        assert stage is not None
        return stage

    def reset_incomplete(self, run_id: str) -> None:
        """A resumed job re-runs anything that did not finish; completed work stays."""
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_run_stages SET status = 'pending', progress = 0,"
                " error_code = NULL, error_message = NULL, message = 'Pending'"
                " WHERE run_id = ? AND status = 'running'",
                (run_id,),
            )

    def reset_for_retry(self, run_id: str) -> None:
        """Keep completed/skipped stages; make failed and interrupted ones runnable."""
        with self.db.session() as conn:
            conn.execute(
                "UPDATE project_run_stages SET status = 'pending', progress = 0,"
                " error_code = NULL, error_message = NULL, message = 'Pending'"
                " WHERE run_id = ? AND status IN ('failed', 'running')",
                (run_id,),
            )


class ProjectClipRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace(self, run_id: str, project_id: str, clips: list[dict]) -> list[dict]:
        """Persist the planned clips once; existing manual state is preserved.

        Re-planning after a resume is allowed but must not wipe repair history or
        a user's approval, so a matching candidate row is updated in place.
        """
        now = utc_now()
        with self.db.session() as conn:
            existing = {
                row["candidate_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM project_run_clips WHERE run_id = ?", (run_id,)
                ).fetchall()
            }
            for ordinal, clip in enumerate(clips):
                candidate_id = clip.get("candidate_id")
                current = existing.get(candidate_id)
                if current:
                    conn.execute(
                        "UPDATE project_run_clips SET ordinal = ?, strategy = ?, plan_json = ?,"
                        " updated_at = ? WHERE id = ?",
                        (
                            ordinal,
                            str(clip.get("strategy") or ""),
                            json.dumps(clip.get("plan") or {}),
                            now,
                            current["id"],
                        ),
                    )
                    continue
                conn.execute(
                    "INSERT INTO project_run_clips(id, run_id, project_id, candidate_id, ordinal,"
                    " status, strategy, plan_json, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id(),
                        run_id,
                        project_id,
                        candidate_id,
                        ordinal,
                        "planned",
                        str(clip.get("strategy") or ""),
                        json.dumps(clip.get("plan") or {}),
                        now,
                        now,
                    ),
                )
        return self.list(run_id)

    def list(self, run_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM project_run_clips WHERE run_id = ? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return [_decode_clip(dict(row)) for row in rows]

    def clear_render(self, render_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM project_run_clips WHERE render_id = ?", (render_id,)
            ).fetchall()
            review = json.dumps(
                {
                    "status": "needs_review",
                    "summary": "The export for this clip was deleted. Re-render to replace it.",
                    "findings": [],
                    "checks": [],
                    "model_review": {},
                    "remaining_warnings": [],
                }
            )
            conn.execute(
                "UPDATE project_run_clips SET render_id = NULL, status = 'needs_review',"
                " findings_json = '[]', review_json = ?, updated_at = ? WHERE render_id = ?",
                (review, utc_now(), render_id),
            )
        return [_decode_clip(dict(row)) for row in rows]

    def get(self, clip_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM project_run_clips WHERE id = ?", (clip_id,)
            ).fetchone()
        if row is None:
            raise NotFound("Production clip not found")
        return _decode_clip(dict(row))

    def update(self, clip_id: str, **fields: Any) -> dict:
        allowed = {
            "status", "strategy", "edl_version", "render_id", "repair_attempts",
            "technical_errors", "findings", "history", "plan", "review",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Unsupported clip field {key}")
            column = {
                "findings": "findings_json",
                "history": "history_json",
                "plan": "plan_json",
                "review": "review_json",
            }.get(key, key)
            assignments.append(f"{column} = ?")
            values.append(json.dumps(value) if key in {"findings", "history", "plan", "review"} else value)
        if not assignments:
            return self.get(clip_id)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(clip_id)
        with self.db.session() as conn:
            conn.execute(
                f"UPDATE project_run_clips SET {', '.join(assignments)} WHERE id = ?", values
            )
        return self.get(clip_id)

    def append_history(self, clip_id: str, entry: dict) -> dict:
        clip = self.get(clip_id)
        history = list(clip.get("history") or [])
        history.append(entry)
        return self.update(clip_id, history=history)

    def delete(self, clip_id: str) -> dict:
        clip = self.get(clip_id)
        with self.db.session() as conn:
            conn.execute("DELETE FROM project_run_clips WHERE id = ?", (clip_id,))
        return clip

    def counts(self, run_id: str) -> dict[str, int]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM project_run_clips WHERE run_id = ?"
                " GROUP BY status",
                (run_id,),
            ).fetchall()
        counts = {row["status"]: int(row["total"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts


class DirectorCacheRepository:
    """Cache a validated editorial decision against exactly what produced it."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM director_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["result"] = _loads(data.pop("result_json", None), {})
        return data

    def put(self, key: str, *, provider: str, model: str, content_hash: str,
            result: dict) -> None:
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO director_cache(cache_key, provider, model, content_hash, result_json,"
                " created_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(cache_key) DO UPDATE SET provider=excluded.provider,"
                " model=excluded.model, content_hash=excluded.content_hash,"
                " result_json=excluded.result_json, created_at=excluded.created_at",
                (key, provider, model, content_hash, json.dumps(result), utc_now()),
            )


class ExportSaveRepository:
    """Provenance for copies of a render written to the user's export folder."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, *, render_id: str, project_id: str, directory: str, filename: str,
               checksum: str | None, size_bytes: int | None) -> dict:
        now = utc_now()
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT id FROM export_saves WHERE render_id = ? AND directory = ? AND filename = ?",
                (render_id, directory, filename),
            ).fetchone()
            save_id = row["id"] if row else new_id()
            conn.execute(
                "INSERT INTO export_saves(id, render_id, project_id, directory, filename, checksum,"
                " size_bytes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(render_id, directory, filename) DO UPDATE SET"
                " checksum=excluded.checksum, size_bytes=excluded.size_bytes, updated_at=excluded.updated_at",
                (save_id, render_id, project_id, directory, filename, checksum, size_bytes, now, now),
            )
        return self.get(render_id, directory, filename) or {}

    def get(self, render_id: str, directory: str, filename: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM export_saves WHERE render_id = ? AND directory = ? AND filename = ?",
                (render_id, directory, filename),
            ).fetchone()
        return dict(row) if row else None

    def list_for_render(self, render_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM export_saves WHERE render_id = ? ORDER BY created_at DESC",
                (render_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_for_project(self, project_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM export_saves WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_for_render(self, render_id: str) -> None:
        with self.db.session() as conn:
            conn.execute("DELETE FROM export_saves WHERE render_id = ?", (render_id,))
