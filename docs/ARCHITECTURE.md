# Clipforge - Architecture

> Status: the service boundaries, security rules and job model below still describe the system. The data-model section predates later tables (profiles, briefs, corrections, library, packaging, evaluations and render reviews). Treat `apps/worker/src/clipforge/db/schema.sql` as authoritative for the current schema.

## 1. Shape of the system

```
┌──────────────────────────────────────────────┐        ┌────────────────────────────────────────┐
│ Browser                                      │        │ apps/worker  (127.0.0.1:8788)          │
│  Next.js client components                   │        │  FastAPI + SQLite + job queue          │
│        │                                     │        │        │                               │
│        ▼ same-origin fetch                   │        │        ├── media (FFprobe/FFmpeg)      │
│  Next.js route handler  /api/[...path]  ─────┼───────▶│        ├── transcription (whisper)     │
│  (server). Injects worker token, streams     │ token  │        ├── ranking (baseline/LLM)      │
│  bodies and Range requests, never exposes it │        │        ├── rendering (FFmpeg)          │
│                                              │        │        └── job runner (1 consumer)     │
│  Server components ─── typed worker client ──┼───────▶│                                        │
└──────────────────────────────────────────────┘        │  data/app.db   data/projects/<id>/...  │
                                                        └────────────────────────────────────────┘
```

Rules that follow from this shape:

- The browser talks **only** to the Next.js origin. No CORS, no token in client JS.
- The worker binds `127.0.0.1` by default. Exposure is a deliberate, documented change.
- The worker is the only process that touches media files, the database, or FFmpeg.
- No long-running work ever happens inside an HTTP request that a browser waits on. Requests enqueue; the UI polls job state.

## 2. Service boundaries

### Web (`apps/web`)

Owns: routing, server-rendered project views, the BFF proxy, upload forms, polling, the editor UI, download links.

Does not own: media, transcripts, scoring, rendering, file paths.

Key modules:

| Path | Responsibility |
| --- | --- |
| `src/lib/worker.ts` | Typed server-side client. Injects token. Maps worker errors to `WorkerError`. |
| `src/app/api/[...path]/route.ts` | Pass-through proxy for browser calls (JSON, multipart, Range). |
| `src/lib/format.ts` | Timecode, bytes, duration formatting. Pure, unit-tested. |
| `src/features/*` | Feature slices: projects, import, analysis, candidates, editor, exports. |

### Worker (`apps/worker/src/clipforge`)

| Module | Responsibility |
| --- | --- |
| `api/` | FastAPI app, routes, request/response models, error envelope |
| `db/` | SQLite connection, schema, repositories (one per aggregate) |
| `jobs/` | Queue, runner, job handlers, progress + cancellation |
| `media/` | FFprobe inspection, audio extraction, waveform, validation |
| `pipeline/` | Orchestrated analysis stages |
| `providers/transcription/` | `TranscriptionProvider` protocol + faster-whisper implementation |
| `providers/llm/` | `CandidateScorerProvider` protocol + OpenAI-compatible adapter |
| `providers/diarisation/` | Protocol + `UnavailableDiarisation` (honest stub) |
| `ranking/` | Candidate generation, features, scoring, dedupe |
| `rendering/` | Render spec validation, FFmpeg argv building, ASS subtitles, execution |
| `security/` | Path jail, filename sanitising, URL/SSRF validation, limits |
| `storage/` | Project workspace layout and disk budget |

### Shared contract (`packages/shared`)

Zod schemas for every request/response body and for the render spec. The Python models mirror them; `apps/worker/tests/test_contract.py` asserts both sides agree on the fixtures in `packages/shared/fixtures/`.

## 3. Data model

SQLite, WAL mode, foreign keys on.

```
projects(id, name, created_at, updated_at, goals_json, settings_json, status)
sources(id, project_id, kind, original_name, storage_path, size_bytes,
        duration_ms, width, height, fps, has_audio, probe_json, created_at)
transcripts(id, project_id, source_id, language, language_probability, model,
            duration_ms, segments_json, fingerprint, created_at)
analysis_runs(id, project_id, status, settings_json, provider, warnings_json, created_at, finished_at)
candidates(id, project_id, analysis_run_id, start_ms, end_ms, title, hook, reason,
           category, excerpt, scores_json, overall, warnings_json, method, method_version,
           decision, user_start_ms, user_end_ms, created_at)
jobs(id, project_id, type, state, stage, progress, message, error_code, error_message,
     attempts, payload_json, result_json, cancel_requested, created_at, started_at, finished_at)
renders(id, project_id, job_id, candidate_id, spec_json, status, output_path, size_bytes,
        duration_ms, width, height, created_at, finished_at)
```

Design notes:

- **Effective boundaries**: `COALESCE(user_start_ms, start_ms)`. Analysis output is immutable; user edits live in their own columns. Re-running analysis never silently discards an edit — it creates a new `analysis_run` and new candidates.
- **Immutable source**: `sources.storage_path` is written once. Renders read it; nothing writes to it.
- **Transcript reuse**: `fingerprint = sha256(source content hash | model | compute_type | language | vad)`. Re-analysing with unchanged settings reuses the stored transcript instead of re-running ASR.
- **No paths in JSON returned to the browser.** Paths are internal; downloads are addressed by `render_id`.

## 4. Job model

States: `queued → running → completed | failed | cancelled`.

- One consumer thread, FIFO, `max_workers=1`. Every job runs in that thread; blocking C extensions (ctranslate2, FFmpeg) are safe there and the API stays responsive.
- Progress is written to the DB at stage transitions and at real measurement points:
  - transcription: `max(segment.end) / duration` (real ASR output, not a timer)
  - rendering: FFmpeg `-progress pipe:1` `out_time_us / expected_duration`
  - download import: yt-dlp progress hook bytes / total
- Cancellation is cooperative *between* stages and preemptive *inside* FFmpeg (the runner kills the process group on the cancel event). cancellation leaves the source untouched and the job in `cancelled`.
- Retry re-enqueues the same job row (`attempts += 1`) after clearing the error. Retry is refused while a job for the same project+type is queued or running, so double-clicking "Analyse" cannot start two ASR runs.
- Startup sweep: any job left `running` by a hard kill is marked `failed` with `worker_restarted`, and its cleanup runs. This is what "persistent queue" buys us.

## 5. Provider interfaces

```python
class TranscriptionProvider(Protocol):
    name: str
    def capabilities(self) -> TranscriptionCapabilities: ...
    def transcribe(self, audio: Path, *, options: TranscriptionOptions,
                   on_progress: Callable[[float], None]) -> TranscriptResult: ...

class CandidateScorerProvider(Protocol):
    name: str
    version: str
    def score(self, candidates: Sequence[CandidateDraft], context: ScoringContext)
        -> list[ScoredCandidate]: ...

class DiarisationProvider(Protocol):
    available: bool
    def diarise(self, audio: Path) -> list[SpeakerTurn]: ...
```

`UnavailableDiarisation` returns `available=False` and raises `ProviderUnavailable` if called. The capabilities endpoint reports it as unavailable, so the UI disables speaker controls instead of pretending.

## 6. Rendering

A render is a **validated spec** plus a **pure argv builder** plus an **executor**:

1. `RenderSpec` (Pydantic, versioned) is validated against source bounds and platform limits.
2. `build_ffmpeg_args(spec, paths)` returns a `list[str]` — no shell, ever.
3. The executor runs it with `cwd` set to the render directory (this is what keeps Windows path escaping out of the filter graph), `-progress pipe:1`, a timeout, and a cancel hook.
4. Output is probed before being marked complete. A render that produces no decodable output is a failed render.

Captions are generated as ASS from word timings. One dialogue event per word makes the active word highlight trivial and exact; libass handles the overlap.

## 7. Security model

Local-only threat model: the machine is trusted, the *content* is not.

- Uploads are validated by FFprobe content probe, not by extension or MIME alone.
- Internal filenames are generated UUIDs; the user's filename is stored as metadata only.
- Every path is rebuilt from `data/projects/<uuid>/<known-subdir>/<generated-name>` and then `resolve()`d and checked against the project root. No user string is ever concatenated into a path.
- FFmpeg/yt-dlp are invoked with argv arrays. `shell=True` appears nowhere.
- URL import: scheme allowlist, DNS resolution to public addresses only (blocks RFC1918, loopback, link-local, CGNAT, multicast, metadata IP), redirects revalidated per hop, byte cap, duration cap, hard timeout, `noplaylist`.
- Downloads are addressed by render ID; the path is read from the DB and re-verified inside the project root.
- API errors return a code and a safe message. Stack traces go to the log file only.
- The worker token lives in `data/worker-token` (mode 0600) and is only read by the Next.js server process.

## 8. Failure philosophy

| Failure | Behaviour |
| --- | --- |
| ASR model download fails | Job fails with `model_download_failed`, retryable with a smaller model; source preserved |
| No audio stream | Import succeeds with `has_audio=false`; analysis rejects with a clear message |
| LLM endpoint unreachable | Analysis completes on baseline scores and records a warning |
| FFmpeg render fails | Job fails with the last 40 lines of stderr; source and analysis untouched |
| Worker crash mid-job | On restart the job is marked failed and retryable |
| Disk full | Pre-flight check refuses the job with `insufficient_disk` |

Nothing in this table silently substitutes sample data for a failed operation.
