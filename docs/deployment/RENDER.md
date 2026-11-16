# Render deployment

`render.yaml` is a Blueprint for a single-instance deployment:

```
Public web (Next.js standalone, stateless)
        │  private HTTP + token
        ▼
Private worker (FastAPI + queue + FFmpeg, exactly 1 instance)
        │
        ├── Persistent disk at /data (SQLite + model cache + scratch)
        └── Optional Supabase Storage (source, renders, captions, assets)
```

No service hostnames, credentials, or ports are hard-coded. Render injects
`PORT`; the web service receives the worker's internal `host:port` and the
generated token through `fromService`.

The disk is required in every configuration because SQLite, the model cache and
FFmpeg scratch space stay local. Supabase changes where durable media lives, not
the single-worker topology.

## What the Blueprint creates

| Service | Type | Plan | Disk | Notes |
| --- | --- | --- | --- | --- |
| `clipforge-worker` | private (`pserv`) | `standard` | 20 GB at `/data` | owns the database and queue; never public |
| `clipforge-web` | public (`web`) | `starter` | none | all browser traffic; proxies to the worker |

Approximate list cost at the time of writing: **$37/month** (web $7, worker $25,
20 GB disk $5). Confirm current Render pricing and region before applying; prices
change and are not encoded as truth here.

## Apply the Blueprint

These steps require a Render account and access to the GitHub repository. They
cannot be performed from this machine.

1. Push the repository to GitHub (branch `main`).
2. In Render, choose **New → Blueprint**.
3. Connect the repository and select the branch.
4. Render reads `render.yaml`. Review:
   - the two services and their plans,
   - the 20 GB disk on the worker,
   - the generated `CLIPFORGE_WORKER_TOKEN`,
   - the optional Supabase URL and service-role key prompts. Leave them blank and
     keep `CLIPFORGE_STORAGE_BACKEND=local` to start with disk storage.
5. Apply the Blueprint. The worker builds first; the web service follows.
6. Wait for both deploys to finish. The web health check is `/`; the worker image
   has a Docker `HEALTHCHECK` on `/api/health`.
7. Open `https://<web-service>/api/health`. It should return the worker's health
   JSON through the proxy. If it returns 503, the web service cannot reach the
   worker or the token does not match.

The first deploy does not download a transcription model. That happens on the
first analysis job, which is why the worker needs a persistent disk and why the
first job is slower.

## Environment variables

Render sets these automatically from `render.yaml`:

| Service | Variable | Source |
| --- | --- | --- |
| web | `CLIPFORGE_WORKER_URL` | worker `hostport` (scheme normalised in code) |
| web | `CLIPFORGE_WORKER_TOKEN` | worker's generated env var |
| web | `PORT` | Render |
| worker | `CLIPFORGE_WORKER_TOKEN` | generated once by Render |
| worker | `PORT` | Render |
| worker | `CLIPFORGE_DATA_DIR` | `/data` |
| worker | `CLIPFORGE_WORKER_HOST` | `0.0.0.0` |
| worker | `CLIPFORGE_STORAGE_BACKEND` | `local` in the Blueprint |
| worker | `CLIPFORGE_SUPABASE_*` | optional dashboard secrets; worker only |

Everything else has a code default. See
`docs/deployment/ENVIRONMENT_VARIABLES.md`. Add optional keys (YouTube/Twitch)
in the Render dashboard or in `render.yaml`; do not commit values.

### Using Supabase Storage

1. Create a private bucket in the Supabase project, for example `clipforge`.
2. In Render, open the **worker** service → Environment.
3. Set `CLIPFORGE_STORAGE_BACKEND=supabase`, `CLIPFORGE_SUPABASE_URL`,
   `CLIPFORGE_SUPABASE_SERVICE_KEY` and (if different)
   `CLIPFORGE_SUPABASE_BUCKET`. Keep
   `CLIPFORGE_SUPABASE_STORAGE_PREFIX=/storage/v1` for hosted Supabase; it is
   already the default in the Blueprint.
4. Redeploy the worker. If any value is missing or wrong, the worker fails at
   startup instead of quietly writing to the disk.
5. Upload a short clip and confirm it appears in the bucket and can be streamed
   back. Then delete the project and confirm the objects are gone.

Do not add these variables to the web service. The service-role key bypasses row
security and must never reach the browser. Cloud Supabase is reachable from
Render over HTTPS; a self-hosted Supabase instance on your own machine is not,
unless you expose it securely.

## Manual configuration after the first deploy

- **Custom domain** (optional): add it to the web service, not the worker.
- **API keys** (optional): `CLIPFORGE_YOUTUBE_API_KEY`,
  `CLIPFORGE_TWITCH_CLIENT_ID`, `CLIPFORGE_TWITCH_CLIENT_SECRET` on the worker.
- **Larger disk**: change `sizeGB` in `render.yaml` and apply. Render can grow a
  disk, but shrinking is not supported. Take a backup first.
- **Worker size**: `standard` (2 GB) is the minimum sensible size for FFmpeg plus
  `base` transcription. `starter` is likely to be OOM-killed during a render.
- **Notifications / autoscaling**: leave autoscaling off. The worker is
  single-writer and cannot be horizontally scaled.

## Migrations

There is no separate migration command. On startup the worker:

1. creates missing tables from `schema.sql` (`CREATE TABLE IF NOT EXISTS`),
2. applies additive column migrations by inspecting `PRAGMA table_info`,
3. starts the queue and marks orphaned `running` jobs as failed.

The migrations are additive. Before deploying a schema change to a production
disk, take a copy of `/data/app.db` (and ideally the whole volume). If a migration
needs a destructive rewrite, it must be implemented and tested explicitly; this
repository does not have one.

## Restart, shutdown, and recovery

- Render sends `SIGTERM` on deploy or manual restart.
- Uvicorn gets a 20-second graceful window; the queue cancels the current job and
  waits up to 15 seconds; FFmpeg is terminated cooperatively.
- A job that cannot finish in that window is marked **failed** on the next start.
  It is not silently resumed. Use the retry action for analysis or render after
  the service is healthy again.
- Renders are published only after FFprobe validates the output, so an interrupted
  render does not produce a download that claims success.
- The disk survives deploys and restarts. Deleting the service deletes the disk.
  Back it up before deleting or recreating the service.

## Large uploads

The browser uploads to the web service, which streams the body to the worker. The
body is not buffered in memory, but it does pass through the web tier. Limits:

- `CLIPFORGE_MAX_UPLOAD_MB` (default 2048) is enforced by the worker.
- The worker checks declared size, content type, and free space before writing.
- Even with Supabase Storage, the worker needs a local copy for FFprobe, analysis
  and FFmpeg, and the cache can hold one source plus one render. Keep at least the
  largest accepted source plus headroom free.
- With local storage, the disk must hold the source, extracted audio, waveform,
  transcript, and every render. 20 GB is a starting point, not a guarantee for
  long sources.

For sources near the limit, prefer a local deployment or split the source before
upload. Direct browser-to-storage uploads are not implemented; see
`docs/deployment/STORAGE_AND_WORKERS.md`.

## Expected first-deploy checks

Run these against the deployed web URL:

```bash
curl -fsS https://<web-service>/api/health
curl -fsS https://<web-service>/api/capabilities
```

Then, in the browser:

1. Create a project and upload a short clip.
2. Wait for inspection to complete (no model needed).
3. Start analysis; the first run downloads the model to `/data/models`.
4. Review a candidate and render a 9:16 clip.
5. Download the MP4 and confirm it plays.

Anything that fails at step 3 is usually disk space, worker memory, or the model
download being blocked. Check the worker logs in Render.

## Known limitations on Render

- One worker instance only: SQLite and the in-process queue require a single
  writer, even when media is in Supabase.
- No multi-region and no horizontal scale.
- No automatic job resume after a hard kill; retry manually.
- The Supabase free tier's storage, egress and inactivity limits are not suitable
  for a library of long videos. Budget for a paid plan or use the disk.
- Private services do not get a public URL, by design. Do not add one.
- The web service has no disk. If it ever needs local state, that is a design
  change, not a configuration change.
- Browser automation is not part of this repository; the UI must be checked
  manually after a deploy.
