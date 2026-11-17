# Docker

The Compose stack mirrors the hosted topology:

| Service | Image | Exposure | State |
| --- | --- | --- | --- |
| `web` | Next.js standalone | `localhost:3000` | none |
| `worker` | FastAPI + queue + FFmpeg | internal only on `8788` | named volume `clipforge-data` at `/data`; media local by default or in Supabase Storage |

There is deliberately no Postgres or Redis service. The application uses SQLite
and an in-process queue; adding unused infrastructure would not make the stack
more production-ready. Supabase, when selected, is an external Storage API, not
a Compose service.

Keep the named volume either way: it holds SQLite, the transcription model cache,
scratch files and (with the default backend) all media. The worker is still a
single-writer service.

## First run

```bash
cp .env.example .env
```

Set a real token in `.env` (the compose file refuses to start without one):

```bash
# macOS / Linux / Git Bash
node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"

# PowerShell
node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"
```

Put it in `.env`:

```dotenv
CLIPFORGE_WORKER_TOKEN=<generated-value>
```

Then:

```bash
docker compose up --build
```

Compose waits for the worker health check before starting the web app. Open
<http://localhost:3000>. The web image is stateless; durable database and model
state is always in the `clipforge-data` volume.

If port 3000 is busy, set `CLIPFORGE_WEB_PORT=3100` in `.env`.

## Optional Supabase Storage

Docker does not run Supabase. To keep source videos, renders and brand assets in
a Supabase project, create a private bucket, then set on the worker:

```dotenv
CLIPFORGE_STORAGE_BACKEND=supabase
CLIPFORGE_SUPABASE_URL=https://<project-ref>.supabase.co
CLIPFORGE_SUPABASE_SERVICE_KEY=<service-role-key>
CLIPFORGE_SUPABASE_BUCKET=clipforge
```

Restart the worker after changing these. The web container does not receive the
service-role key. Missing values fail at startup rather than falling back to the
volume.

For a **self-hosted Supabase stack running on the host**, the worker container
must not use `localhost`; that is the container itself. Use
`http://host.docker.internal:8000` on Windows/macOS with the storage API's mapped
port (check your Supabase Compose port mapping), or the Supabase service name if
it is on the same Docker network. The URL must point at a server that implements
the Supabase Storage REST API. A plain Postgres container is not enough.

If you run the `supabase/storage-api:latest` image directly, point the worker at
it and clear the gateway prefix:

```dotenv
CLIPFORGE_SUPABASE_URL=http://storage-api:5000
CLIPFORGE_SUPABASE_STORAGE_PREFIX=
```

The direct image serves `/object/{bucket}/{key}`. Behind Supabase's Kong gateway
the same API is exposed at `/storage/v1/object/{bucket}/{key}`, which is why the
prefix defaults to `/storage/v1`. The storage API image requires a Postgres
database, `AUTH_JWT_SECRET`, a tenant and a bucket; it is not a standalone
container, and this Compose file deliberately does not bundle it.

The verification script forces `local` storage so it can prove the volume without
touching a real bucket.

## Verify without touching local data

```bash
node scripts/docker-verify.mjs
```

That script uses its own Compose project (`clipforge-verify`), its own volume, and
port `3417`. It builds both images, starts the stack, proves the web proxy reaches
the worker, runs FFmpeg/FFprobe inside the worker image, uploads a one-second
clip through the proxy, confirms the file and database are on the worker volume,
and removes everything afterwards. It never downloads a transcription model and
it overrides `CLIPFORGE_STORAGE_BACKEND=local`, so a Supabase setting in your
`.env` cannot change what the check proves.

`node scripts/docker-verify.mjs --keep` leaves the stack running for inspection.

## Everyday commands

```bash
docker compose build              # rebuild both images
docker compose up -d              # start in the background
docker compose ps                 # status and health
docker compose logs -f worker     # worker logs
docker compose logs -f web        # Next.js logs
docker compose exec worker sh     # shell as the unprivileged user
docker compose down               # stop; keep the data volume
docker compose down -v            # stop and delete all local container data
```

Inspect the volume without leaving the container:

```bash
docker compose exec worker ls -la /data
docker compose exec worker ls -la /data/projects
```

## Volume ownership

The worker process runs as UID/GID `1001` (`clipforge`). A fresh named volume or
a Render disk is often mounted root-owned, so PID 1 is the entrypoint, which
starts as root only long enough to `chown` the mount root and then `exec`s
`gosu clipforge`. Uvicorn never runs as root. The image intentionally has no
`USER clipforge` line; that would block the one-time ownership fix.

If you replace the named volume with a bind mount, the host directory must be
writable by UID `1001`:

```yaml
# compose.override.yaml — local only, do not commit
services:
  worker:
    volumes:
      - ./data:/data
```

```bash
# Linux only; Docker Desktop on Windows/macOS handles this differently.
sudo chown -R 1001:1001 ./data
```

Do not bake `data/`, models, or renders into an image. `.dockerignore` excludes
them, and the runtime writes them to the volume.

## Using a Docker worker with host Turbopack

Development is fastest with Next.js on the host. To run only the worker in
Docker, publish its port on loopback and point the host web app at it:

```yaml
# compose.override.yaml
services:
  worker:
    ports:
      - "127.0.0.1:8788:8788"
```

```bash
docker compose up -d worker
# .env: CLIPFORGE_WORKER_URL=http://127.0.0.1:8788
# .env: CLIPFORGE_WORKER_TOKEN=<same token as the worker>
pnpm dev
```

Never publish `8788` on a public interface. The token is the only thing between
the internet and a service that can process local media.

## Image design

`apps/web/Dockerfile`

- `node:22.14.0-bookworm-slim`, pnpm `12.11.2` installed with npm (the corepack
  shim expects a file pnpm 12 no longer ships).
- Multi-stage: manifests → frozen install → build → standalone runner.
- `output: "standalone"` with `outputFileTracingRoot` at the monorepo root, so the
  workspace package and pnpm links are traced correctly.
- Production dependencies only; no dev dependencies, source maps, or `.git`.
- Runs as `nextjs` UID `1001`.
- `PORT` and `HOSTNAME` are honoured by the standalone server.

`apps/worker/Dockerfile`

- `python:3.13.3-slim-bookworm` with `ffmpeg`, `fonts-dejavu-core` and `gosu`.
- Installs `apps/worker/requirements.lock` first, then the project with
  `--no-deps`, so builds do not silently float.
- No models, media, database, credentials, or GPU libraries.
- The application runs as `clipforge` UID `1001`; only the entrypoint starts as
  root, to claim a freshly mounted volume before dropping privileges.
- Uvicorn runs with `--proxy-headers` and a 20-second graceful shutdown window.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Set CLIPFORGE_WORKER_TOKEN` | `.env` missing the token | add it; see above |
| Web starts, every action 503 | worker unhealthy | `docker compose logs worker` |
| Web health check fails | worker token mismatch | same `CLIPFORGE_WORKER_TOKEN` for both |
| `permission denied` on `/data` | bind mount owned by root/another UID | `chown -R 1001:1001 ./data` |
| `worker-entrypoint: not found` | CRLF line endings on `docker/worker-entrypoint.sh` | keep LF; `.gitattributes` enforces it |
| Build is very slow first time | base images, pip wheels, pnpm store | expected; later builds reuse layers |
| Container killed during render | Compose stop grace < encode time | `stop_grace_period: 30s` is set; retry the render |
| Disk fills during model download | volume too small | recreate the volume with more space or use `base` |

## What this does not do

- It does not scale the worker. One worker, one SQLite writer, one volume.
- It does not run Supabase or any other object storage. Supabase is configured as
  an external HTTPS endpoint.
- It does not upload directly from the browser to object storage. Uploads stream
  through the web proxy into the worker, which validates and probes them. See
  `docs/deployment/STORAGE_AND_WORKERS.md`.
- It does not run a separate migration job. Schema migrations run at worker
  startup and are additive.
