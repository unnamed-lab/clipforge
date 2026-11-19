# Known limitations

This page states what Clipforge does **not** do, and what its automated checks do and do not prove. It is deliberately blunt: an accurate limitation is more useful than an optimistic feature list.

If a capability is missing here but present in the code, the code wins. Check the [API reference](API.md) and `GET /api/capabilities` before assuming a feature is absent.

## Contents

1. [Product scope](#product-scope)
2. [Transcription and understanding](#transcription-and-understanding)
3. [Editing and composition](#editing-and-composition)
4. [Packaging and research](#packaging-and-research)
5. [Operations and scale](#operations-and-scale)
6. [Security model](#security-model)
7. [Verification](#verification)
8. [Platform-specific notes](#platform-specific-notes)

---

## Product scope

Clipforge is a **single-user, local-first desktop-style web app**. It is not:

- a multi-tenant service with accounts, roles or per-user isolation;
- a publishing tool (no TikTok, YouTube, Instagram or X upload);
- a cloud transcoding service;
- a model-training platform;
- a replacement for a nonlinear editor.

Everything is served from one web process and one worker process against one data directory. There is no job scheduler across machines, no object-store queue and no horizontal worker pool.

No licence file is present in the repository yet. Treat the source as all rights reserved until one is added.

---

## Transcription and understanding

- **CPU-only speech recognition in practice.** `CLIPFORGE_WHISPER_DEVICE` accepts `cpu` or `cuda`, but only the CPU int8 path is supported and tested, and `/api/capabilities` reports GPU transcription as unavailable (`gpu_required: false`, `acceleration_for_transcription: false`). A `cuda` value requires a CUDA-capable CTranslate2 build this install does not provide. A one-hour recording on the reference four-core laptop takes tens of minutes with `small`.
- **No speaker diarisation.** The diarisation provider is a stub that reports unavailable. Speaker labels, "who said what" filters and speaker-aware candidate selection are not implemented. `include_speakers` exists in the brief but cannot be grounded in speaker data.
- **No face tracking, active-speaker detection or gaze-aware reframing.** Composition is scene-level: aspect, edges, text density and motion, not faces.
- **Word timings are model output.** ASR errors propagate into captions and scoring. The worker stores corrections separately from the transcript, but the workspace has no correction editor. Corrections are API-only and do not re-rank existing candidates.
- **Language coverage is whatever the configured faster-whisper model supports.** There is no per-language validation beyond a short hint and detected probability.
- **The score is an editorial estimate.** It is a weighted average of transcript features. It is not a prediction of reach, watch time, revenue or platform distribution. No engagement data is used.
- **Optional LLM rescoring is not reliably reproducible.** Even at low temperature, provider models can change. Every run records the method and version it used, but an old score may not reproduce exactly.
- **No audio event understanding.** Laughter, applause, music cues and crowd noise are not classified. Features are text-derived, with speech density standing in for pace.
- **No semantic fact-checking.** Packaging rejects some unsupported claims through rules and the rubric, but it cannot verify that a statement is true.

---

## Editing and composition

- **One clip at a time in the UI.** The workspace selects a candidate and exports it. The render spec and API accept multiple segments (1–40), but there is no UI for authoring a multi-segment timeline.
- **No timeline editor.** There are no tracks, layers, keyframes, transitions, speed ramps, animation or manual subtitle placement. Trims are in/out points with transcript snapping.
- **`split` / stacked composition is unavailable for a single source.** It degrades to `fit` with an explicit warning. This is a single-source product; showing two synchronous sources is not implemented.
- **Auto composition is heuristic.** It samples scene changes and text/motion evidence, then chooses crop, contain or picture-in-picture. It can choose wrongly on unusual footage. Warnings are the honest signal; manual composition exists for that reason.
- **Contain mode scales the source down.** On a 16:9 source in a 9:16 frame, the picture is small. The gate warns when text may become unreadable, but it does not make the text readable.
- **Picture-in-picture uses a blurred background.** The layout is not a designed split screen with independent foreground and background framing.
- **Caption text is not editable in the workspace.** Word corrections exist through the API. Burned-in captions are generated from stored timings and the selected preset.
- **Caption readability is measured geometrically.** The gate checks rendered bounds against the frame and safe area. It does not judge contrast against a moving background, reading speed or line-break quality.
- **No automatic loudness standard compliance.** Normalisation applies FFmpeg loudness normalisation. The gate checks that audio exists and that A/V sync is plausible; it does not certify −14 LUFS or broadcast delivery.
- **Logo and music are profile/API concepts.** The workspace export panel exposes composition, captions, normalisation and compression. Logo overlays, music beds, ducking and fades require the render API with an uploaded asset.
- **Boundary review is a suggestion.** It detects trailing shots and proposes alternative out points. It can miss a bad ending or propose an unnecessary cut. Nothing is applied automatically.

---

## Packaging and research

- **Packaging is a drafting tool.** It produces titles, descriptions, hashtags, keywords and thumbnail text for a human to review. There is no scheduling or publishing integration.
- **No engagement metrics are invented.** When a trend or research provider is unconfigured, the API reports it as unavailable. A manual trend is stored as a reference with no fabricated volume or competition score.
- **Research providers are optional and often unavailable.** YouTube Data API, Twitch and trends endpoints require credentials. YouTube keyword suggestions use an undocumented public endpoint and are opt-in.
- **Title scoring is a rubric, not a guarantee.** The rubric rewards specificity, claim support and platform fit. It cannot predict performance.
- **Trends are not a current-events oracle.** With no provider configured, the trend check is empty; with one configured, it reflects that provider's data and region, not the whole internet.
- **No A/B testing or attribution.** Clipforge does not post variants, measure them or learn from published results. The evaluation dataset routes record human judgements, not platform outcomes.

---

## Operations and scale

- **One worker process, one SQLite writer.** Do not run two workers against the same data directory. Do not mount the SQLite file on a network filesystem. The queue is a single-consumer FIFO, so one slow transcription delays every later job, including renders.
- **Resume is download-only.** A URL import interrupted by an outage or a worker restart resumes from the bytes it already fetched. Transcription and rendering have no checkpoint: an interrupted job of those types is marked failed (`worker_restarted`) and starts over when retried. Partial renders are discarded.
- **Cancellation is cooperative.** FFmpeg/ffprobe subprocesses are terminated and handlers check between stages. A blocking library call may take time to notice.
- **Retry usually runs the job again from the start.** The exception is a URL import, which resumes whatever it still has on disk. There is no checkpointing inside transcription or rendering.
- **Disk usage is real.** Sources are retained for renders. Proxies, audio, waveform images, model caches, scene thumbnails, drafts and finished renders accumulate under `data/` or the Compose volume. The reserve floor prevents the disk from reaching zero; it does not clean up for you.
- **Upload is a single multipart request.** There is no direct browser-to-storage upload and no resumable upload. Large files consume worker memory and request time, and are limited by `CLIPFORGE_MAX_UPLOAD_MB`.
- **URL import depends on `yt-dlp` and the source site.** It can break when a site changes, when a video is region-locked, or when a rendition is unavailable. Inspection is metadata-only and its size is approximate. A download keeps a sidecar resume manifest next to its fragments; fragments without a matching manifest are discarded rather than appended to.
- **No pagination.** Project lists, candidate lists and library lists return full result sets. This is fine for a personal library; it is not designed for thousands of projects.
- **SQLite schema changes are additive by hand.** There is no migration framework. New columns are declared in `db/connection.py` and applied at startup with `ALTER TABLE` when missing. Destructive schema changes require a manual migration and a backup.
- **Model download requires network access.** A first run without a cached model fails if the host is offline. The worker image deliberately contains no models; Compose downloads whichever model is configured (the Compose default is `base`) into the data volume on first use.
- **Windows-specific path handling is tested through scripts, not a separate CI job.** CI runs Ubuntu. `scripts/dev.ps1` and the Python launcher are the supported Windows path, but CI does not execute them.
- **No telemetry or usage analytics.** There is no built-in dashboard for job throughput beyond the API and logs.

---

## Security model

- **One shared token, not user authentication.** Anyone with the token can use the whole API. There are no roles, scopes, sessions or audit log per user. Keep the worker on loopback or a private network.
- **The web app is the only intended client.** The BFF proxy injects the token so the browser never sees it. A direct worker URL exposed to the internet with the token is equivalent to handing over the service.
- **URL import is SSRF-guarded, not sandboxed.** The URL validator rejects unsafe shapes, but `yt-dlp` and FFmpeg still process untrusted remote media. Run the worker with the least privilege you can.
- **Media parsing is delegated to FFmpeg.** A malicious file exploits FFmpeg, not a memory-safe parser. Keep FFmpeg current.
- **Optional external AI sends transcript text.** LLM rescoring and LLM packaging require explicit provider configuration. Rescoring also requires per-run consent. Packaging sends the brief and transcript when **Use the configured LLM** is enabled; the checkbox is on by default, so turn it off for a fully local run.
- **Supabase Storage is an explicit trust boundary.** Selecting it uploads durable media to your Supabase project. SQLite, scratch files and models remain local.
- **Secrets live in the environment.** Do not put the worker token, Supabase service key or LLM key in a `NEXT_PUBLIC_*` variable, in client code or in a committed `.env`.
- **Token rotation requires a restart.** The token is read at startup and compared on each request; there is no hot reload.

---

## Verification

### What the automated gate covers

`node scripts/verify.mjs` runs, in fail-fast order:

1. Web TypeScript typecheck (`tsc --noEmit`).
2. Web ESLint.
3. Worker Ruff.
4. Shared package tests (Vitest).
5. Web tests (Vitest).
6. Worker tests with `pytest -m "not slow"`.

`--build` adds the production Next.js build. `--docker` adds the Compose image build and smoke test. `--slow` includes worker tests that run real faster-whisper inference.

`.github/workflows/ci.yml` runs the web checks (typecheck, lint, shared tests, web tests, production build), the worker checks (Ruff, fast tests, with FFmpeg installed), and `scripts/docker-verify.mjs` for the Compose images and smoke test. CI does not run slow inference tests and does not run a browser.

### What is not automated

- **No browser automation.** `playwright.config.ts` and `tests/e2e/smoke.spec.ts` exist, and `package.json` defines `test:e2e`, but `@playwright/test` is not a declared dependency and no CI job runs it. UI claims in these docs were checked against source and, where stated, a manual run.
- **No visual regression tests.** Layout, colour and caption rendering are not screenshot-tested.
- **No cross-browser matrix.** The app is developed against Chromium. Safari and Firefox behaviour is not part of the gate.
- **No load or soak testing.** Long sessions, many projects and multi-gigabyte sources are not exercised automatically.
- **No real-host GPU test.** GPU transcription is unsupported, so no CUDA test exists.
- **No security scanning in CI.** Dependency and licence scanning are not configured.

### Documentation verification record

This documentation was written against the source tree on **2026-11-19**. The route inventory, request models, job states, schema, preset lists, composition fallback and verification steps were read from the files listed below. The automated checks were **not** re-run for a documentation-only change, and no browser pass was performed.

| Claim area | Evidence read |
| --- | --- |
| Routes, auth, errors | `apps/worker/src/clipforge/api/app.py`, `routes_*.py`, `models.py` |
| Job states and dedupe | `apps/worker/src/clipforge/jobs/queue.py`, `db/repositories.py` |
| Schema and upgrades | `apps/worker/src/clipforge/db/schema.sql`, `db/connection.py` |
| Analysis stages and transcript reuse | `apps/worker/src/clipforge/pipeline/analysis.py` |
| Composition fallback and warnings | `apps/worker/src/clipforge/rendering/composition.py`, `rendering/spec.py` |
| Quality gate checks | `apps/worker/src/clipforge/quality/gate.py` |
| Brief and editorial controls | `apps/worker/src/clipforge/briefs/spec.py`, `packages/shared/src/presets.ts` |
| Packaging fields and trend actions | `apps/worker/src/clipforge/api/models.py`, `routes_packaging.py` |
| UI controls | `apps/web/src/app/**/*.tsx` |
| Verification steps | `scripts/verify.mjs`, `.github/workflows/ci.yml`, `package.json` |

When you change behaviour, update the relevant reference doc and add the check that proves it. Do not change this section to claim a run that did not happen.

---

## Platform-specific notes

### Windows

- Use PowerShell and the documented `py -3.13` launcher. The worker resolves `apps/worker/.venv/Scripts/python.exe`.
- Run `scripts/dev.ps1` from the repository root. It starts both processes and terminates both on Ctrl+C.
- Paths with spaces are supported through `scripts/worker.mjs`; invoking Python directly from a shell that splits paths is not.
- Long paths and antivirus scanning can slow model loading and FFmpeg scratch writes.

### macOS

- FFmpeg from Homebrew must include `libass` and `libx264`; a minimal build may lack them.
- Apple Silicon runs CPU inference through the same `int8` path. There is no Metal or Core ML transcription backend in this release.

### Linux

- Install `ffmpeg` with the required filters. Some distribution packages split `libass` or omit `showwavespic`.
- The worker must be able to write the data directory and create subprocesses.
- If you run Compose, keep one worker replica. `deploy.replicas` greater than one is unsupported.

### Docker and hosted

- The worker is reachable only on the Compose network; do not publish port 8788.
- The Render Blueprint uses a persistent disk and a single worker instance. Scaling the worker to multiple instances would corrupt queue semantics and SQLite access.
- Ephemeral filesystems without a persistent disk lose SQLite and, with local storage, every source and render.
