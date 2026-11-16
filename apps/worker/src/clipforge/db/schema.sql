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

CREATE TABLE IF NOT EXISTS client_profiles (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    archived      INTEGER NOT NULL DEFAULT 0,
    document_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brand_assets (
    id                TEXT PRIMARY KEY,
    profile_id        TEXT REFERENCES client_profiles(id) ON DELETE SET NULL,
    project_id        TEXT REFERENCES projects(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,
    original_name     TEXT NOT NULL,
    storage_path      TEXT NOT NULL,
    relative_path     TEXT NOT NULL,
    mime              TEXT NOT NULL,
    width             INTEGER,
    height            INTEGER,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    duration_ms       INTEGER,
    licence_confirmed INTEGER NOT NULL DEFAULT 0,
    renderable        INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_brand_assets_profile ON brand_assets(profile_id, kind);

CREATE TABLE IF NOT EXISTS clipping_briefs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    brief_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clipping_briefs_project ON clipping_briefs(project_id, version DESC);

CREATE TABLE IF NOT EXISTS caption_presets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_corrections (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    word_index  INTEGER NOT NULL,
    text        TEXT,
    start_ms    INTEGER,
    end_ms      INTEGER,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, word_index)
);

CREATE TABLE IF NOT EXISTS edit_specs (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    candidate_id          TEXT REFERENCES candidates(id) ON DELETE SET NULL,
    profile_id            TEXT,
    spec_json             TEXT NOT NULL,
    profile_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edit_specs_project ON edit_specs(project_id, created_at DESC);


CREATE TABLE IF NOT EXISTS library_sources (
    id                TEXT PRIMARY KEY,
    platform          TEXT NOT NULL,
    connector         TEXT NOT NULL,
    external_id       TEXT,
    url               TEXT NOT NULL,
    title             TEXT NOT NULL,
    creator           TEXT,
    duration_ms       INTEGER,
    thumbnail_url     TEXT,
    language          TEXT,
    description       TEXT,
    published_at      TEXT,
    availability      TEXT NOT NULL DEFAULT 'unknown',
    availability_note TEXT,
    metadata_status   TEXT NOT NULL DEFAULT 'manual',
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_checked_at   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_library_sources_url ON library_sources(url);

CREATE TABLE IF NOT EXISTS source_rights (
    source_id        TEXT PRIMARY KEY REFERENCES library_sources(id) ON DELETE CASCADE,
    reference        TEXT NOT NULL DEFAULT 'unknown',
    import_media     TEXT NOT NULL DEFAULT 'unknown',
    transcribe       TEXT NOT NULL DEFAULT 'unknown',
    edit_export      TEXT NOT NULL DEFAULT 'unknown',
    commercial_reuse TEXT NOT NULL DEFAULT 'unknown',
    train_model      TEXT NOT NULL DEFAULT 'unknown',
    licence          TEXT,
    licence_url      TEXT,
    evidence         TEXT,
    attribution      TEXT,
    permission_source TEXT,
    restrictions     TEXT,
    notes            TEXT,
    review_status    TEXT NOT NULL DEFAULT 'needs_review',
    reviewed_at      TEXT,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_rights_status ON source_rights(review_status);

CREATE TABLE IF NOT EXISTS library_imports (
    id           TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES library_sources(id) ON DELETE CASCADE,
    project_id   TEXT REFERENCES projects(id) ON DELETE SET NULL,
    operation    TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_library_imports_source ON library_imports(source_id, created_at DESC);

CREATE TABLE IF NOT EXISTS evaluations (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    candidate_id     TEXT REFERENCES candidates(id) ON DELETE SET NULL,
    render_id        TEXT REFERENCES renders(id) ON DELETE SET NULL,
    library_source_id TEXT REFERENCES library_sources(id) ON DELETE SET NULL,
    decision         TEXT NOT NULL,
    rejection_reason TEXT,
    notes            TEXT,
    ratings_json     TEXT NOT NULL DEFAULT '{}',
    brief_json       TEXT NOT NULL DEFAULT '{}',
    model_config_json TEXT NOT NULL DEFAULT '{}',
    edit_spec_json   TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluations_project ON evaluations(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS datasets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    filters_json TEXT NOT NULL DEFAULT '{}',
    seed        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    frozen_at   TEXT
);

CREATE TABLE IF NOT EXISTS dataset_items (
    dataset_id       TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    evaluation_id    TEXT NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    source_id        TEXT REFERENCES library_sources(id) ON DELETE SET NULL,
    split            TEXT NOT NULL,
    included         INTEGER NOT NULL DEFAULT 1,
    exclusion_reason TEXT,
    PRIMARY KEY (dataset_id, evaluation_id)
);
