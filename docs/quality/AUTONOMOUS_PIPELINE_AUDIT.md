# Autonomous production pipeline - repository audit

Date: 2026-11-21
Scope: the repository as it stands at commit `5351be8` plus the uncommitted
crop-before-scale optimisation in `rendering/ffmpeg.py`.

This is the audit required before adding project orchestration. It records what
the code does today, not what the documentation promises.

## Execution paths that exist today

```
Browser
  -> Next.js BFF (/api/[...path])
  -> FastAPI worker (token guarded)
       analysis   : pipeline/analysis.py      -> faster-whisper -> candidates -> JSON evidence
       discovery  : discovery/service.py      -> more candidates from the stored transcript
       render     : api/routes_renders.py     -> rendering/service.py -> ffmpeg -> quality/gate.py
       packaging  : packaging/*               -> optional LLM + rule-based metadata
       queue      : jobs/queue.py             -> SQLite FIFO -> jobs/handlers.py
```

There is exactly one renderer (`rendering/ffmpeg.py`), one render specification
(`rendering/spec.py`), and one output gate (`quality/gate.py`). Preview,
composition preview, quality preview and export all consume a `RenderSpec`.

## Capability classification

| # | Capability | Status | Evidence and gap |
| --- | --- | --- | --- |
| 1 | Project creation and persistence | Implemented, verified | `api/routes_projects.py`, `ProjectRepository`; id/name/status/goals/settings/profile/overrides/brief persisted. No project-level workflow state. |
| 2 | Source upload/download and media storage | Implemented, verified | `media/importer.py`, `media/proxy.py`, local and Supabase backends, resume for URL imports. `sources.storage_path` is never exposed by `serialise_source`. |
| 3 | Transcription and speaker identification | Partial | faster-whisper with word timings, caching by content fingerprint, corrections. Diarisation is a stub that reports unavailable; `speaker` is only copied from whatever the transcript provider returns. |
| 4 | Scene analysis and source-time mapping | Implemented, verified | `media/visual.py`, `rendering/composition.py`, `quality/boundaries.py`. Scene evidence is derived from a sampled proxy and says so. |
| 5 | Moment discovery and regeneration | Implemented, verified | Boundary-aware generation, explainable scoring, dedupe, targeted additional discovery. |
| 6 | Hook and narrative strategy selection | Implemented, verified | `hooks/select.py`, `editorial/strategy.py`, `editorial/narrative.py`, persisted as `hook_json` and `editorial_json`. Deterministic only; no whole-project editorial decision. |
| 7 | Client Profiles and project overrides | Implemented, partially verified | `profiles/spec.py`, `resolve_settings`, profile snapshot on renders. Precedence rules exist and are tested in `test_render.py`; there is no end-to-end profile API test. |
| 8 | Webcam-region detection and tracking | Implemented, verified | `media/facecam.py`, `media/facecam_detect.py`, `test_facecam_tracking.py`. Manual overrides and locks are modelled. |
| 9 | Composition and crop calculation | Implemented, verified | `rendering/composition.py`, `test_composition.py` including the uncommitted crop-before-scale change. |
| 10 | Caption generation and positioning | Implemented, verified | `rendering/subtitles.py`, caption bounds report consumed by the gate. |
| 11 | Enhancement and rendering | Implemented, verified | `enhancement/*`, `rendering/service.py`, `test_enhancement*.py`. Measured limits documented in `docs/ENHANCEMENT.md`. |
| 12 | Preview/export parity | Implemented, verified | `build_preview_command` and `build_command` share `_framing_chain`; parity asserted in `test_composition.py` and `test_enhancement.py`. |
| 13 | QA and review functionality | **Partial** | `quality/gate.py` validates a finished render and stores a report in `renders.review_json`. Findings are display checks, not stable finding IDs; there is no per-clip review status and no repair loop. |
| 14 | Job orchestration, retries, cancellation | **Partial** | Durable SQLite FIFO, progress, cooperative cancel, FFmpeg termination, transient retry parking, interrupted-download resume. No stage checkpoint for analysis/render, no heartbeat, no stall detection, no hard deadline, no per-stage resume. |
| 15 | Export storage and duplicate handling | **Partial** | `renders` rows are stable asset ids and old renders are never deleted. Files are named `<render_id>.mp4`; there is no human filename, version number, checksum, current/superseded state, or duplicate-detection by edit decisions. |
| 16 | Tests and deployment constraints | Implemented, partially verified | 500+ worker tests incl. real FFmpeg and ESPCN, web unit tests, CI. Playwright is configured but not a dependency and is not in the gate. One worker, one SQLite writer, single consumer queue. |

### Missing outright

- Project-level production run: there is no durable record of "analyse → plan →
  configure → render → review → repair → package" for a project.
- A structured editorial director that returns a validated plan for several
  clips at once.
- Local model provider support (Ollama) and a provider/model registry with
  health, capabilities and privacy classification.
- Review report versions, finding IDs, repair attempts and needs-human-review
  state.
- Project manifest and versioned, human-named exports.

## Confirmed root causes

1. **The workflow is user-driven per candidate.** `create_render` builds one
   spec for one candidate and queues one render job. Nothing persists which
   stage of a project has completed, so nothing can resume or be audited.
2. **The LLM integration is a scorer, not a director.** `providers/llm/base.py`
   exposes `score()` only; `openai_compat.py` is the only adapter. There is no
   capability registry, health probe, structured plan schema or deterministic
   fallback plan.
3. **Validation is one pass with no second chance.** A gate failure preserves a
   draft and fails the job. Nothing maps a failure to a supported, bounded
   corrective edit.
4. **Render identity is an internal id.** `renders.id` is stable, but the file
   name, version, checksum and current/superseded relationship are absent, so
   duplicate jobs and repeated edits cannot be distinguished by a user.
5. **Configuration logic lives in the route module.** `_layout_payload`,
   `_caption_overrides`, `_audio_payload` and `_logo_payload` are private to
   `routes_renders.py`. Any orchestration must reuse them rather than restate
   the precedence rules, otherwise preview and export can drift.

## Reuse decisions taken for the implementation

- Analysis and targeted discovery are reused as-is; the pipeline calls
  `run_analysis` / `run_discovery` and skips them when a valid transcript and
  candidate run already exist.
- The renderer, spec model, composition planner, caption builder and export
  gate are reused; no second rendering path is created.
- Layout/caption/audio/logo precedence is extracted from `routes_renders.py`
  into `rendering/intake.py` and both the HTTP route and the orchestrator call
  it.
- `renders` remains the export asset table. It gains version, display name,
  spec hash, checksum, run/clip links and superseded state instead of a new
  parallel exports table.
- New tables are limited to project runs, stage checkpoints, per-clip
  production state and the editorial director cache. No new queue, broker or
  database is introduced.

## Known constraints carried forward

- One worker process and one SQLite writer; pipeline stages run inside the
  existing single-consumer queue.
- No paid service is required. Remote LLM use stays opt-in and is disabled
  unless the operator configures it.
- CPU-only transcription and enhancement remain slow; the pipeline reports the
  same measured warnings rather than inventing certainty.
- Browser verification is manual; this change adds API and unit/integration
  tests but does not add Playwright to CI.
