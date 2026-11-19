# Deployment Audit — Clipforge

Date: 2026-11-15
Scope: repository at `clipforge/` (branch `main`, commit `dc3245a` plus uncommitted extension work)

This audit records what existed, what blocked a reliable Docker/Render deployment, and the staged plan taken. It does not claim any deployment has run. Sections marked **Verified** were checked in this workspace on the date above; everything else is a design recommendation.

**Implementation status:** the P0/P1 findings were addressed in the working tree after this audit. The object-storage boundary in P2 is now implemented as a local/Supabase Storage abstraction; the remaining single-writer constraints are documented rather than pretended away. The original findings are kept below so the reasoning stays reviewable.

---

## 1. Inventory

### Frontend — `apps/web`

| Item | Finding |
| --- | --- |
| Framework | Next.js `15.5.29` (App Router), React `19.3.1` |
| Language | TypeScript `5.7`, `strict`, `noUncheckedIndexedAccess` |
| Styling | Tailwind CSS v4 via `@tailwindcss/postcss` |
| Workspace package | `@clipforge/shared` (`workspace:*`, transpiled by Next) |
| Dev script | `next dev --port 3000` (no Turbopack flag) |
| Build/start | `next build`, `next start --port 3000` |
| Output mode | default (not `standalone`) |
| Tests | `vitest run` configured, **no test files exist** |
| Public assets | no `apps/web/public` directory |
| Docker/Render files | none |

### Worker — `apps/worker`

| Item | Finding |
| --- | --- |
| Framework | FastAPI `0.163`, Starlette `1.10`, uvicorn `0.54` |
| Python | `>=3.11,<3.14`; project venv is `3.13.3` (3.14 has no ctranslate2 wheel) |
| Transcription | `faster-whisper` `1.2.1`, CPU `int8`, model downloaded at runtime |
| Media | FFmpeg + FFprobe from `PATH`; `libx264`, AAC, libass required |
| Database | SQLite at `<data>/app.db`; schema applied on startup + column migrations |
| Queue | in-process single consumer thread, SQLite-backed, poll loop |
| API + worker | **one process** (`python -m clipforge`); no separate worker entry point |
| Dependency lock | none (ranges in `pyproject.toml`) |
| Lint | none configured |
| Tests | pytest, 94 tests, fast suite green on 2026-11-15 |
| Docker/Render files | none |

### Repository tooling

| Item | Finding |
| --- | --- |
| Package manager | pnpm `12.11.2` (`packageManager` field), lockfile present |
| Workspaces | `apps/web`, `packages/*` only; worker is isolated Python |
| Node requirement | `>=20`; workspace machine runs `22.14.0` |
| Build approvals | `pnpm-workspace.yaml#allowBuilds` (required by pnpm 12) |
| CI | none |
| Compose / Render | none; `infra/` exists but is empty |
| Env example | root `.env.example`, local-only, missing deployment variables |

### Runtime data layout

Everything durable is under one directory (`CLIPFORGE_DATA_DIR`, default `<repo>/data`):

```
data/
  app.db            SQLite database (projects, jobs, renders, rights, datasets)
  worker-token      shared secret generated on first boot
  projects/<id>/    source, audio, transcript, renders, tmp
  assets/           client-profile logos and music
  models/           faster-whisper model cache
  logs/             rotating logs
  tmp/
```

The web app reads `data/worker-token` when `CLIPFORGE_WORKER_TOKEN` is absent. It otherwise never touches media: every browser request is proxied to the worker over HTTP.

---

## 2. Deployment blockers and risks

### P0 — functional, must fix before any deployment

1. **Proxy drops the `/api` prefix.** `apps/web/src/app/api/[...path]/route.ts` builds `/${path.join("/")}`, so `/api/health` is forwarded to `/health`, which the worker does not serve. Every browser call through the proxy returns 404. This was not caught because there are no browser tests and the UI has never been verified against a running pair.
2. **Worker binds `127.0.0.1` by default.** Containers and Render require `0.0.0.0`.
3. **Worker ignores Render's `PORT`.** Render injects `PORT` for web and private services; the worker only reads `CLIPFORGE_WORKER_PORT`.
4. **Web image has no route to the token.** In a container there is no `../../data/worker-token`; the token must come from `CLIPFORGE_WORKER_TOKEN`.
5. **Next.js has no standalone output.** A slim production image cannot run `next start` without copying the full `node_modules` graph.

### P1 — deployment correctness

6. **CORS is hard-coded** to `http://localhost:3000` and `http://127.0.0.1:3000`. Harmless behind the server proxy, but wrong for any direct browser access and impossible to configure.
7. **Single-writer storage.** SQLite, the job queue, model cache and media all live on one filesystem. A Render disk can attach to only one service. Therefore: exactly **one worker instance**, the disk belongs to the worker, and the web tier must stay stateless. Do not scale the worker horizontally and do not add a separate background-worker service that shares the same SQLite file.
8. **Interrupted jobs are marked failed, not resumed.** On startup `mark_orphans()` fails jobs left `running`. Recovery is the existing retry endpoint; there is no automatic resume. This is safe (no half-written render is published) but must be documented.
9. **Graceful shutdown is time-boxed.** `queue.stop(timeout=15)` cancels the current job and waits 15 s. FFmpeg is terminated cooperatively; a slow probe could exceed the window and Render will SIGKILL after its grace period.
10. **Upload size vs. hosting limits.** Default max upload is 2 GB. The web proxy streams the body (no buffering), but Render request limits, client timeouts and disk size must be sized deliberately.
11. **First transcription downloads a model** (~140–480 MB depending on model) into `data/models`. Baking it into the image is explicitly avoided; the disk must have room and the first run is slower.
12. **No dependency lock for Python.** Image builds are not reproducible.
13. **No Python lint gate.** Syntax/undefined-name errors reach tests only.
14. **`verify.mjs` runs package test scripts that have no tests**, so the documented verification gate cannot pass cleanly.
15. **No Docker/Compose/Render validation** and no CI workflow.

### P2 — storage and scale (documented limitation, not silently ignored)

16. ~~**No object-storage adapter.**~~ **Addressed:** `clipforge/storage` now exposes local and Supabase Storage backends. Media keys live in `relative_path`; `storage_path` is a cache hint. SQLite, the queue and model cache still require the worker disk.
17. **No direct-to-object-storage uploads (unchanged).** Browser uploads stream through Next.js to the worker, which validates and probes them before storing. This is acceptable for a single private deployment, but doubles egress through the web tier.
18. **Database on the worker disk (unchanged).** Safe for one instance; a future split needs Postgres plus a shared object store and a real queue.

---

## 3. Recommended target architecture

```
Browser
  │  HTTPS
  ▼
Render Web Service (Next.js standalone, stateless, 1+ instances)
  │  private HTTP + shared token (internal URL)
  ▼
Render Private Service (FastAPI API + job queue + FFmpeg, exactly 1 instance)
  │
  ├── Persistent disk /data  (SQLite + model cache + scratch + optional media cache)
  └── Supabase Storage       (optional durable media: source, renders, captions, assets)
```

- No Postgres or Redis is added: the application does not use them, and adding unused infrastructure would be fiction.
- Next.js handles all public traffic and injects the worker token server-side.
- The worker is never publicly exposed.
- The worker's health endpoint (`/api/health`) is public inside the app and requires no token.
- Docker Compose mirrors this topology locally; normal frontend development still runs Turbopack on the host.

---

## 4. Planned file changes

### Runtime and configuration

| File | Change |
| --- | --- |
| `apps/web/package.json` | `dev` uses `next dev --turbopack`; add focused test script if needed |
| `apps/web/next.config.ts` | `output: "standalone"`, `outputFileTracingRoot` = repo root; keep webpack production build |
| `apps/web/src/app/api/[...path]/route.ts` | forward `/api/<path>` correctly |
| `apps/web/src/lib/worker.ts` | normalise scheme-less internal URLs; clearer unreachable message |
| `apps/worker/src/clipforge/config.py` | honour `PORT`, `CLIPFORGE_CORS_ORIGINS`, storage/dir overrides |
| `apps/worker/src/clipforge/api/app.py` | configurable CORS |
| `.env.example` | local + Docker + Render placeholders, no secrets |
| `.gitignore` | keep media, models, renders, env files out |

### Containers and hosting

| File | Purpose |
| --- | --- |
| `apps/web/Dockerfile` | multi-stage pnpm build -> standalone runner, non-root |
| `apps/worker/Dockerfile` | pinned Python, FFmpeg/FFprobe + fonts, locked deps, non-root with volume ownership fix |
| `docker/worker-entrypoint.sh` | fix mounted-disk ownership, then drop privileges |
| `.dockerignore` | exclude media, models, venvs, `node_modules`, `.next`, Git, secrets |
| `compose.yaml` | web + worker, health checks, internal network, named data volume |
| `render.yaml` | web + private worker, disk, generated token, health checks |
| `apps/worker/requirements.lock` | exact runtime pins for reproducible image builds |
| `apps/worker/src/clipforge/storage/` | storage contract, local backend, Supabase Storage REST adapter, project cleanup |
| `apps/worker/tests/test_storage.py` | key safety, local round-trip, mocked Supabase upload/download/delete/sign, config failures |

### Validation and documentation

| File | Purpose |
| --- | --- |
| `scripts/verify.mjs` | fail-fast local gate; optional Docker checks |
| `scripts/docker-verify.mjs` | compose config, image builds, health smoke, FFmpeg smoke |
| `apps/worker/pyproject.toml` | ruff dev dependency + config |
| `apps/web/src/lib/proxy-path.ts` + test | pure function for proxy path forwarding |
| `packages/shared/src/time.test.ts` | real coverage for time contracts |
| `docs/deployment/DEPLOYMENT_AUDIT.md` | this file |
| `docs/deployment/LOCAL_DEVELOPMENT.md` | host workflow with Turbopack |
| `docs/deployment/DOCKER.md` | images, Compose, volumes, troubleshooting |
| `docs/deployment/RENDER.md` | Blueprint, secrets, disks, manual steps, costs |
| `docs/deployment/ENVIRONMENT_VARIABLES.md` | every variable, defaults, scope |
| `docs/deployment/STORAGE_AND_WORKERS.md` | single-writer model, durability, recovery, object-storage path |
| `.github/workflows/ci.yml` | typecheck/lint/tests/build; optional Docker build |

---

## 5. Explicit non-goals and honest limitations

- **Storage only, not a database split.** Supabase is used for media bytes, never for SQLite or the queue. The worker still needs a writable disk and remains a single writer.
- **No multi-instance worker.** SQLite + in-process queue + single-writer semantics make that incorrect, even when media is remote.
- **No direct browser-to-storage upload yet.** The worker must validate and probe media, so uploads still traverse the proxy.
- **No GPU assumptions.** CPU `int8` transcription only; FFmpeg uses CPU encoding.
- **No paid API requirement.** Baseline scoring is local; the optional LLM adapter stays off.
- **Render deployment cannot be proven from this machine** without a Render account and repository access. The Blueprint is validated structurally; the actual deploy is a documented manual step.

---

## 6. Validation plan (to be executed)

1. `pnpm typecheck`, `pnpm lint`, `pnpm build`.
2. `pytest` (fast suite) for the worker; concat with ruff.
3. `docker compose config` and `docker build` for both images.
4. Start Compose; assert `/api/health` on both services; assert web `/api/health` proxies to the worker.
5. FFmpeg/FFprobe smoke inside the worker image using a generated 1 s test clip (no model download).
6. Upload a small file through the web proxy into the worker and confirm the stored source and a queued job.
7. Restart the worker mid-queue and confirm an interrupted download is requeued and resumes, while other interrupted jobs surface as failed/retryable.
8. Record actual results in the final report; anything not run is called out explicitly.

---

## 7. Validation results (2026-11-16)

### Passed in this workspace

| Check | Command | Result |
| --- | --- | --- |
| TypeScript | `node scripts/verify.mjs --build` | passed |
| Web lint | same | passed |
| Worker lint | same + `python -m ruff check apps/worker/src apps/worker/tests` | passed |
| Shared tests | same | 9 passed |
| Web tests | same | 4 passed |
| Worker tests | same + `python -m pytest apps/worker/tests -q` | passed (non-slow suite) |
| Production web build | same (`next build`, no standalone tracing on Windows) | passed |
| Web Docker image | `docker build -f apps/web/Dockerfile -t clipforge-web:check .` | passed, standalone image (~408 MB) |
| Compose config / missing secret | `node scripts/docker-verify.mjs` | passed (missing token rejected, valid config accepted) |

### Attempted but not proven here

The container checks exposed real defects, all fixed:

- Corepack's pnpm shim expected `bin/pnpm.cjs`, which pnpm 12 no longer ships.
  The web image now installs the pinned CLI with npm.
- The web build was not requesting standalone output. The builder stage now sets
  `NEXT_OUTPUT=standalone`.
- The worker image's Debian mirror used plain HTTP and timed out. The Dockerfile
  now uses HTTPS with bounded retries.
- A `USER clipforge` line would have prevented the entrypoint from claiming a
  root-owned Render disk. The image now starts as root only in the entrypoint and
  drops to UID 1001 before uvicorn.
- Pip had no retry budget or reusable download cache. It now retries with a
  BuildKit cache mount.
- The worker wheel omitted `clipforge/db/schema.sql`. A source checkout and an
  editable install hid this, but a clean container crashed at startup because
  the file was missing from site-packages. `pyproject.toml` now declares it as
  package data; the built image contains it.

A complete worker image build and Compose smoke test still did not finish on this
machine. Repeated attempts hit PyPI read timeouts, Docker Hub TLS handshakes, and
Docker Desktop BuildKit frontend crashes. The host also fell to roughly 0.25 GB
free before stale verification processes were stopped. These are environment
failures, not passing results. No live Render deploy or live Supabase bucket was
exercised. The `supabase/storage-api:latest` path is covered by mocked REST tests,
not by a running storage container.
