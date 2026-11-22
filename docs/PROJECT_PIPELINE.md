# Automatic production pipeline

Clipforge can run a whole project without a settings form: analyse the source,
choose moments, plan each edit, configure the frame, render, inspect the actual
file, repair supported defects and organise versioned exports.

The workflow is durable. Every stage and every clip has a database checkpoint,
so a restart resumes after the last completed stage. It never re-runs
transcription because a caption preset or export name changed.

## What owns what

| Concern | Owner |
| --- | --- |
| Media decoding, composition, captions, encoding | Existing FFmpeg renderer (`rendering/`) |
| Edit decision list and output timestamps | `RenderSpec` / `to_edl()` |
| Which moments become clips and how they are cut | Editorial director (`editorial/director.py`) |
| Deterministic validation of the encoded file | `quality/gate.py` + `quality/review.py` |
| Stage state, retries, cancellation, repair budget | `pipeline/orchestrator.py` + SQLite tables |
| Provider selection and privacy | `providers/llm/registry.py` |

There is one renderer. The pipeline does not produce video itself; it creates a
normal `renders` row with a normal `RenderSpec` and calls `perform_render`.

## Stages

1. `SOURCE_VALIDATION` - project, source row, readable file, duration, video stream.
2. `SOURCE_ANALYSIS` - reuses a stored transcript and candidate set; otherwise runs the existing analysis pipeline once.
3. `MOMENT_DISCOVERY` - reuses current candidates. If the director asks for more, one targeted discovery pass may run against the stored transcript.
4. `EDITORIAL_PLANNING` - the provider-independent director selects candidate IDs and returns source-time segments, strategies, hooks, risks and evidence.
5. `PLAN_VALIDATION` - rejects unknown candidates, spans outside the source, clips under two seconds and unimplemented strategies.
6. `CONFIGURATION` - resolves project/profile/request precedence through `rendering/intake.py` and builds one `RenderSpec` per clip.
7. `RENDERING` - creates a versioned render row and calls the existing renderer. A technical failure is retried separately from an editorial repair.
8. `RENDER_VALIDATION` - maps FFprobe/FFmpeg gate measurements, EDL checks, transcript boundaries and measured audio levels into findings.
9. `EDITORIAL_REVIEW` - optional model review of measured evidence. It is told it has not seen the frames and cannot claim otherwise.
10. `REPAIR_PLANNING` - selects at most one supported mechanical repair per iteration.
11. `RENDER_REPAIR` - applies the repair, increments the EDL version, renders a new version and re-reviews. Bounded by `CLIPFORGE_AUTOPRODUCE_REPAIR_LIMIT` (default 2).
12. `FINAL_QA` - requires completed streams, a persisted gate report and a checksum. Clips that still fail are `needs_review`, never "complete".
13. `PACKAGE_DELIVERABLES` - marks the best valid render current, preserves superseded versions and writes `manifest.json`.
14. `COMPLETED` - terminal stage. The run status is `completed`, `partially_completed`, `awaiting_review`, `cancelled` or `failed`.

## Run and clip states

Run: `queued`, `running`, `retrying`, `awaiting_review`, `partially_completed`,
`completed`, `cancelled`, `failed`.

Clip: `pending`, `planned`, `configured`, `rendering`, `rendered`,
`requires_repair`, `repair_planned`, `needs_review`, `passed`,
`passed_with_warnings`, `failed`.

Progress is written by the worker from real stage execution. There is no
time-based fake percentage.

## Supported automatic repairs

- Caption line too wide or outside its safe band: reduce font size, words per
  line and safe width, then re-render and re-check.
- Suspicious or mid-sentence ending with a confident sentence alternative:
  move the last out point to that sentence boundary, only if the clip stays at
  least two seconds long.
- Exact duplicate source span: remove the duplicate.
- Crop likely cutting left/right on-screen content: contain that scene with a
  blurred background.

Everything else - unreadable slides, uncertain webcam regions, measured audio
clipping, model narrative observations - is reported with evidence and left for
a person. Repairs are not applied indefinitely: one attempt per supported kind
per clip and a configurable total limit.

## Timeouts, stalls and cancellation

Four separate mechanisms:

- **Stage timeout** (`CLIPFORGE_PIPELINE_STAGE_TIMEOUT_SECONDS`, default 1800).
  Long stages report progress; a stage that exceeds its limit is stopped with
  `stage_timeout`.
- **Stall detection** (`CLIPFORGE_PIPELINE_STALL_SECONDS`, default 300). The job
  queue watchdog treats a missing heartbeat as a stall, not as "slow". Slow but
  active work keeps a fresh heartbeat.
- **Hard deadline** (`CLIPFORGE_PIPELINE_DEADLINE_SECONDS`, default 4 hours).
  Persisted on the run and on the job. Completed work and previous exports are
  kept.
- **Render timeout** (`CLIPFORGE_RENDER_TIMEOUT_SECONDS`, default 2400) passed
  to the existing FFmpeg executor.

Cancellation is cooperative between clips and preemptive inside FFmpeg. A
cancelled run keeps completed stages and every valid export.

## Export identity

- The database `renders.id` is the stable asset id. A filename is metadata.
- A new version is created when the edit decisions, source identity or render
  settings change.
- The same source, EDL, settings and model versions reuse the existing current
  render instead of creating a duplicate.
- Earlier valid versions stay downloadable and are marked `superseded`.
- Downloads use a readable name such as
  `customer-perspective__hook-first__portrait__v001.mp4`.
- Approved clips are also copied to `Documents/Clipforge/<project-slug>/` by
  default, or to the folder set in the project's production options. This is a
  delivered copy: deleting an export in Clipforge does not delete a copy that
  was already written there.
- Removing a clip from the production list keeps its export versions. Deleting
  an export is a separate explicit action.

## Manifest

`GET /api/projects/{id}/manifest` returns the database-backed manifest. A copy
is written to `data/projects/<id>/manifest.json` when the local workspace is
writable. It records source identity, analysis run, pipeline engine, export
asset ids, versions, checksums, review status and provenance. It never includes
filesystem paths or private storage keys.

## Provider policy

- No provider is required. With none configured, the deterministic planner uses
  the existing candidate ranking, hooks and timeline.
- `CLIPFORGE_LLM_PROVIDER=ollama` uses a local OpenAI-compatible endpoint
  (`http://127.0.0.1:11434/v1` by default). Set the model explicitly; Clipforge
  does not download weights at startup.
- Any other OpenAI-compatible endpoint is treated as remote. It is refused
  unless `CLIPFORGE_LLM_ALLOW_REMOTE=true` is set by the operator, and remote
  editorial planning or review also needs `editorial_consent` or
  `model_review_consent` on the run request.
- Model editorial planning must be enabled with
  `CLIPFORGE_LLM_EDITORIAL_ENABLED=true`.
- Provider credentials are read from the worker environment only and never
  returned by the API.
- Only candidate excerpts and transcript evidence are sent. The source video is
  never uploaded to a model provider.

The director's output is schema-validated. Segments outside the candidate's
source range are rejected. Hook text must be an exact transcript quote; an
ungrounded hook is dropped and recorded as a risk. Malformed output gets one
corrected retry, then the deterministic fallback.

## Reuse and invalidation

- Transcripts remain keyed by source content hash, model, compute type, language
  and VAD.
- A completed analysis is reused while its transcript and candidates exist.
- Caption, frame, filename and packaging changes do not invalidate analysis.
- A changed source is a different project source row; its content hash changes,
  so transcript and director caches no longer match.
- Director cache keys include the candidate evidence, provider, model and
  director version.
- Render reuse is keyed by the canonical spec fingerprint and source identity.

## Known limits

- One worker and one SQLite writer. Runs execute sequentially in the existing
  queue.
- The director uses text and measured composition metadata. It does not watch
  frames or listen to audio; the deterministic gate does measure the encoded
  file.
- No speaker diarisation, so speaker-aware direction remains unavailable.
- Automatic repairs are deliberately narrow. Unreadable slides and uncertain
  webcam regions require a person.
- The pipeline still depends on CPU transcription and rendering; the documented
  enhancement limits apply.
