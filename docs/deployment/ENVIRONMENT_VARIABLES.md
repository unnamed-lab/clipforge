# Environment variables

Only `CLIPFORGE_WORKER_TOKEN` is required in a container deployment. Everything
else has a working default, except the Supabase values when you opt into
`CLIPFORGE_STORAGE_BACKEND=supabase`. Values must never be committed; `.env` is
ignored and `.env.example` contains placeholders only.

## Web service (Next.js)

| Variable | Default | Required | Purpose |
| --- | --- | --- | --- |
| `CLIPFORGE_WORKER_URL` | `http://127.0.0.1:8788` | no | Worker base URL. Accepts `http://host:port` or bare `host:port` (Render private services provide the latter). |
| `CLIPFORGE_WORKER_TOKEN` | read from `<data>/worker-token` | **yes in containers** | Injected server-side on every proxied request. Never sent to the browser. |
| `PORT` | `3000` (image) | platform | Port the standalone Next.js server listens on. Render injects it. |
| `HOSTNAME` | `0.0.0.0` (image) | no | Bind address for the standalone server. |
| `NODE_ENV` | `production` (image) | no | Production runtime. |
| `CLIPFORGE_WEB_PORT` | `3000` | no | Compose host port mapping only; not read by Next.js. |

The web app reads worker configuration at runtime. It does not need the data
directory, FFmpeg, Python, or the token file when `CLIPFORGE_WORKER_TOKEN` is set.

## Worker service (FastAPI + queue + FFmpeg)

| Variable | Default | Required | Purpose |
| --- | --- | --- | --- |
| `CLIPFORGE_WORKER_TOKEN` | generated into `<data>/worker-token` | **yes in containers** | Shared secret. Must match the web service. |
| `CLIPFORGE_DATA_DIR` | `<repo>/data` (`/data` in image) | no | Single writable root: database, media, models, logs, temp. |
| `CLIPFORGE_WORKER_HOST` | `127.0.0.1` | container: `0.0.0.0` | Bind address. |
| `CLIPFORGE_WORKER_PORT` | `8788` | no | Local bind port. Overridden by `PORT`. |
| `PORT` | unset | platform | If set, takes precedence over `CLIPFORGE_WORKER_PORT`. Render injects it. |
| `CLIPFORGE_MAX_UPLOAD_MB` | `2048` | no | Maximum accepted source size. |
| `CLIPFORGE_MAX_DURATION_MINUTES` | `360` | no | Maximum accepted source duration. |
| `CLIPFORGE_DISK_RESERVE_MB` | `512` | no | Free-space headroom kept on the data volume. |
| `CLIPFORGE_WHISPER_MODEL` | `small` | no | `tiny`, `base`, `small`, `medium`. `base` is the hosted default. |
| `CLIPFORGE_WHISPER_DEVICE` | `cpu` | no | CPU only; no GPU assumption. |
| `CLIPFORGE_WHISPER_COMPUTE_TYPE` | `int8` | no | CPU quantisation. |
| `CLIPFORGE_ENHANCEMENT_DEVICE` | `auto` | no | ONNX Runtime provider preference for the optional Real-ESRGAN model: `auto`, `cpu`, `cuda` or `dml`. Falls back to CPU. ESPCN runs through OpenCV on CPU. |
| `CLIPFORGE_OPENCV_THREADS` | `min(6, logical CPUs)` | no | OpenCV DNN worker threads for ESPCN. Six measured faster than eight on a 4-core/8-thread CPU. |
| `CLIPFORGE_LLM_ENABLED` | `false` | no | Opt-in external rescoring. Transcript text leaves the device only if enabled and consented. |
| `CLIPFORGE_LLM_BASE_URL` | empty | if LLM enabled | OpenAI-compatible endpoint. |
| `CLIPFORGE_LLM_MODEL` | empty | if LLM enabled | Model name at that endpoint. |
| `CLIPFORGE_LLM_API_KEY` | empty | if LLM enabled | Bearer token. Secret. |
| `CLIPFORGE_LLM_TIMEOUT_SECONDS` | `60` | no | Adapter timeout. |
| `CLIPFORGE_YOUTUBE_API_KEY` | empty | no | Official YouTube Data API discovery. Without it the connector reports unavailable. |
| `CLIPFORGE_TWITCH_CLIENT_ID` | empty | no | Twitch Helix connector. |
| `CLIPFORGE_TWITCH_CLIENT_SECRET` | empty | no | Twitch Helix connector. Secret. |
| `CLIPFORGE_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | no | Comma-separated browser origins for direct access. Irrelevant behind the proxy. |
| `CLIPFORGE_STORAGE_BACKEND` | `local` | no | `local` or `supabase`. Unknown values fail at startup. |
| `CLIPFORGE_SUPABASE_URL` | empty | if supabase | Project URL (`https://<ref>.supabase.co`) or a reachable self-hosted Storage API base. |
| `CLIPFORGE_SUPABASE_SERVICE_KEY` | empty | if supabase | Service-role key. Secret. Worker only; never exposed to the browser. |
| `CLIPFORGE_SUPABASE_BUCKET` | `clipforge` | no | Existing private bucket name. |
| `CLIPFORGE_SUPABASE_STORAGE_PREFIX` | `/storage/v1` | no | Path prefix for the Storage API. Clear it only when pointing directly at `supabase/storage-api:latest`. |
| `CLIPFORGE_SUPABASE_TIMEOUT_SECONDS` | `60` | no | Storage request timeout. |

### Object storage

`local` is the default and needs no extra configuration. `supabase` stores source
videos, renders, sidecar SRT files and brand assets in a private Supabase Storage
bucket; SQLite, transcripts, the model cache and scratch files stay on the worker
disk.

- Use the **service-role** key. The anon key cannot manage private objects and must
  not be used here.
- Set these variables on the worker only. The web service does not need them.
- Create the bucket before starting the worker. The worker validates settings at
  startup and fails clearly if the URL, key or bucket is missing; it does not fall
  back to local storage.
- For a self-hosted Supabase stack in Docker, use an address reachable from the
  worker container (`host.docker.internal` on Windows/macOS, a Compose service name
  on Linux). `localhost` inside the container points at the worker itself.
- Hosted Supabase and Supabase's Kong gateway expose the Storage API under
  `/storage/v1`; keep the default prefix for those. If you point the worker
  straight at the `supabase/storage-api:latest` container (usually port 5000),
  set `CLIPFORGE_SUPABASE_STORAGE_PREFIX=` so requests go to `/object/...`
  instead of `/storage/v1/object/...`. The storage API image still needs its own
  Postgres database, JWT secret and tenant configuration; it is not a standalone
  drop-in container.
- The free Supabase plan has small storage, database and egress quotas and pauses
  inactive projects. Sustained video storage needs a paid plan. See
  `STORAGE_AND_WORKERS.md` for the operational limits.

### Token precedence

1. `CLIPFORGE_WORKER_TOKEN` if non-empty.
2. `<CLIPFORGE_DATA_DIR>/worker-token` if it exists and is non-empty.
3. Otherwise a new random token is generated and written with `0600` where the OS
   supports it.

The web service follows the same order, except it has no data directory in a
container. If both are running from the same checkout locally, step 2 keeps them
in sync. In Compose/Render, set the same generated value on both services.

## Development and build tooling

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLIPFORGE_PYTHON` | venv, then `python`/`python3` | Interpreter used by `scripts/worker.mjs`. |
| `CLIPFORGE_RELOAD` | unset | `1` enables uvicorn reload. Development only. |
| `CLIPFORGE_VERIFY_PORT` | `3417` | Host port used by `scripts/docker-verify.mjs`. |
| `COMPOSE_PROJECT_NAME` | `clipforge` | Compose project name. The verification script overrides it. |

## Render-specific notes

- `PORT` is injected by Render for both public and private services. Do not pin
  it in `render.yaml`.
- `CLIPFORGE_WORKER_URL` is populated from the private service's `hostport`
  property and normalised to `http://...` by the web app.
- `CLIPFORGE_WORKER_TOKEN` is generated once by Render on the worker and copied to
  the web service. Rotating it requires redeploying both services.
- Optional API keys should be set on the **worker**, not the web service.
- Supabase Storage variables belong on the **worker** only. The Blueprint defaults
  to `local`; switch `CLIPFORGE_STORAGE_BACKEND` to `supabase` and provide the URL
  and service-role key in the Render dashboard. The bucket must already exist and
  be private.
- Put secrets in the Render dashboard (or reference them from a secret store).
  `render.yaml` should only contain `generateValue: true` or non-secret values.
