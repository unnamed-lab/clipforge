PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',
    goals       TEXT NOT NULL DEFAULT '{}',
    settings    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    original_name   TEXT NOT NULL,
    storage_path    TEXT NOT NULL,
    relative_path   TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    width           INTEGER,
    height          INTEGER,
    fps             REAL,
    has_audio       INTEGER NOT NULL DEFAULT 0,
    video_codec     TEXT,
    audio_codec     TEXT,
    container       TEXT,
    source_url      TEXT,
    probe_json      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcripts (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_id             TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    fingerprint           TEXT NOT NULL,
    language              TEXT,
    language_probability  REAL,
    model                 TEXT NOT NULL,
    duration_ms           INTEGER NOT NULL DEFAULT 0,
    segments_json         TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcripts_fingerprint ON transcripts(project_id, fingerprint);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'running',
    settings_json  TEXT NOT NULL DEFAULT '{}',
    provider       TEXT,
    warnings_json  TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    rank            INTEGER NOT NULL DEFAULT 0,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    title           TEXT NOT NULL,
    hook            TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'moment',
    excerpt         TEXT NOT NULL DEFAULT '',
    scores_json     TEXT NOT NULL DEFAULT '{}',
    overall         REAL NOT NULL DEFAULT 0,
    warnings_json   TEXT NOT NULL DEFAULT '[]',
    method          TEXT NOT NULL,
    method_version  TEXT NOT NULL,
    decision        TEXT NOT NULL DEFAULT 'proposed',
    user_start_ms   INTEGER,
    user_end_ms     INTEGER,
    duplicate_of    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidates(project_id, analysis_run_id, rank);

CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    project_id       TEXT REFERENCES projects(id) ON DELETE CASCADE,
    type             TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'queued',
    stage            TEXT NOT NULL DEFAULT 'queued',
    progress         REAL NOT NULL DEFAULT 0,
    message          TEXT NOT NULL DEFAULT '',
    error_code       TEXT,
    error_message    TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    result_json      TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, type, state);

CREATE TABLE IF NOT EXISTS renders (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id        TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    candidate_id  TEXT REFERENCES candidates(id) ON DELETE SET NULL,
    spec_json     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    relative_path TEXT,
    size_bytes    INTEGER,
    duration_ms   INTEGER,
    width         INTEGER,
    height        INTEGER,
    error_code    TEXT,
    error_message TEXT,
    created_at    TEXT NOT NULL,
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_renders_project ON renders(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
