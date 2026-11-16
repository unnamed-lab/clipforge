"""Repositories for profiles, briefs, assets, transcripts edits, library and review."""

from __future__ import annotations

import json
from typing import Any

from clipforge.db.connection import Database, utc_now
from clipforge.db.util import loads, new_id
from clipforge.errors import Conflict, NotFound


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


# ------------------------------------------------------------------ client profiles


def serialise_profile(row: dict) -> dict:
    data = dict(row)
    data["document"] = loads(data.pop("document_json", None), {})
    data["archived"] = bool(data.get("archived"))
    return data


class ProfileRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self, *, include_archived: bool = False) -> list[dict]:
        query = "SELECT * FROM client_profiles"
        if not include_archived:
            query += " WHERE archived = 0"
        query += " ORDER BY archived ASC, name COLLATE NOCASE ASC"
        with self.db.session() as conn:
            rows = conn.execute(query).fetchall()
        return [serialise_profile(dict(row)) for row in rows]

    def get(self, profile_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM client_profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise NotFound("Client profile not found")
        return serialise_profile(dict(row))

    def create(self, document: dict) -> dict:
        profile_id = new_id()
        now = utc_now()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO client_profiles(id, name, version, archived, document_json, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (profile_id, str(document["name"])[:120], 1, 0, _json(document), now, now),
            )
        return self.get(profile_id)

    def update(self, profile_id: str, document: dict) -> dict:
        current = self.get(profile_id)
        with self.db.session() as conn:
            conn.execute(
                "UPDATE client_profiles SET name = ?, document_json = ?, version = version + 1, updated_at = ?"
                " WHERE id = ?",
                (str(document["name"])[:120], _json(document), utc_now(), profile_id),
            )
        updated = self.get(profile_id)
        if updated["version"] == current["version"]:
            raise Conflict("Profile update did not change the version")
        return updated

    def duplicate(self, profile_id: str, name: str | None = None) -> dict:
        source = self.get(profile_id)
        document = dict(source["document"])
        suffix = " copy"
        candidate = (name or f"{source['name']}{suffix}").strip()[:120]
        existing = {profile["name"].lower() for profile in self.list(include_archived=True)}
        unique = candidate
        counter = 2
        while unique.lower() in existing:
            unique = f"{candidate} {counter}"[:120]
            counter += 1
        document["name"] = unique
        return self.create(document)

    def set_archived(self, profile_id: str, archived: bool) -> dict:
        self.get(profile_id)
        with self.db.session() as conn:
            conn.execute(
                "UPDATE client_profiles SET archived = ?, updated_at = ? WHERE id = ?",
                (1 if archived else 0, utc_now(), profile_id),
            )
        return self.get(profile_id)

    def delete(self, profile_id: str) -> None:
        self.get(profile_id)
        with self.db.session() as conn:
            conn.execute("UPDATE projects SET profile_id = NULL WHERE profile_id = ?", (profile_id,))
            conn.execute("DELETE FROM brand_assets WHERE profile_id = ?", (profile_id,))
            conn.execute("DELETE FROM client_profiles WHERE id = ?", (profile_id,))


# --------------------------------------------------------------------- brand assets


def serialise_asset(row: dict) -> dict:
    data = dict(row)
    data.pop("storage_path", None)
    data["licence_confirmed"] = bool(data.get("licence_confirmed"))
    data["renderable"] = bool(data.get("renderable", 1))
    data["file_url"] = f"/api/assets/{data['id']}/file"
    return data


class BrandAssetRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        profile_id: str | None,
        project_id: str | None,
        kind: str,
        original_name: str,
        storage_path: str,
        relative_path: str,
        mime: str,
        width: int | None,
        height: int | None,
        size_bytes: int,
        duration_ms: int | None,
        licence_confirmed: bool,
        renderable: bool,
    ) -> dict:
        asset_id = new_id()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO brand_assets(id, profile_id, project_id, kind, original_name, storage_path,"
                " relative_path, mime, width, height, size_bytes, duration_ms, licence_confirmed,"
                " renderable, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, profile_id, project_id, kind, original_name[:200], storage_path, relative_path,
                 mime, width, height, size_bytes, duration_ms, int(licence_confirmed), int(renderable),
                 utc_now()),
            )
        return self.get(asset_id)

    def get(self, asset_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM brand_assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise NotFound("Asset not found")
        return dict(row)

    def list_for_profile(self, profile_id: str, kind: str | None = None) -> list[dict]:
        query = "SELECT * FROM brand_assets WHERE profile_id = ?"
        values: list[Any] = [profile_id]
        if kind:
            query += " AND kind = ?"
            values.append(kind)
        query += " ORDER BY created_at DESC"
        with self.db.session() as conn:
            rows = conn.execute(query, values).fetchall()
        return [serialise_asset(dict(row)) for row in rows]

    def list_for_project(self, project_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM brand_assets WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [serialise_asset(dict(row)) for row in rows]

    def set_licence(self, asset_id: str, confirmed: bool) -> dict:
        self.get(asset_id)
        with self.db.session() as conn:
            conn.execute(
                "UPDATE brand_assets SET licence_confirmed = ? WHERE id = ?", (int(confirmed), asset_id)
            )
        return serialise_asset(self.get(asset_id))

    def delete(self, asset_id: str) -> dict:
        asset = self.get(asset_id)
        with self.db.session() as conn:
            conn.execute("DELETE FROM brand_assets WHERE id = ?", (asset_id,))
        return asset


# ------------------------------------------------------------------- clipping briefs


class BriefRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def current(self, project_id: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM clipping_briefs WHERE project_id = ? ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["brief"] = loads(data.pop("brief_json", None), {})
        return data

    def save(self, project_id: str, brief: dict) -> dict:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM clipping_briefs WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            version = int(row["version"]) + 1
            brief_id = new_id()
            now = utc_now()
            conn.execute(
                "INSERT INTO clipping_briefs(id, project_id, version, brief_json, created_at) VALUES(?,?,?,?,?)",
                (brief_id, project_id, version, _json(brief), now),
            )
            conn.execute(
                "UPDATE projects SET brief_json = ?, updated_at = ? WHERE id = ?",
                (_json(brief), now, project_id),
            )
        return {"id": brief_id, "project_id": project_id, "version": version, "brief": brief, "created_at": now}

    def history(self, project_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM clipping_briefs WHERE project_id = ? ORDER BY version DESC", (project_id,)
            ).fetchall()
        items = []
        for row in rows:
            data = dict(row)
            data["brief"] = loads(data.pop("brief_json", None), {})
            items.append(data)
        return items


# ------------------------------------------------------------------ caption presets


class CaptionPresetRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute("SELECT * FROM caption_presets ORDER BY name COLLATE NOCASE").fetchall()
        items = []
        for row in rows:
            data = dict(row)
            data["config"] = loads(data.pop("config_json", None), {})
            data["builtin"] = bool(data["builtin"])
            items.append(data)
        return items

    def create(self, name: str, config: dict) -> dict:
        preset_id = new_id()
        now = utc_now()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO caption_presets(id, name, config_json, builtin, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (preset_id, name[:80], _json(config), 0, now, now),
            )
        return {"id": preset_id, "name": name[:80], "config": config, "builtin": False}

    def delete(self, preset_id: str) -> None:
        with self.db.session() as conn:
            row = conn.execute("SELECT builtin FROM caption_presets WHERE id = ?", (preset_id,)).fetchone()
            if row is None:
                raise NotFound("Caption preset not found")
            if row["builtin"]:
                raise Conflict("Built-in presets cannot be deleted")
            conn.execute("DELETE FROM caption_presets WHERE id = ?", (preset_id,))


# -------------------------------------------------------------- transcript corrections


class CorrectionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self, project_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM transcript_corrections WHERE project_id = ? ORDER BY word_index", (project_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert(self, project_id: str, word_index: int, *, text: str | None, start_ms: int | None,
               end_ms: int | None) -> dict:
        if start_ms is not None and end_ms is not None and end_ms <= start_ms:
            raise Conflict("Correction end must be after its start")
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO transcript_corrections(project_id, word_index, text, start_ms, end_ms, updated_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(project_id, word_index) DO UPDATE SET text = excluded.text,"
                " start_ms = excluded.start_ms, end_ms = excluded.end_ms, updated_at = excluded.updated_at",
                (project_id, word_index, text, start_ms, end_ms, utc_now()),
            )
        return {"project_id": project_id, "word_index": word_index, "text": text,
                "start_ms": start_ms, "end_ms": end_ms}

    def reset(self, project_id: str, word_index: int | None = None) -> int:
        with self.db.session() as conn:
            if word_index is None:
                result = conn.execute(
                    "DELETE FROM transcript_corrections WHERE project_id = ?", (project_id,)
                )
            else:
                result = conn.execute(
                    "DELETE FROM transcript_corrections WHERE project_id = ? AND word_index = ?",
                    (project_id, word_index),
                )
        return result.rowcount


# -------------------------------------------------------------------- edit specs


class EditSpecRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, project_id: str, candidate_id: str | None, profile_id: str | None,
               spec: dict, profile_snapshot: dict) -> dict:
        spec_id = new_id()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO edit_specs(id, project_id, candidate_id, profile_id, spec_json,"
                " profile_snapshot_json, created_at) VALUES(?,?,?,?,?,?,?)",
                (spec_id, project_id, candidate_id, profile_id, _json(spec), _json(profile_snapshot), utc_now()),
            )
        return self.get(spec_id)

    def get(self, spec_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM edit_specs WHERE id = ?", (spec_id,)).fetchone()
        if row is None:
            raise NotFound("Edit specification not found")
        data = dict(row)
        data["spec"] = loads(data.pop("spec_json", None), {})
        data["profile_snapshot"] = loads(data.pop("profile_snapshot_json", None), {})
        return data

    def list_for_project(self, project_id: str, limit: int = 20) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM edit_specs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        items = []
        for row in rows:
            data = dict(row)
            data["spec"] = loads(data.pop("spec_json", None), {})
            data["profile_snapshot"] = loads(data.pop("profile_snapshot_json", None), {})
            items.append(data)
        return items


# ------------------------------------------------------------------ library sources


def serialise_library_source(row: dict) -> dict:
    data = dict(row)
    data["metadata"] = loads(data.pop("metadata_json", None), {})
    return data


class LibrarySourceRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, platform: str, connector: str, url: str, title: str, creator: str | None,
               external_id: str | None = None, duration_ms: int | None = None,
               thumbnail_url: str | None = None, language: str | None = None,
               description: str | None = None, published_at: str | None = None,
               metadata_status: str = "manual", metadata: dict | None = None) -> dict:
        existing = self.find_by_url(url)
        if existing is not None:
            return existing
        source_id = new_id()
        now = utc_now()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO library_sources(id, platform, connector, external_id, url, title, creator,"
                " duration_ms, thumbnail_url, language, description, published_at, metadata_status,"
                " metadata_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, platform, connector, external_id, url, title[:300], (creator or "")[:200] or None,
                 duration_ms, thumbnail_url, language, (description or "")[:2000] or None, published_at,
                 metadata_status, _json(metadata or {}), now, now),
            )
        return self.get(source_id)

    def get(self, source_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM library_sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise NotFound("Content source not found")
        return serialise_library_source(dict(row))

    def find_by_url(self, url: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM library_sources WHERE url = ?", (url,)).fetchone()
        return serialise_library_source(dict(row)) if row else None

    def list(self, *, platform: str | None = None, availability: str | None = None,
             query: str | None = None, limit: int = 100) -> list[dict]:
        clause = "WHERE 1=1"
        values: list[Any] = []
        if platform:
            clause += " AND platform = ?"
            values.append(platform)
        if availability:
            clause += " AND availability = ?"
            values.append(availability)
        if query:
            clause += " AND (title LIKE ? OR creator LIKE ?)"
            values.extend([f"%{query}%", f"%{query}%"])
        values.append(limit)
        with self.db.session() as conn:
            rows = conn.execute(
                f"SELECT * FROM library_sources {clause} ORDER BY created_at DESC LIMIT ?", values
            ).fetchall()
        return [serialise_library_source(dict(row)) for row in rows]

    def update(self, source_id: str, **fields: Any) -> dict:
        self.get(source_id)
        allowed = {
            "title", "creator", "duration_ms", "thumbnail_url", "language", "description", "published_at",
            "availability", "availability_note", "metadata_status", "external_id",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key == "metadata_json" or key == "metadata":
                assignments.append("metadata_json = ?")
                values.append(_json(value))
            elif key in allowed:
                assignments.append(f"{key} = ?")
                values.append(value)
        if not assignments:
            return self.get(source_id)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(source_id)
        with self.db.session() as conn:
            conn.execute(
                f"UPDATE library_sources SET {', '.join(assignments)} WHERE id = ?", values
            )
        return self.get(source_id)

    def mark_checked(self, source_id: str, availability: str, note: str | None = None) -> dict:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE library_sources SET availability = ?, availability_note = ?, last_checked_at = ?,"
                " updated_at = ? WHERE id = ?",
                (availability, note, utc_now(), utc_now(), source_id),
            )
        return self.get(source_id)

    def delete(self, source_id: str) -> None:
        self.get(source_id)
        with self.db.session() as conn:
            conn.execute("DELETE FROM library_sources WHERE id = ?", (source_id,))


# -------------------------------------------------------------------------- rights


def serialise_rights(row: dict) -> dict:
    data = dict(row)
    data["summary"] = permission_summary(data)
    return data


PERMISSION_LABELS = {
    "reference": "Reference / view",
    "import_media": "Download and import",
    "transcribe": "Transcribe and analyse",
    "edit_export": "Edit and export clips",
    "commercial_reuse": "Commercial reuse",
    "train_model": "Model training",
}
PERMISSION_STATES = {"allowed", "denied", "unknown"}


def permission_summary(rights: dict | None) -> dict:
    rights = rights or {}
    summary = {}
    for key in PERMISSION_LABELS:
        state = rights.get(key) or "unknown"
        summary[key] = state if state in PERMISSION_STATES else "unknown"
    summary["review_status"] = rights.get("review_status") or "needs_review"
    return summary


class RightsRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, source_id: str) -> dict | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM source_rights WHERE source_id = ?", (source_id,)).fetchone()
        return serialise_rights(dict(row)) if row else None

    def upsert(self, source_id: str, values: dict) -> dict:
        current = self.get(source_id) or {"source_id": source_id}
        merged = {**current, **{key: value for key, value in values.items() if value is not None}}
        states = {key: merged.get(key, "unknown") for key in PERMISSION_LABELS}
        review_status = values.get("review_status") or _review_status(states, merged)
        now = utc_now()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO source_rights(source_id, reference, import_media, transcribe, edit_export,"
                " commercial_reuse, train_model, licence, licence_url, evidence, attribution,"
                " permission_source, restrictions, notes, review_status, reviewed_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(source_id) DO UPDATE SET reference = excluded.reference,"
                " import_media = excluded.import_media, transcribe = excluded.transcribe,"
                " edit_export = excluded.edit_export, commercial_reuse = excluded.commercial_reuse,"
                " train_model = excluded.train_model, licence = excluded.licence,"
                " licence_url = excluded.licence_url, evidence = excluded.evidence,"
                " attribution = excluded.attribution, permission_source = excluded.permission_source,"
                " restrictions = excluded.restrictions, notes = excluded.notes,"
                " review_status = excluded.review_status, reviewed_at = excluded.reviewed_at,"
                " updated_at = excluded.updated_at",
                (source_id, states["reference"], states["import_media"], states["transcribe"],
                 states["edit_export"], states["commercial_reuse"], states["train_model"],
                 merged.get("licence"), merged.get("licence_url"), merged.get("evidence"),
                 merged.get("attribution"), merged.get("permission_source"),
                 merged.get("restrictions"), merged.get("notes"), review_status,
                 merged.get("reviewed_at") or (now if review_status == "reviewed" else None), now),
            )
        return self.get(source_id)


def _review_status(states: dict[str, str], merged: dict) -> str:
    if all(state in {"allowed", "denied"} for state in states.values()):
        return "reviewed"
    if any(state == "allowed" for state in states.values()):
        return "partial"
    return "needs_review"


class LibraryImportRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def log(self, *, source_id: str, project_id: str | None, operation: str, status: str,
            detail: str | None = None) -> dict:
        import_id = new_id()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO library_imports(id, source_id, project_id, operation, status, detail, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (import_id, source_id, project_id, operation, status, detail, utc_now()),
            )
        return {"id": import_id, "source_id": source_id, "project_id": project_id,
                "operation": operation, "status": status, "detail": detail}

    def list_for_source(self, source_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM library_imports WHERE source_id = ? ORDER BY created_at DESC", (source_id,)
            ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------- evaluation


RATING_KEYS = (
    "relevance", "hook", "context", "pacing", "audio", "captions", "framing", "brand", "overall",
)


def serialise_evaluation(row: dict) -> dict:
    data = dict(row)
    data["ratings"] = loads(data.pop("ratings_json", None), {})
    data["brief"] = loads(data.pop("brief_json", None), {})
    data["model_config"] = loads(data.pop("model_config_json", None), {})
    data["edit_spec"] = loads(data.pop("edit_spec_json", None), {})
    return data


class EvaluationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, project_id: str, candidate_id: str | None, render_id: str | None,
               library_source_id: str | None, decision: str, ratings: dict, brief: dict,
               model_config: dict, edit_spec: dict, rejection_reason: str | None = None,
               notes: str | None = None) -> dict:
        evaluation_id = new_id()
        now = utc_now()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO evaluations(id, project_id, candidate_id, render_id, library_source_id,"
                " decision, rejection_reason, notes, ratings_json, brief_json, model_config_json,"
                " edit_spec_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (evaluation_id, project_id, candidate_id, render_id, library_source_id, decision,
                 rejection_reason, notes, _json(ratings), _json(brief), _json(model_config),
                 _json(edit_spec), now, now),
            )
        return self.get(evaluation_id)

    def get(self, evaluation_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)).fetchone()
        if row is None:
            raise NotFound("Evaluation not found")
        return serialise_evaluation(dict(row))

    def list_for_project(self, project_id: str) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluations WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [serialise_evaluation(dict(row)) for row in rows]

    def list_all(self, limit: int = 500) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluations ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [serialise_evaluation(dict(row)) for row in rows]

    def delete(self, evaluation_id: str) -> None:
        self.get(evaluation_id)
        with self.db.session() as conn:
            conn.execute("DELETE FROM evaluations WHERE id = ?", (evaluation_id,))


# ------------------------------------------------------------------------- datasets


def serialise_dataset(row: dict, items: list[dict] | None = None) -> dict:
    data = dict(row)
    data["filters"] = loads(data.pop("filters_json", None), {})
    if items is not None:
        data["items"] = items
    return data


class DatasetRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, name: str, filters: dict, seed: int, items: list[dict]) -> dict:
        dataset_id = new_id()
        now = utc_now()
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO datasets(id, name, version, filters_json, seed, created_at) VALUES(?,?,?,?,?,?)",
                (dataset_id, name[:120], 1, _json(filters), seed, now),
            )
            conn.executemany(
                "INSERT INTO dataset_items(dataset_id, evaluation_id, source_id, split, included,"
                " exclusion_reason) VALUES(?,?,?,?,?,?)",
                [
                    (dataset_id, item["evaluation_id"], item.get("source_id"), item["split"],
                     int(item.get("included", 1)), item.get("exclusion_reason"))
                    for item in items
                ],
            )
        return self.get(dataset_id)

    def get(self, dataset_id: str) -> dict:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
            if row is None:
                raise NotFound("Dataset not found")
            items = conn.execute(
                "SELECT * FROM dataset_items WHERE dataset_id = ? ORDER BY split, evaluation_id",
                (dataset_id,),
            ).fetchall()
        decoded = []
        for item in items:
            data = dict(item)
            data["included"] = bool(data["included"])
            decoded.append(data)
        return serialise_dataset(dict(row), decoded)

    def list(self) -> list[dict]:
        with self.db.session() as conn:
            rows = conn.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
        return [serialise_dataset(dict(row)) for row in rows]

    def freeze(self, dataset_id: str) -> dict:
        with self.db.session() as conn:
            conn.execute("UPDATE datasets SET frozen_at = ? WHERE id = ?", (utc_now(), dataset_id))
        return self.get(dataset_id)

    def delete(self, dataset_id: str) -> None:
        with self.db.session() as conn:
            conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
