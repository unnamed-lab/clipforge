from __future__ import annotations

import json
from typing import Any, Iterable

from clipforge.db.connection import Database, utc_now
from clipforge.db.util import loads as _loads
from clipforge.db.util import new_id, row_dict as _row
from clipforge.errors import Conflict, NotFound

_UNSET: Any = object()



# --------------------------------------------------------------------------- projects


class ProjectRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, name: str, goals: dict | None = None, settings: dict | None = None,
               profile_id: str | None = None, brief: dict | None = None,
               library_source_id: str | None = None) -> dict:
        project_id = new_id()
        now = utc_now()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO projects(id, name, status, goals, settings, profile_id, brief_json,"
                " library_source_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project_id, name, "draft", json.dumps(goals or {}), json.dumps(settings or {}),
                 profile_id, json.dumps(brief or {}), library_source_id, now, now),
            )
        return self.get(project_id)

    def get(self, project_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("Project not found")
        project = dict(row)
        project["goals"] = _loads(project.pop("goals"), {})
        project["settings"] = _loads(project.pop("settings"), {})
        project["brief"] = _loads(project.pop("brief_json", None), {})
        project["overrides"] = _loads(project.pop("overrides_json", None), {})
        return project

    def list(self) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                """
                SELECT p.*,
                       s.duration_ms, s.width, s.height, s.has_audio, s.original_name,
                       (SELECT COUNT(*) FROM candidates c WHERE c.project_id = p.id) AS candidate_count,
                       (SELECT COUNT(*) FROM renders r WHERE r.project_id = p.id AND r.status = 'completed') AS render_count
                FROM projects p
                LEFT JOIN sources s ON s.project_id = p.id
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["goals"] = _loads(item.pop("goals"), {})
            item["settings"] = _loads(item.pop("settings"), {})
            item["brief"] = _loads(item.pop("brief_json", None), {})
            item["overrides"] = _loads(item.pop("overrides_json", None), {})
            item["has_audio"] = bool(item["has_audio"]) if item["has_audio"] is not None else None
            items.append(item)
        return items

    def update(self, project_id: str, *, name: str | None = None, status: str | None = None,
               goals: dict | None = None, settings: dict | None = None) -> dict:
        assignments: list[str] = []
        values: list[Any] = []
        if name is not None:
            assignments.append("name = ?")
            values.append(name)
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if goals is not None:
            assignments.append("goals = ?")
            values.append(json.dumps(goals))
        if settings is not None:
            assignments.append("settings = ?")
            values.append(json.dumps(settings))
        if not assignments:
            return self.get(project_id)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(project_id)
        with self.db.session() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(assignments)} WHERE id = ?", values)
        return self.get(project_id)

    def touch(self, project_id: str) -> None:
        with self.db.session() as conn:
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id))

    def set_extension(self, project_id: str, *, profile_id: str | None | object = _UNSET,
                      overrides: dict | None = None, brief: dict | None = None) -> dict:
        assignments: list[str] = []
        values: list[Any] = []
        if profile_id is not _UNSET:
            assignments.append("profile_id = ?")
            values.append(profile_id)
        if overrides is not None:
            assignments.append("overrides_json = ?")
            values.append(json.dumps(overrides))
        if brief is not None:
            assignments.append("brief_json = ?")
            values.append(json.dumps(brief))
        if not assignments:
            return self.get(project_id)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(project_id)
        with self.db.session() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(assignments)} WHERE id = ?", values)
        return self.get(project_id)

    def delete(self, project_id: str) -> None:
        with self.db.session() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def exists(self, project_id: str) -> bool:
        with self.db.session() as conn:
            return conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None

    def find_by_library_source(self, source_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM projects WHERE library_source_id = ? ORDER BY created_at DESC", (source_id,)
            ).fetchall()
        return [self.get(row["id"]) for row in rows]


# --------------------------------------------------------------------------- sources


def serialise_source(row: dict) -> dict:
    row = dict(row)
    row.pop("storage_path", None)
    row.pop("probe_json", None)
    row["has_audio"] = bool(row.get("has_audio"))
    return row


class SourceRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, *, project_id: str, kind: str, original_name: str, storage_path: str,
               relative_path: str, size_bytes: int, duration_ms: int, width: int | None,
               height: int | None, fps: float | None, has_audio: bool, video_codec: str | None,
               audio_codec: str | None, container: str | None, source_url: str | None,
               probe: dict) -> dict:
        now = utc_now()
        with self.db.session() as conn:
            existing = conn.execute("SELECT id FROM sources WHERE project_id = ?", (project_id,)).fetchone()
            source_id = existing["id"] if existing else new_id()
            conn.execute(
                """
                INSERT INTO sources(id, project_id, kind, original_name, storage_path, relative_path,
                                    size_bytes, duration_ms, width, height, fps, has_audio, video_codec,
                                    audio_codec, container, source_url, probe_json, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id) DO UPDATE SET
                    kind=excluded.kind, original_name=excluded.original_name,
                    storage_path=excluded.storage_path, relative_path=excluded.relative_path,
                    size_bytes=excluded.size_bytes, duration_ms=excluded.duration_ms,
                    width=excluded.width, height=excluded.height, fps=excluded.fps,
                    has_audio=excluded.has_audio, video_codec=excluded.video_codec,
                    audio_codec=excluded.audio_codec, container=excluded.container,
                    source_url=excluded.source_url, probe_json=excluded.probe_json
                """,
                (source_id, project_id, kind, original_name, storage_path, relative_path, size_bytes,
                 duration_ms, width, height, fps, int(has_audio), video_codec, audio_codec, container,
                 source_url, json.dumps(probe), now),
            )
        return self.get(project_id)

    def get(self, project_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM sources WHERE project_id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("This project has no source video yet")
        return dict(row)

    def find_optional(self, project_id: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM sources WHERE project_id = ?", (project_id,)).fetchone()
        return _row(row)


# ----------------------------------------------------------------------- transcripts


class TranscriptRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, *, project_id: str, source_id: str, fingerprint: str, language: str | None,
             language_probability: float | None, model: str, duration_ms: int, segments: list[dict]) -> dict:
        transcript_id = new_id()
        with self.db.session() as conn:
            conn.execute("DELETE FROM transcripts WHERE project_id = ? AND fingerprint = ?", (project_id, fingerprint))
            conn.execute(
                "INSERT INTO transcripts(id, project_id, source_id, fingerprint, language, language_probability,"
                " model, duration_ms, segments_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (transcript_id, project_id, source_id, fingerprint, language, language_probability, model,
                 duration_ms, json.dumps(segments), utc_now()),
            )
        return self.get(transcript_id)

    def get(self, transcript_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM transcripts WHERE id = ?", (transcript_id,)).fetchone()
        if row is None:
            raise NotFound("Transcript not found")
        return self._decode(dict(row))

    def find_by_fingerprint(self, project_id: str, fingerprint: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM transcripts WHERE project_id = ? AND fingerprint = ? ORDER BY created_at DESC LIMIT 1",
                (project_id, fingerprint),
            ).fetchone()
        return self._decode(dict(row)) if row else None

    def latest(self, project_id: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM transcripts WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (project_id,)
            ).fetchone()
        return self._decode(dict(row)) if row else None

    @staticmethod
    def _decode(row: dict) -> dict:
        row["segments"] = _loads(row.pop("segments_json"), [])
        return row


# --------------------------------------------------------------------- analysis runs


class AnalysisRunRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, project_id: str, settings: dict) -> dict:
        run_id = new_id()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO analysis_runs(id, project_id, status, settings_json, created_at) VALUES(?,?,?,?,?)",
                (run_id, project_id, "running", json.dumps(settings), utc_now()),
            )
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFound("Analysis run not found")
        data = dict(row)
        data["settings"] = _loads(data.pop("settings_json"), {})
        data["warnings"] = _loads(data.pop("warnings_json"), [])
        return data

    def finish(self, run_id: str, *, status: str, provider: str | None, warnings: list[str]) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE analysis_runs SET status = ?, provider = ?, warnings_json = ?, finished_at = ? WHERE id = ?",
                (status, provider, json.dumps(warnings), utc_now(), run_id),
            )

    def latest(self, project_id: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (project_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["settings"] = _loads(data.pop("settings_json"), {})
        data["warnings"] = _loads(data.pop("warnings_json"), [])
        return data


# ----------------------------------------------------------------------- candidates


def serialise_candidate(row: dict) -> dict:
    data = dict(row)
    data["scores"] = _loads(data.pop("scores_json"), {})
    data["warnings"] = _loads(data.pop("warnings_json"), [])
    detail = _loads(data.pop("hook_json", None), {}) or {}
    data["hook_detail"] = detail
    data["hook"] = detail.get("text") or data.get("hook") or ""
    data["brief"] = _loads(data.pop("brief_json", None), {})
    # Older rows store no boundary review at all; the API still owes callers the
    # shape the shared schema documents, with lists rather than missing keys.
    boundary = _loads(data.pop("boundary_json", None), {}) or {}
    boundary.setdefault("evidence", [])
    boundary.setdefault("alternatives", [])
    data["boundary"] = boundary
    data["editorial"] = _loads(data.pop("editorial_json", None), {}) or {}
    data["duration"] = _loads(data.pop("duration_json", None), {}) or {}
    data["packaging"] = _loads(data.pop("packaging_json", None), {}) or {}
    data["summary"] = data.get("summary") or ""
    data["start_ms"] = data.get("user_start_ms") if data.get("user_start_ms") is not None else data["start_ms"]
    data["end_ms"] = data.get("user_end_ms") if data.get("user_end_ms") is not None else data["end_ms"]
    data["duration_ms"] = int(data["end_ms"]) - int(data["start_ms"])
    data["edited"] = data.get("user_start_ms") is not None or data.get("user_end_ms") is not None
    data["start_ms"] = int(data["start_ms"])
    data["end_ms"] = int(data["end_ms"])
    data["overall"] = round(float(data.get("overall") or 0), 1)
    return data


class CandidateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace_for_run(self, project_id: str, run_id: str, candidates: Iterable[dict]) -> int:
        now = utc_now()
        rows = []
        for index, candidate in enumerate(candidates):
            rows.append(
                (
                    candidate["id"], project_id, run_id, index, int(candidate["start_ms"]), int(candidate["end_ms"]),
                    candidate["title"][:200], candidate.get("hook", "")[:300], candidate.get("reason", "")[:2000],
                    candidate.get("category", "moment"), candidate.get("excerpt", "")[:4000],
                    json.dumps(candidate.get("scores", {})), float(candidate.get("overall", 0)),
                    json.dumps(candidate.get("warnings", [])), candidate.get("method", "baseline"),
                    candidate.get("method_version", "1"), candidate.get("duplicate_of"), now, now,
                    json.dumps(candidate.get("hook_detail") or {}), json.dumps(candidate.get("brief") or {}),
                    candidate.get("summary", "")[:600], json.dumps(candidate.get("boundary") or {}),
                    json.dumps(candidate.get("editorial") or {}), json.dumps(candidate.get("duration") or {}),
                )
            )
        with self.db.session() as conn:
            conn.execute("DELETE FROM candidates WHERE project_id = ? AND analysis_run_id = ?", (project_id, run_id))
            conn.executemany(
                """
                INSERT INTO candidates(id, project_id, analysis_run_id, rank, start_ms, end_ms, title, hook,
                    reason, category, excerpt, scores_json, overall, warnings_json, method, method_version,
                    duplicate_of, created_at, updated_at, hook_json, brief_json, summary, boundary_json,
                    editorial_json, duration_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        return len(rows)

    def list_for_project(self, project_id: str, run_id: str | None = None) -> list[dict]:
        with self.db.session() as conn:
            if run_id is None:
                row = conn.execute(
                    "SELECT id FROM analysis_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                if row is None:
                    return []
                run_id = row["id"]
            rows = conn.execute(
                "SELECT * FROM candidates WHERE project_id = ? AND analysis_run_id = ? ORDER BY rank ASC",
                (project_id, run_id),
            ).fetchall()
        return [serialise_candidate(dict(r)) for r in rows]

    def get(self, project_id: str, candidate_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE project_id = ? AND id = ?", (project_id, candidate_id)
            ).fetchone()
        if row is None:
            raise NotFound("Candidate not found")
        return serialise_candidate(dict(row))

    def update(self, project_id: str, candidate_id: str, *, start_ms: int | None, end_ms: int | None,
               decision: str | None, title: str | None, summary: str | None = None,
               hook: dict | None = None) -> dict:
        self.get(project_id, candidate_id)
        assignments: list[str] = []
        values: list[Any] = []
        if start_ms is not None:
            assignments.append("user_start_ms = ?")
            values.append(int(start_ms))
        if end_ms is not None:
            assignments.append("user_end_ms = ?")
            values.append(int(end_ms))
        if decision is not None:
            assignments.append("decision = ?")
            values.append(decision)
        if title is not None:
            assignments.append("title = ?")
            values.append(title[:200])
        if summary is not None:
            assignments.append("summary = ?")
            values.append(summary[:600])
        if hook is not None:
            assignments.append("hook = ?")
            values.append(str(hook.get("text") or "")[:300])
            assignments.append("hook_json = ?")
            values.append(json.dumps(hook))
        if not assignments:
            return self.get(project_id, candidate_id)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.extend([project_id, candidate_id])
        with self.db.session() as conn:
            conn.execute(
                f"UPDATE candidates SET {', '.join(assignments)} WHERE project_id = ? AND id = ?", values
            )
        return self.get(project_id, candidate_id)

    def clear_hook(self, project_id: str, candidate_id: str) -> dict:
        self.get(project_id, candidate_id)
        with self.db.session() as conn:
            conn.execute(
                "UPDATE candidates SET hook = '', hook_json = '{}', updated_at = ?"
                " WHERE project_id = ? AND id = ?",
                (utc_now(), project_id, candidate_id),
            )
        return self.get(project_id, candidate_id)

    def reset_edits(self, project_id: str, candidate_id: str) -> dict:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE candidates SET user_start_ms = NULL, user_end_ms = NULL, updated_at = ?"
                " WHERE project_id = ? AND id = ?",
                (utc_now(), project_id, candidate_id),
            )
        return self.get(project_id, candidate_id)

    def set_editorial(
        self,
        project_id: str,
        candidate_id: str,
        editorial: dict,
        *,
        duration: dict | None = None,
    ) -> dict:
        self.get(project_id, candidate_id)
        assignments = ["editorial_json = ?", "updated_at = ?"]
        values: list[Any] = [json.dumps(editorial), utc_now()]
        if duration is not None:
            assignments.insert(0, "duration_json = ?")
            values.insert(0, json.dumps(duration))
        values.extend([project_id, candidate_id])
        with self.db.session() as conn:
            conn.execute(
                f"UPDATE candidates SET {', '.join(assignments)} WHERE project_id = ? AND id = ?",
                values,
            )
        return self.get(project_id, candidate_id)

    def set_boundary(self, project_id: str, candidate_id: str, changes: dict) -> dict:
        current = self.get(project_id, candidate_id)
        boundary = dict(current.get("boundary") or {})
        boundary.update(changes)
        with self.db.session() as conn:
            conn.execute(
                "UPDATE candidates SET boundary_json = ?, updated_at = ? WHERE project_id = ? AND id = ?",
                (json.dumps(boundary), utc_now(), project_id, candidate_id),
            )
        return self.get(project_id, candidate_id)

    def set_packaging(self, project_id: str, candidate_id: str, packaging: dict) -> dict:
        self.get(project_id, candidate_id)
        with self.db.session() as conn:
            conn.execute(
                "UPDATE candidates SET packaging_json = ?, updated_at = ? WHERE project_id = ? AND id = ?",
                (json.dumps(packaging, ensure_ascii=False), utc_now(), project_id, candidate_id),
            )
        return self.get(project_id, candidate_id)


# ------------------------------------------------------------------------------- jobs


def serialise_job(row: dict) -> dict:
    data = dict(row)
    data["payload"] = _loads(data.pop("payload_json"), {})
    data["result"] = _loads(data.pop("result_json"), None)
    data["cancel_requested"] = bool(data.get("cancel_requested"))
    data["progress"] = round(float(data.get("progress") or 0), 1)
    return data


class JobRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, project_id: str | None, job_type: str, payload: dict | None = None,
               state: str = "queued") -> dict:
        job_id = new_id()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO jobs(id, project_id, type, state, stage, progress, message, payload_json, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, project_id, job_type, state, "queued", 0, "Waiting to start",
                 json.dumps(payload or {}), utc_now()),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("Job not found")
        return serialise_job(dict(row))

    def active_for(self, project_id: str, job_type: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE project_id = ? AND type = ? AND state IN ('queued','running')"
                " ORDER BY created_at DESC LIMIT 1",
                (project_id, job_type),
            ).fetchone()
        return serialise_job(dict(row)) if row else None

    def list_for_project(self, project_id: str, limit: int = 20) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?", (project_id, limit)
            ).fetchall()
        return [serialise_job(dict(r)) for r in rows]

    def claim_next(self) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE state = 'queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                "UPDATE jobs SET state = 'running', stage = 'starting', progress = 0,"
                " message = 'Starting', started_at = ?, attempts = attempts + 1"
                " WHERE id = ? AND state = 'queued'",
                (utc_now(), row["id"]),
            )
            if updated.rowcount == 0:
                return None
        return self.get(row["id"])

    def update_progress(self, job_id: str, *, stage: str, progress: float, message: str) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE jobs SET stage = ?, progress = ?, message = ? WHERE id = ?",
                (stage, max(0.0, min(100.0, round(progress, 1))), message[:300], job_id),
            )

    def is_cancel_requested(self, job_id: str) -> bool:
        with self.db.session() as conn:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def request_cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job["state"] in {"completed", "failed", "cancelled"}:
            raise Conflict("That job has already finished")
        with self.db.session() as conn:
            conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        return self.get(job_id)

    def complete(self, job_id: str, result: dict | None, message: str = "Completed") -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'completed', stage = 'done', progress = 100, message = ?,"
                " result_json = ?, finished_at = ? WHERE id = ?",
                (message[:300], json.dumps(result or {}), utc_now(), job_id),
            )

    def fail(self, job_id: str, *, code: str, message: str) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'failed', stage = 'failed', message = ?, error_code = ?,"
                " error_message = ?, finished_at = ? WHERE id = ?",
                (message[:300], code, message[:500], utc_now(), job_id),
            )

    def cancel(self, job_id: str, message: str = "Cancelled by user") -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'cancelled', stage = 'cancelled', message = ?, finished_at = ?"
                " WHERE id = ?",
                (message[:300], utc_now(), job_id),
            )

    def requeue(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job["state"] == "queued":
            return job
        if job["state"] == "running":
            raise Conflict("That job is still running")
        with self.db.session() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'queued', stage = 'queued', progress = 0, message = 'Waiting to start',"
                " error_code = NULL, error_message = NULL, cancel_requested = 0, started_at = NULL,"
                " finished_at = NULL WHERE id = ?",
                (job_id,),
            )
        return self.get(job_id)

    def mark_orphans(self) -> int:
        with self.db.session() as conn:
            rows = conn.execute("SELECT id FROM jobs WHERE state = 'running'").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET state = 'failed', stage = 'failed', error_code = 'worker_restarted',"
                    " error_message = 'The worker restarted before this job finished. Retry it.',"
                    " finished_at = ? WHERE id = ?",
                    (utc_now(), row["id"]),
                )
        return len(rows)


# ---------------------------------------------------------------------------- renders


def serialise_render(row: dict) -> dict:
    data = dict(row)
    data["spec"] = _loads(data.pop("spec_json"), {})
    data["profile_snapshot"] = _loads(data.pop("profile_snapshot_json", None), {})
    data["review"] = _loads(data.pop("review_json", None), {})
    data.pop("relative_path", None)
    data["download_url"] = f"/api/renders/{data['id']}/download" if data.get("status") == "completed" else None
    return data


class RenderRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, project_id: str, job_id: str | None, candidate_id: str | None, spec: dict,
               status: str = "queued", kind: str = "export", profile_snapshot: dict | None = None,
               edit_spec_id: str | None = None) -> dict:
        render_id = new_id()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO renders(id, project_id, job_id, candidate_id, spec_json, status, kind,"
                " profile_snapshot_json, edit_spec_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (render_id, project_id, job_id, candidate_id, json.dumps(spec), status, kind,
                 json.dumps(profile_snapshot or {}), edit_spec_id, utc_now()),
            )
        return self.get(render_id)

    def get(self, render_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
        if row is None:
            raise NotFound("Render not found")
        return serialise_render(dict(row))

    def get_raw(self, render_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
        if row is None:
            raise NotFound("Render not found")
        return dict(row)

    def list_for_project(self, project_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM renders WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [serialise_render(dict(r)) for r in rows]

    def list_raw_for_project(self, project_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM renders WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_completed(self, render_id: str, *, relative_path: str, size_bytes: int, duration_ms: int,
                       width: int, height: int, subtitle_path: str | None = None) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE renders SET status = 'completed', relative_path = ?, size_bytes = ?, duration_ms = ?,"
                " width = ?, height = ?, subtitle_path = ?, finished_at = ? WHERE id = ?",
                (relative_path, size_bytes, duration_ms, width, height, subtitle_path, utc_now(), render_id),
            )

    def update_spec(self, render_id: str, spec: dict) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE renders SET spec_json = ? WHERE id = ?", (json.dumps(spec), render_id)
            )

    def set_review(self, render_id: str, review: dict) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE renders SET review_json = ? WHERE id = ?", (json.dumps(review), render_id)
            )

    def mark_failed(self, render_id: str, code: str, message: str) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE renders SET status = 'failed', error_code = ?, error_message = ?, finished_at = ?"
                " WHERE id = ?",
                (code, message[:500], utc_now(), render_id),
            )

    def mark_running(self, render_id: str) -> None:
        with self.db.session() as conn:
            conn.execute("UPDATE renders SET status = 'running' WHERE id = ?", (render_id,))

    def attach_job(self, render_id: str, job_id: str) -> None:
        with self.db.session() as conn:
            conn.execute("UPDATE renders SET job_id = ? WHERE id = ?", (job_id, render_id))

    def mark_cancelled(self, render_id: str) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE renders SET status = 'cancelled', finished_at = ? WHERE id = ?", (utc_now(), render_id)
            )


class Repositories:
    """Container so handlers can be injected with one object."""

    def __init__(self, db: Database) -> None:
        from clipforge.db.extensions import (
            BrandAssetRepository,
            BriefRepository,
            CaptionPresetRepository,
            CorrectionRepository,
            DatasetRepository,
            EditSpecRepository,
            EvaluationRepository,
            LibraryImportRepository,
            LibrarySourceRepository,
            ProfileRepository,
            RightsRepository,
            TrendCacheRepository,
        )

        self.db = db
        self.projects = ProjectRepository(db)
        self.sources = SourceRepository(db)
        self.transcripts = TranscriptRepository(db)
        self.runs = AnalysisRunRepository(db)
        self.candidates = CandidateRepository(db)
        self.jobs = JobRepository(db)
        self.renders = RenderRepository(db)
        self.profiles = ProfileRepository(db)
        self.assets = BrandAssetRepository(db)
        self.briefs = BriefRepository(db)
        self.caption_presets = CaptionPresetRepository(db)
        self.corrections = CorrectionRepository(db)
        self.edit_specs = EditSpecRepository(db)
        self.library = LibrarySourceRepository(db)
        self.rights = RightsRepository(db)
        self.library_imports = LibraryImportRepository(db)
        self.evaluations = EvaluationRepository(db)
        self.datasets = DatasetRepository(db)
        self.trend_cache = TrendCacheRepository(db)
