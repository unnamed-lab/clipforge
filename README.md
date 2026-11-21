# Clipforge

**A local-first video repurposing studio.** Import a long recording, transcribe it on your own machine, find the moments worth keeping, tighten the edges, and export captioned clips with title, caption, hashtag and keyword suggestions.

Clipforge runs as two local processes: a Next.js web app and a Python worker that owns FFmpeg, faster-whisper, SQLite and every media file. No paid API is required for the core workflow, and with the default storage backend the source video never leaves the machine.

> **Status:** v0.1.0, early development. Single-user and local-first by design. It is not a multi-tenant SaaS, and several advanced endpoints have no dedicated UI yet. See [Known limitations](docs/KNOWN_LIMITATIONS.md) before relying on it for production work.

---

## What it does

1. **Import** a local file or a URL (`yt-dlp`) after confirming you have the rights to reuse it.
2. **Transcribe** speech with faster-whisper on CPU, including word timestamps, language detection and optional VAD. Transcripts are cached by content fingerprint.
3. **Find moments** with boundary-aware candidate generation, deterministic explainable scoring and near-duplicate removal. Optional LLM rescoring is off by default and requires explicit consent.
4. **Plan the edit** with a duration target, hook, narrative strategy, pacing profile and a source-to-timeline map. Reordered plans are not applied silently.
5. **Review and trim** against transcript snap points, check a proposed ending, keep or reject candidates, and see exactly why each score was given.
6. **Package** the clip with grounded titles, descriptions, hashtags and keywords, per-field regeneration, a title rubric and an optional trend check.
7. **Export** 9:16, 16:9 or 1:1 video at 720p–4K with scene-aware composition, burned-in or sidecar captions, compression presets, audio normalisation, optional local video enhancement (Standard/Clean/AI Enhance/AI Enhance+), before/after frame comparison and a measured export quality gate.

The score in the editor is an **editorial estimate from transcript features, not a prediction of reach or engagement**.

---

## Feature tour

| Area | What is implemented |
| --- | --- |
| Import | Local upload with FFprobe validation; URL import through `yt-dlp` with SSRF, size and duration guards; URL inspection before download; quality and processing modes; source replacement. |
| Transcription | faster-whisper CPU int8, `tiny`/`base`/`small`/`medium`; word timestamps; VAD; language hint; transcript reuse by content hash, model, compute type, language and VAD. |
| Candidate discovery | Sentence and word models from timings; boundary-aware windows across duration bands; weighted transcript features (hook, quotability, completeness, coherence, density, emotion, novelty, context, boundary, risk); goal-specific weights; explainable reasons and warnings; dedupe. |
| Editorial planning | Short/Standard/Extended/Custom duration targets; hook selection and review; Automatic, Cold open, In medias res, Result first, Chronological, Question-answer, Story escalation, Reaction-led and Manual strategies; pacing profiles; retention plan; timeline with warnings. |
| Editing | Video preview bounded to the clip; trim scrubber; in/out nudge and transcript snapping; save/reset trims; ending review with alternatives; keep/reject/restore candidates; title and decision edits. The selected hook and editorial reasoning are shown but not edited in the workspace. |
| Captions | Burn-in and/or SRT sidecar; four built-in presets (`clean`, `large`, `minimal`, `branded`) plus an API `custom` preset; bundled fonts; position, alignment, margin, background and word highlight; measured caption bounds reported by the quality gate. |
| Export | 9:16 / 16:9 / 1:1; preserve-source, 720p, 1080p, 1440p, 4K or custom even dimensions; Auto, Fill/crop, Contain, Picture-in-picture or Manual composition; per-scene composition decisions with reasons and warnings; hook and editorial application; Standard/Clean/AI Enhance/AI Enhance+ video quality profiles with a same-frame before/after comparison; compression presets or custom CRF/bitrate/width/fps; optional logo overlay; persistent render history. |
| Packaging | Titles across eight angles; title rubric and rejection of unsupported claims; description, hashtags, keywords, thumbnail text; field-level regeneration that preserves approved fields; optional YouTube/Twitch/trend providers, all reported honestly when unavailable. |
| Quality gate | FFprobe/FFmpeg evidence for streams, dimensions, source resolution, frame rate, duration and duration target, full decode, audio track, A/V sync, black frames, freezes, caption bounds, composition warnings, logo overlays and ending review. A blocking failure keeps the draft for inspection and does not publish it as a finished render. |
| Operations | Persistent SQLite job queue with stages, progress, cancellation, retry and dedupe; local or Supabase Storage; disk-space guards; health and capability reporting; Docker Compose and Render Blueprint. |

Some capabilities exist only in the worker API, not the workspace UI: reusable brand/project profiles, uploaded logo and music assets, custom caption presets, content library and rights tracking, evaluation datasets, and explicit multi-segment render specs. The [user guide](docs/USER_GUIDE.md) marks the boundary.

---

## Architecture

```
Browser
  │  same-origin requests only
  ▼
Next.js web app  (apps/web, :3000)
  │  server-side BFF proxy; injects the worker token
  ▼
FastAPI worker   (apps/worker, 127.0.0.1:8788)
  ├── persistent single-consumer job queue (SQLite)
  ├── FFprobe / FFmpeg
  ├── faster-whisper (CPU)
  ├── ranking, editorial planning, packaging
  └── local storage or Supabase Storage
        │
        └── data/app.db, data/projects/<id>/...
```

- The browser never talks to the worker directly and never sees the token.
- The worker is the only process that touches media, SQLite, FFmpeg or model files.
- HTTP requests enqueue work and return; the UI polls job state. No render or transcription runs inside a browser-facing request.
- The worker is a single-writer service. Do not run multiple worker instances against one data directory.

More detail: [Architecture](docs/ARCHITECTURE.md) and [Processing pipeline](docs/PROCESSING_PIPELINE.md).

---

## Quick start

### Prerequisites

| Tool | Requirement | Notes |
| --- | --- | --- |
| Node.js | `>=20` (22.14.0 used here) | Use an LTS release. |
| pnpm | 12.11.2 | Pinned by `packageManager`; enable with `corepack enable`. |
| Python | `>=3.11,<3.14` (3.13 used here) | Python 3.14 has no `ctranslate2` wheel for this stack. |
| FFmpeg | 6.x or newer | Must include `libx264`, AAC, `libass`, `subtitles` and `showwavespic`. |
| FFprobe | Same package as FFmpeg | Used for every media inspection. |

Verify FFmpeg before debugging anything else:

```bash
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters
```

### Install

```bash
corepack enable
pnpm install
```

Windows (PowerShell):

```powershell
cd apps/worker
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
cd ../..
```

macOS / Linux:

```bash
cd apps/worker
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cd ../..
```

The worker resolves its interpreter from `CLIPFORGE_PYTHON`, then `apps/worker/.venv`, then `PATH`. Check it with:

```bash
node scripts/worker.mjs python
```

### Configure

```bash
cp .env.example .env
```

PowerShell: `Copy-Item .env.example .env`

For host development leave `CLIPFORGE_WORKER_TOKEN` empty. The worker generates `data/worker-token` on first boot and the web app reads that file. Do not commit `.env`, `data/`, or generated media.

### Run

Use two terminals:

```bash
# Terminal 1 - worker on http://127.0.0.1:8788
pnpm worker:dev

# Terminal 2 - web on http://localhost:3000
pnpm dev
```

Or start both with one command:

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
```

```bash
# macOS / Linux
bash scripts/dev.sh
```

Open <http://localhost:3000>. The quickest end-to-end check is the proxied health route:

```bash
curl http://localhost:3000/api/health
```

If that succeeds, the web app, the proxy, the token and the worker are all working.

The first analysis downloads the configured Whisper model (the host default is `small`; the Docker default is `base`). Expect the first job to be slower than later ones.

---

## Docker

The Compose stack mirrors the hosted topology: a stateless web container and a single worker container with a durable volume.

```bash
cp .env.example .env
node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"
```

Put the generated value in `.env`:

```dotenv
CLIPFORGE_WORKER_TOKEN=<generated-value>
```

Then:

```bash
docker compose up --build
```

Open <http://localhost:3000>. Compose waits for the worker health check before starting the web app. The named volume `clipforge-data` holds SQLite, the model cache, scratch files and (with local storage) all media. Keep exactly one worker replica.

Optional Supabase Storage and port overrides are covered in [Docker deployment](docs/deployment/DOCKER.md).

---

## Deployment

`render.yaml` describes a two-service Render Blueprint: a public, stateless Next.js web service and a private, single-instance worker with a 20 GB persistent disk. The Blueprint cannot be applied without a Render account and a GitHub remote.

See [Render deployment](docs/deployment/RENDER.md), [Storage and workers](docs/deployment/STORAGE_AND_WORKERS.md) and [Environment variables](docs/deployment/ENVIRONMENT_VARIABLES.md).

---

## Commands

| Command | Purpose |
| --- | --- |
| `pnpm dev` | Next.js dev server with Turbopack on port 3000. |
| `pnpm worker:dev` | FastAPI worker with reload on `127.0.0.1:8788`. |
| `scripts/dev.ps1` / `scripts/dev.sh` | Start both processes and stop both on Ctrl+C. |
| `pnpm build` | Production Next.js build. |
| `pnpm start` | Serve the production build. |
| `pnpm typecheck` | Web TypeScript check. |
| `pnpm lint` | Web ESLint. |
| `pnpm test` | Web unit tests (Vitest). |
| `pnpm test:worker` | Worker pytest suite (add `-- -m "not slow"` for the fast set). |
| `pnpm benchmark:render` | Repeatable render benchmark suite; see [Render performance](docs/PERFORMANCE.md). |
| `pnpm lint:worker` | Worker Ruff check. |
| `node scripts/verify.mjs` | Typecheck, both linters, shared/web tests and fast worker tests, in fail-fast order. |
| `node scripts/verify.mjs --build` | Adds the production Next.js build. |
| `node scripts/verify.mjs --docker` | Adds the Compose build and smoke test. |
| `node scripts/verify.mjs --slow` | Includes worker tests marked `slow` (real inference). |
| `pnpm docker:build` / `up` / `down` | Compose lifecycle. |

`package.json` also defines `test:e2e`, and `playwright.config.ts` exists, but `@playwright/test` is not a declared dependency and CI does not run a browser gate. Treat UI changes as unverified until exercised manually; see [Known limitations](docs/KNOWN_LIMITATIONS.md#verification).

---

## Configuration

Configuration is read from `.env` by both processes. The full table lives in [Environment variables](docs/deployment/ENVIRONMENT_VARIABLES.md); these are the settings that change behaviour most:

| Variable | Default | Effect |
| --- | --- | --- |
| `CLIPFORGE_WORKER_URL` | `http://127.0.0.1:8788` | Where the web app finds the worker. |
| `CLIPFORGE_WORKER_TOKEN` | generated into `data/worker-token` | Shared secret for `/api/*` (except `/api/health`). Required by Compose and hosted deployments. |
| `CLIPFORGE_DATA_DIR` | `./data` | SQLite, media, model cache and scratch space. |
| `CLIPFORGE_MAX_UPLOAD_MB` | `2048` | Upload limit. |
| `CLIPFORGE_MAX_DURATION_MINUTES` | `360` | Source duration limit. |
| `CLIPFORGE_DISK_RESERVE_MB` | `512` | Free-space floor before imports, model downloads and renders. |
| `CLIPFORGE_WHISPER_MODEL` | `small` (host), `base` (Compose) | `tiny`, `base`, `small` or `medium`. |
| `CLIPFORGE_WHISPER_DEVICE` | `cpu` | GPU transcription is not supported in this release. |
| `CLIPFORGE_LLM_ENABLED` | `false` | Enables optional OpenAI-compatible candidate rescoring. Requires per-run consent. |
| `CLIPFORGE_STORAGE_BACKEND` | `local` | `local` or `supabase`. SQLite and scratch files stay local either way. |

Secrets belong only in the worker environment. Never put the Supabase service-role key or an LLM key in a `NEXT_PUBLIC_*` variable.

---

## Data and privacy

- With `local` storage, source media, proxies, audio, renders and subtitles live under `data/`.
- SQLite (`data/app.db`) is the only database. There is no Redis or Postgres.
- URL import contacts the source site through `yt-dlp`; inspect first if you want to see the renditions and approximate size.
- Transcription is local. Transcript text leaves the machine only through an optional LLM feature: candidate rescoring requires a configured provider plus per-run consent; packaging sends the brief and transcript to the configured provider when **Use the configured LLM** is enabled. Turn that toggle off for fully local packaging.
- Packaging research providers are optional, disabled by default, and report as unavailable when unconfigured. No engagement metric is invented.
- Supabase Storage is an explicit opt-in. Selecting it uploads durable media objects to your Supabase project.

---

## Repository layout

```
apps/
  web/                 Next.js 15 App Router UI + BFF proxy
  worker/              FastAPI worker, SQLite, queue, FFmpeg, faster-whisper
packages/
  shared/              Zod contracts, presets and cross-runtime fixtures
docs/
  deployment/          Local, Docker, Render, environment, storage
  quality/             Dated implementation and failure audits
scripts/
  dev.ps1, dev.sh      Run web + worker together
  worker.mjs           Cross-platform Python launcher
  verify.mjs           Automated verification gate
  docker-verify.mjs    Compose build and smoke test
data/                  Runtime data (git-ignored)
```

---

## Documentation

| Document | Contents |
| --- | --- |
| [Documentation index](docs/README.md) | What each document covers and which ones are dated records. |
| [User guide](docs/USER_GUIDE.md) | End-to-end workflow, editor controls and export behaviour. |
| [Video enhancement](docs/ENHANCEMENT.md) | Root-cause audit, model/licence evaluation, pipeline order, measured CPU throughput and remaining limits. |
| [Render performance](docs/PERFORMANCE.md) | Reproducible benchmarks, per-stage timings, the 5 ms/frame verdict and the performance regression gate. |
| [API reference](docs/API.md) | Worker routes, authentication, job model and examples. |
| [Known limitations](docs/KNOWN_LIMITATIONS.md) | What is deliberately absent, and what "verified" means here. |
| [Architecture](docs/ARCHITECTURE.md) | Service boundaries, data model, job model and security rules. |
| [Processing pipeline](docs/PROCESSING_PIPELINE.md) | Import, probe, transcription, segmentation, ranking, rendering and cleanup. |
| [Local development](docs/deployment/LOCAL_DEVELOPMENT.md) | Prerequisites, install, run, checks and troubleshooting. |
| [Docker](docs/deployment/DOCKER.md) | Compose topology and volume behaviour. |
| [Render](docs/deployment/RENDER.md) | Blueprint deployment and its limits. |
| [Environment variables](docs/deployment/ENVIRONMENT_VARIABLES.md) | Authoritative configuration table. |
| [Storage and workers](docs/deployment/STORAGE_AND_WORKERS.md) | Backend invariants and the scaling path. |
| [Quality audits](docs/quality/) | Dated records of editorial, packaging and export work. |

---

## Known limitations

- CPU-only transcription; a one-hour recording can take tens of minutes depending on model and hardware.
- One worker process and one SQLite writer. No horizontal scaling.
- No speaker diarisation, face tracking or speaker-aware reframing.
- Jobs interrupted by a worker restart are marked failed; there is no automatic resume.
- The workspace edits one clip at a time. Multi-segment render specs are supported by the API, not authored in the UI.
- `split` composition is not available for a single source and degrades to contain with a warning.
- Packaging metadata is a draft for a human to publish; there is no publishing integration.
- Browser automation is not part of the verification gate.

The full list, with workarounds: [Known limitations](docs/KNOWN_LIMITATIONS.md).

---

## Contributing

Development conventions, verification steps and the documentation contract are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

No licence file is present in this repository yet. Treat the code as all rights reserved until a licence is added; do not redistribute it without the owner's permission.
