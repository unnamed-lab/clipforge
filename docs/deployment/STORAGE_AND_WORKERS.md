# Storage and workers

This document states what the application actually does today, what it does not
do, and the migration path if the single-writer model is no longer enough.

## Invariants

1. **One worker process owns the database, queue and scratch space.** SQLite, the
   job queue, model cache and logs live under one writable root
   (`CLIPFORGE_DATA_DIR`, `/data` in containers). Media bytes live there too when
   `CLIPFORGE_STORAGE_BACKEND=local`, or in Supabase Storage when it is `supabase`.
2. **The web tier is stateless.** It proxies HTTP and injects the worker token. It
   never reads media from disk and never receives the Supabase service-role key.
3. **Exactly one worker instance.** A second process against the same SQLite file
   and project directories is unsupported, even if the queue's conditional claim
   prevents duplicate job execution.
4. **No media bytes in the database or job queue.** Jobs carry identifiers,
   timestamps, and a JSON payload. The database stores storage keys, never bytes.
5. **Large media is never loaded into an API response.** Source streaming and
   render downloads use ranged responses; uploads stream to disk first.

## Layout

```
/data
  app.db                     SQLite database (WAL mode)
  app.db-wal, app.db-shm     SQLite sidecars
  worker-token               only when no token env var is provided
  projects/<project_id>/
    audio/audio.wav          16 kHz mono PCM for transcription
    analysis/transcript.json
    analysis/waveform.png
    tmp/                     per-render scratch, cleared before each run
  cache/                     remote media cache (supabase backend only)
    projects/<project_id>/source/<file_id>.<ext>
    projects/<project_id>/renders/<render_id>.mp4
    projects/<project_id>/renders/<render_id>.srt
    assets/<asset_id>.<ext>
  models/                    faster-whisper cache (never baked into the image)
  logs/                      rotating application logs
  tmp/                       service-level scratch
```

With the default `local` backend, media sits directly at
`projects/<project_id>/source`, `projects/<project_id>/renders` and `assets/`
instead of under `cache/`. The database always stores the storage key in
`relative_path`; `storage_path` is a local cache hint, not the source of truth.

All identifiers are generated internally. User-supplied filenames are not used as
paths. File extensions are chosen from validated content, not from the upload
name.

## Storage backends

| Backend | `CLIPFORGE_STORAGE_BACKEND` | Where durable media lives | Use for |
| --- | --- | --- | --- |
| Local | `local` (default) | the worker's data volume | local development, a single host, or Render with a disk |
| Supabase | `supabase` | a private Supabase Storage bucket | keeping media off the worker disk; Docker or Render |

Both backends implement the same `put`, `get`, `exists`, `delete` and
`presigned URL` contract in `apps/worker/src/clipforge/storage/`. FFmpeg always
works on a local copy: cold remote objects are downloaded into `cache/` before
analysis, rendering or streaming, then reused.

Supabase is **storage only**. SQLite, the job queue, transcripts and the model
cache stay local. The worker must still have a writable disk; do not point the
database at the Supabase database.

### Configuration

- Create a **private** bucket first. The default name is `clipforge`; change it
  with `CLIPFORGE_SUPABASE_BUCKET` if needed.
- Set `CLIPFORGE_SUPABASE_URL` to `https://<project-ref>.supabase.co` for a
  hosted project. For a self-hosted Supabase stack running in Docker, use an
  address reachable from the worker container (`host.docker.internal` on
  Windows/macOS, or the service name on Linux). `localhost` inside the worker
  container is not the host.
- Set `CLIPFORGE_SUPABASE_SERVICE_KEY` on the worker only. Never use the anon key
  and never expose the service-role key to the browser.
- Keep `CLIPFORGE_SUPABASE_STORAGE_PREFIX=/storage/v1` for hosted Supabase and
  for the Kong gateway in Supabase's Docker stack. When the URL points directly
  at `supabase/storage-api:latest` (the usual port is 5000), clear the prefix:
  the image serves `/object/...`, not `/storage/v1/object/...`. The image also
  requires its own Postgres database, JWT secret and tenant; this repository does
  not bundle it.
- Missing URL, key or bucket fails at worker startup. There is no silent
  fallback to local disk.

### Free-tier limits

Supabase's free plan is around 1 GB of object storage, 500 MB of database space,
and a few GB of egress per month, and projects pause after inactivity. That is
enough for brand assets and short clips, not for a library of long source videos.
The worker will surface a storage error when the bucket rejects a write. Upgrade
to a paid plan or keep `local` storage if you need sustained video storage. See
the current Supabase pricing page before relying on the free tier in production.

## Upload path

1. Browser posts multipart to the web service.
2. Next.js streams the request body to the worker; it does not buffer the file.
3. The worker checks the declared size and free-space reservation, streams to a
   temporary file, probes the content, and rejects non-media or over-limit input.
4. Only then does the file move into local storage or upload to Supabase Storage.

There is no direct-to-object-storage upload yet. The worker must inspect and probe
the media before accepting it, and the browser must not receive storage
credentials. The trade-off is that the web proxy and worker carry the upload
bytes. Direct signed uploads remain a scaling step, not a current feature.

With Supabase, uploads are made from the worker with the service-role key and
`x-upsert: true`. Brand assets and sidecar captions are uploaded as bytes; sources
and renders stream from their local temporary file.

## Generated artifacts

| Artifact | When | Local backend | Supabase backend | Retention |
| --- | --- | --- | --- | --- |
| Source | import | `projects/<id>/source` | `projects/<id>/source` key | until project deletion |
| Audio WAV | analysis | `projects/<id>/audio` | worker disk only | until project deletion |
| Transcript JSON | analysis | `projects/<id>/analysis` | worker disk + SQLite | replaced by newer runs |
| Waveform PNG | analysis | `projects/<id>/analysis` | worker disk only | replaced by newer runs |
| Render MP4 | render | `projects/<id>/renders` | `projects/<id>/renders` key | until project deletion |
| Caption SRT | render with sidecar | `projects/<id>/renders` | `projects/<id>/renders` key | with the render |
| Logo/music asset | profile edit | `assets/` | `assets` key | until asset or profile deletion |
| Whisper model | first analysis | `models/` | worker disk only | persistent; shared by projects |

Derived files that can be regenerated (audio, transcript JSON, waveform) are not
uploaded. The transcript itself is also stored in SQLite and survives a restart.
Thumbnails and per-frame previews are not generated in this version.

## Temporary storage and cleanup

- Each render runs in `projects/<id>/tmp`, which is cleared before the run.
- FFmpeg is invoked with `cwd` set to that directory and relative filenames, so
  partial outputs cannot escape into the public tree.
- A failed or cancelled render removes its scratch directory; it does not publish
  a partial MP4.
- Interrupted jobs are marked failed on the next worker start. Their scratch files
  may remain until the next render or project deletion; they are never served as
  successful output.
- Deleting a project removes its stored source, renders, sidecars and project
  assets, then its workspace and local cache. Deleting a render removes its MP4 and
  sidecar. Deleting an asset removes the stored object. Revoking library rights
  removes dependent dataset items; deleting the linked project media is opt-in.
- A failed storage deletion raises instead of silently dropping the database row,
  so a retry can remove the orphaned object. Missing objects are treated as already
  deleted.
- Remote objects are cached under `/data/cache`. A cold cache is filled on demand;
  old cache files are removed with the project. `CLIPFORGE_DISK_RESERVE_MB` still
  applies to the worker disk, including the cache.

## Disk sizing

The default Render disk is 20 GB. With the local backend, estimate:

```
source size
+ audio WAV    (~1.9 MB per minute at 16 kHz mono 16-bit)
+ renders      (bitrate × duration × number of kept renders)
+ cache        (cold-copied remote source/render while it is processed)
+ model        (~0.14 GB base, ~0.48 GB small, more for medium)
+ headroom     (CLIPFORGE_DISK_RESERVE_MB, default 512 MB)
```

With Supabase storage, source and render bytes are not kept permanently on the
worker, but analysis and rendering still need a local copy, and the cache may hold
one full source plus one render at a time. Keep enough disk for the largest video
you allow plus headroom; do not assume remote storage eliminates scratch space.

The worker refuses uploads and renders when the reserve would be crossed. A full
disk surfaces as a clear capacity error, not a corrupt render. Supabase has its
own bucket quota; a rejected write is reported as a storage error.

## Queue and recovery

- Jobs are rows in SQLite with `queued | running | completed | failed | cancelled`.
- One consumer thread claims work with a conditional update, so a double click or
  a second process cannot run the same job twice.
- Progress is written at a throttled interval; the UI polls the persisted status.
- Cancellation is cooperative between stages and terminates the FFmpeg process.
- On startup, jobs left `running` are marked failed with an interrupted message.
  They are **not** resumed automatically. Retry them from the UI or API after the
  service is healthy.
- There is no per-job memory limit beyond the container size. One job runs at a
  time; the container plan is the practical memory bound.

This is sufficient for a single-user or small-team deployment. It is not a
distributed job system.

## Graceful shutdown

- Render/Docker sends `SIGTERM`.
- Uvicorn stops accepting requests and gets a 20-second graceful window.
- The queue cancels the active job and waits up to 15 seconds; FFmpeg is killed.
- Docker Compose uses `stop_grace_period: 30s`.
- If the process is still killed, the next start marks the job failed and the user
  retries it. No half-written render is advertised as complete.

## Backup and restore

The database and media must be backed up together; a database row without its
file is worse than no row.

With the local backend, back up the whole data volume. Local Compose, with the
stack stopped:

```bash
docker compose stop
docker run --rm -v clipforge_clipforge-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/clipforge-data-$(date +%Y%m%d).tar.gz -C /data .
docker compose start
```

Restore into a fresh volume with the reverse operation, then start the stack and
check `/api/health` and one project's media.

With Supabase, back up the bucket separately from SQLite. The bucket and database
must come from the same point in time if you need a consistent restore. Supabase
plan features vary; verify that your plan includes the backup or PITR you need.
A local `pg_dump`/bucket export is not enough unless the SQLite file is included.

On Render, use the disk backup or snapshot capability available on your plan if
present. Otherwise stop the worker, copy `app.db` plus any local media/cache, and
verify the archive before deleting or recreating the service. Deleting a service
deletes its disk; deleting a Supabase project deletes its bucket.

## Scaling beyond one instance

The storage interface and Supabase adapter are implemented. The remaining changes
required before horizontal scaling are not:

1. Replace SQLite with PostgreSQL (or another shared database) and move the queue
   to a shared broker (Redis, SQS, etc.).
2. Add direct multipart uploads with short-lived signed URLs, so the web tier does
   not carry media bytes. The adapter already exposes signed reads; signed uploads
   and the client flow are not wired.
3. Separate the API from the worker so each can scale independently, with job
   leases, heartbeats, retries, and idempotency keys.
4. Add per-job resource limits and a bounded concurrency setting.

Until those exist, the correct deployment is one worker with one disk. The
Blueprint and Compose file enforce that shape, even when media itself is remote.
