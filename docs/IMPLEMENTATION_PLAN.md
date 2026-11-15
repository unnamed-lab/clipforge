# Clipforge — Implementation Plan

Status: **Sprint 1 complete (Milestone 1–3 core slice)**. Last updated: 2026-11-15.

## 1. Environment audit (recorded before any code was written)

| Item | Finding |
| --- | --- |
| Target directory | `C:\Users\user\Documents\Programming\Workspace\clipforge` — **empty** |
| Git | Directory is *not* its own repository. It is an untracked folder inside `Workspace`, whose `origin` is an unrelated project (`slipdeck-backend`). A dedicated repository was initialised inside `clipforge` so the parent repository is never touched. |
| Existing code | None. No framework, routes, components, database or job system to preserve. |
| Node | v22.14.0 |
| Package managers | pnpm 12.11.2, npm 11.3.0 |
| Python | 3.13.3 (`py -3.13`) and 3.14.5 (`py`). 3.14 is rejected for the worker: `ctranslate2` has no cp314 wheels. Worker venv pinned to 3.13.3. |
| FFmpeg | N-128433 (2026-11-05, gcc 16.2). `libx264`, native `aac`, `libmp3lame`, `libass`, `fontconfig`, `freetype`, `harfbuzz` all present. `subtitles`, `ass`, `crop`, `scale`, `overlay`, `boxblur`, `gblur`, `showwavespic` filters available. No `libfdk_aac` (not needed). |
| CPU | Intel i7-6820HQ — 4 cores / 8 threads (Skylake, 2015) |
| RAM | 15.9 GB |
| GPU | NVIDIA Quadro M1000M, 2 GB, Maxwell compute capability 5.0. **CUDA is not usable for ASR**: `ctranslate2` requires compute capability ≥ 7.0. Falls back to CPU int8. Intel HD 530 present but not used for inference. |
| Disk | **4.9 GB free on `C:`** — the binding constraint. Models and media must be budgeted; the worker checks free space before model download and before render. |
| Network | PyPI and Hugging Face reachable (model download works). |
| OS | Windows, PowerShell 7 |

### Consequences for the design

1. **CPU-only transcription.** Default `small` int8 (~480 MB) with `base` offered as the fast option. A 60-minute podcast takes roughly 25–60 minutes here. Documented, not hidden.
2. **No diarisation in the MVP.** `pyannote.audio` needs a GPU and accepts gated model access; installing it on this machine would be a fake capability. The provider interface exists, the implementation does not.
3. **No local LLM shipped.** A 4-core Skylake cannot run a useful scoring model at acceptable latency, and the disk cannot hold one. The default scorer is deterministic and explainable; an LLM adapter (local *or* hosted, OpenAI-compatible) is opt-in and never required.
4. **Disk-aware by default.** Storage budget checks, generated-media cleanup on delete, and no waveform/AI-frame caching beyond what is cheap.
5. **Sequential job execution.** One job at a time. The CPU has 4 cores; parallel transcription would only slow everything down.

## 2. Verified tool availability

| Capability | Status | Used for |
| --- | --- | --- |
| FFprobe JSON inspection | available | media metadata |
| FFmpeg → 16 kHz mono PCM | available | ASR input |
| FFmpeg HW→SW crop/scale/overlay/blur | available | framing presets |
| FFmpeg `subtitles`/`ass` burn-in | available | captions |
| FFmpeg `showwavespic` | available | editor waveform |
| faster-whisper (CPU int8) | installs, verified E2E | transcription |
| yt-dlp | installs, optional | URL import |
| Windows SAPI voices | available | synthetic speech test fixture |

## 3. Architecture decision

Monorepo, two runtimes, one local host:

- `apps/web` — Next.js 15 (App Router), React 19, TypeScript, Tailwind v4. Owns UI and a same-origin BFF proxy. Never runs media work.
- `apps/worker` — FastAPI + SQLite + a persistent single-consumer job queue. Owns media, transcription, ranking, rendering and all file access.
- `packages/shared` — the wire contract (Zod schemas + generated JSON fixtures) shared by both runtimes.

Rejected alternatives and why:

| Alternative | Why not |
| --- | --- |
| Next.js route handlers spawn FFmpeg themselves | Long jobs would be tied to a serverless-ish request lifecycle, no cancellation, no persistence across requests. |
| Redis / Celery | Extra daemon and another failure mode for a single-user local app. SQLite-backed queue is enough and survives restart. |
| Node-only pipeline (`whisper.cpp` bindings) | Weaker maintained bindings on Windows; faster-whisper + FFmpeg is the most reliable free stack on this machine. |
| Electron/Tauri desktop shell | The browser already provides playback, capture and download. A local web app ships sooner and stays debuggable. |

## 4. Milestones and checklist

Legend: `[x]` implemented and verified · `[~]` implemented, not verified on this hardware · `[ ]` not started.

### Milestone 1 — Working foundation
- [x] Environment audit documented
- [x] Monorepo scaffold, shared contract package
- [x] Worker health + capabilities endpoints (real capability probing, not hard-coded)
- [x] SQLite schema + repositories, idempotent migrations
- [x] Persistent job queue with stage progress, cancellation, retry, dedupe
- [x] Project create / list / get / delete
- [x] Local upload import with content validation (FFprobe, not extension trust)
- [x] URL import via yt-dlp with SSRF + size + duration guards
- [x] Media inspection surfaced in the UI

### Milestone 2 — End-to-end clip discovery
- [x] Audio extraction (16 kHz mono PCM)
- [x] faster-whisper transcription, word timestamps, language detection, VAD, transcript reuse
- [x] Sentence segmentation from word timings
- [x] Boundary-aware candidate generation across three duration bands
- [x] Deterministic baseline ranking with 10 explainable components
- [x] Duplicate/near-duplicate suppression
- [x] Optional OpenAI-compatible LLM rescoring (opt-in, shortlist only, schema-validated, falls back)
- [x] Candidate list + score breakdown + warnings in the UI

### Milestone 3 — Real clip rendering
- [x] FFmpeg render of a single clip (H.264 + AAC), safe argv, cancellation, timeouts
- [x] Post-render validation (file exists, streams present, duration sane)
- [x] Controlled download endpoint with path jail
- [x] Render history persisted and reloadable

### Milestone 4 — Editing experience
- [x] Transcript panel with clickable words → seek
- [x] Boundary editor (start/end), duration guard, segment preview loop
- [x] Caption presets (4) + styling controls, burned via ASS with active-word highlight
- [x] Aspect presets 9:16 / 16:9 / 1:1, centre crop and blurred-background fallbacks
- [x] Waveform track for trimming
- [ ] Face detection / speaker-aware reframing — *planned, deliberately absent*
- [ ] Inline caption text editing before burn — *planned*

### Milestone 5 — Longer edits
- [ ] Multi-segment compilation, reordering, transitions — *planned; manifest-shaped render spec already models segments as an array*
- [x] Render spec is a versioned, validated document (not a bag of flags)

### Milestone 6 — Reliability and distribution
- [x] Failure recovery: source preserved on failure, retry with different model
- [x] Disk-space guards, temp cleanup, orphan-file sweep on project delete
- [x] Windows + Unix setup docs, capability diagnostics
- [ ] Cross-platform CI matrix — *planned*

### Milestone 7 — Hosted mode
- [ ] Not started. Out of scope by design; see `KNOWN_LIMITATIONS.md`.

## 5. Definition of done — verification record

See `docs/KNOWN_LIMITATIONS.md` §"Verification record" for the exact commands run and their output. Summary:

| # | Requirement | Evidence |
| --- | --- | --- |
| 1 | App runs from documented instructions | `scripts/dev.ps1`, README quick start |
| 2 | Local video import | integration test + manual HTTP walkthrough |
| 3 | Real transcript from faster-whisper | `tests/test_pipeline_e2e.py` (real model, synthetic speech) |
| 4 | Ranked candidates with explanation | `tests/test_ranking.py`, UI |
| 5 | Boundary editing persists | `test_candidate_patch_persists` |
| 6 | Valid playable MP4 | `ffprobe` assertion on rendered output |
| 7 | Synchronised captions | burned ASS events from word timings, frame inspected |
| 8 | Project survives restart | `test_project_persists_across_instances` |
| 9 | Recovery without losing source | failed-job retry test, source file checksum stable |
| 10 | No paid API required | baseline scorer is default; LLM disabled |
| 11 | Export file exists and plays | render validation + downloaded byte check |

## 6. First vertical slice (delivered)

`POST /api/projects` → `POST /sources/upload` → `POST /analysis` → poll job → `GET /transcript` →
`GET /candidates` → `PATCH /candidates/{id}` → `POST /renders` → poll job → `GET /renders/{id}/download`.

That path is implemented, exercised by an automated test against real FFmpeg and real faster-whisper, and reachable from the UI.

## 7. Next recommended sprint

1. Multi-segment compilation UI on top of the existing render-spec array.
2. Caption text editing + re-timing before burn.
3. Optional local LLM adapter with a memory/disk feasibility probe (e.g. llama.cpp server) and a "leave device" consent dialog.
4. Resumable chunked uploads for >2 GB sources.
5. Face-tracking reframing as an optional, clearly labelled enhancement.
