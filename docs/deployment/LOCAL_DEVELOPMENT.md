# Local development

Two processes run on the host: the Next.js web app (Turbopack in development) and
the Python worker. With the default local storage backend, media never leaves the
machine unless optional external AI is explicitly enabled. Selecting the Supabase
backend uploads media to your Supabase project.

## Prerequisites

| Tool | Version used here | Notes |
| --- | --- | --- |
| Node.js | 22.14.0 | `>=20` is declared; use an LTS |
| pnpm | 12.11.2 | pinned by `packageManager`; `corepack enable` |
| Python | 3.13.3 | `requires-python >=3.11,<3.14`; 3.14 has no ctranslate2 wheel |
| FFmpeg | 6.x or newer | must include `libx264`, AAC, `libass`, `subtitles`, `showwavespic` |
| FFprobe | same package as FFmpeg | used for every inspection |

Verify the media tools before debugging anything else:

```bash
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters | grep -E "subtitles|ass|showwavespic"
```

## Install

```bash
# JavaScript workspace
corepack enable
pnpm install

# Python worker (Windows)
cd apps/worker
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
cd ../..

# Python worker (macOS / Linux)
cd apps/worker
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cd ../..
```

The worker resolves its interpreter from `CLIPFORGE_PYTHON`, then
`apps/worker/.venv`, then `PATH`. `node scripts/worker.mjs python` prints the one
it will use.

## Configuration

```bash
cp .env.example .env
```

For host development leave `CLIPFORGE_WORKER_TOKEN` empty. The worker generates
`data/worker-token` on first boot and the web app reads that file. Do not commit
`.env` or anything under `data/`.

The relevant defaults are `http://127.0.0.1:8788` for the worker and
`http://localhost:3000` for the web app.

### Storage

`CLIPFORGE_STORAGE_BACKEND` defaults to `local`, which writes media under
`data/`. To test Supabase Storage, create a private bucket and set on the worker:

```dotenv
CLIPFORGE_STORAGE_BACKEND=supabase
CLIPFORGE_SUPABASE_URL=https://<project-ref>.supabase.co
CLIPFORGE_SUPABASE_SERVICE_KEY=<service-role-key>
CLIPFORGE_SUPABASE_BUCKET=clipforge
```

Restart the worker after changing these; they are read at startup. Do not put the
service-role key in a `NEXT_PUBLIC_*` variable. If you run Supabase itself in
Docker on the host and the worker runs in a container, use an address the worker
can reach (`host.docker.internal`), not `localhost`.

Hosted Supabase uses `CLIPFORGE_SUPABASE_STORAGE_PREFIX=/storage/v1` (the
default). If the URL targets `supabase/storage-api:latest` directly, set the
prefix to an empty value; that image mounts the API at `/object/...`. Its own
Postgres database and JWT configuration are separate from Clipforge.

The worker still keeps SQLite, the model cache and scratch files under
`CLIPFORGE_DATA_DIR`. Remote objects are cached there while they are processed.

## Run

Terminal 1 — worker:

```bash
pnpm worker:dev
```

Terminal 2 — web, with Turbopack:

```bash
pnpm dev
```

`pnpm dev` runs `next dev --turbopack --port 3000`. Turbopack is for development
only; `next build` still uses the supported webpack production pipeline, so a
Turbopack change cannot silently alter a release build.

Open <http://localhost:3000>. The header shows whether the worker is reachable.
`http://localhost:3000/api/health` is proxied through Next.js and is the quickest
end-to-end check: if it succeeds, the proxy, the token, and the worker are all
working.

## Checks

```bash
pnpm typecheck                 # web TypeScript
pnpm lint                      # web ESLint (flat config)
pnpm --filter @clipforge/shared test
pnpm --filter @clipforge/web test
pnpm lint:worker               # ruff (E9/F)
pnpm test:worker               # pytest, fast suite
node scripts/verify.mjs        # all of the above in fail-fast order
node scripts/verify.mjs --build
```

`node scripts/verify.mjs --build` adds the production Next.js build. It does not
start Docker; use `--docker` for the container gate.

Browser verification is not automated in this repository (no Playwright
dependency is installed). Treat a UI change as unverified until it has been
exercised manually against a running pair, including loading, empty, and error
states.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `No module named ctranslate2` | venv built with Python 3.14 | recreate with 3.13 |
| Web pages load, actions return 503 | worker not running or wrong URL | `pnpm worker:dev`; check `CLIPFORGE_WORKER_URL` |
| `401` from the worker through the proxy | web and worker tokens differ | delete `data/worker-token`, restart both; or set the same env value |
| `ffmpeg not found` in capabilities | FFmpeg not on `PATH` | install FFmpeg and restart the worker |
| First analysis is slow | model download to `data/models` | expected once; `base` is faster than `small` |
| `No space left on device` | disk reserve or volume full | free space or move `CLIPFORGE_DATA_DIR` |
| Worker exits at startup with a Supabase message | backend selected without URL/key/bucket | set all three; see `.env.example` |
| `Object storage rejected the configured credentials` | wrong key or anon key used | use the service-role key on the worker only |
| `Object storage could not be reached` | URL not reachable from the worker | for self-hosted Supabase use `host.docker.internal`, not `localhost` |
| Port 3000 already in use | another dev server | `pnpm --filter @clipforge/web dev -- --port 3001` |

## Running the web app against the Docker worker

Docker Compose does not publish the worker port by default, because it is an
internal service. If you want the convenience of Turbopack on the host while the
worker runs in a container, add a local override that publishes it on loopback
only and point the host web app at it. See `docs/deployment/DOCKER.md`.
