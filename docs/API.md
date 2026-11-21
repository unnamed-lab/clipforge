# API reference

The worker is a FastAPI service that owns media, transcription, ranking, rendering and persistence. The browser normally reaches it through the Next.js proxy; this document describes the worker contract directly.

- **Direct base URL:** `http://127.0.0.1:8788`
- **Through the web app:** `http://localhost:3000` (`/api/[...path]` proxy)
- **Prefix:** every route starts with `/api`
- **OpenAPI:** `/api/openapi.json`; docs UI at `/api/docs`

The OpenAPI document is generated from the same Pydantic models used at runtime, so it is authoritative for fields and literals. It is also behind the token guard; request it with the header:

```bash
curl -H "x-clipforge-token: $CLIPFORGE_WORKER_TOKEN" \
  http://127.0.0.1:8788/api/openapi.json
```

The FastAPI docs UI cannot attach a custom header by itself, so it is not a usable interactive console without a proxy that injects the token.

## Contents

1. [Authentication](#authentication)
2. [Errors](#errors)
3. [Job model](#job-model)
4. [Response conventions](#response-conventions)
5. [System routes](#system-routes)
6. [Projects](#projects)
7. [Sources, media and transcript](#sources-media-and-transcript)
8. [Analysis and candidates](#analysis-and-candidates)
9. [Jobs](#jobs)
10. [Project settings, briefs and corrections](#project-settings-briefs-and-corrections)
11. [Renders](#renders)
12. [Packaging](#packaging)
13. [Profiles and assets](#profiles-and-assets)
14. [Library, evaluations and datasets](#library-evaluations-and-datasets)
15. [End-to-end example](#end-to-end-example)

---

## Authentication

Every `/api/*` route requires a shared secret in a header:

```http
x-clipforge-token: <token>
```

The only public exception is:

```http
GET /api/health
```

Behaviour:

- In host development, leave `CLIPFORGE_WORKER_TOKEN` empty. The worker generates `data/worker-token` on first boot, and the web app reads that file.
- In Docker Compose and hosted deployments, set the token explicitly. Compose refuses to start without it.
- The comparison is constant-time (`hmac.compare_digest`).
- Requests through the Next.js proxy do not need the header from the browser; the server-side route injects it. Direct worker calls do.
- CORS is restricted to `CLIPFORGE_CORS_ORIGINS` and permits `content-type` and `x-clipforge-token`.

Missing or wrong token:

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Missing or invalid worker token"
  }
}
```

HTTP status: `401`.

---

## Errors

Application errors, validation errors and unexpected errors share one envelope:

```json
{
  "error": {
    "code": "validation_failed",
    "message": "url: String should have at least 8 characters"
  }
}
```

| Status | Typical `code` | Meaning |
| --- | --- | --- |
| `400` | `bad_request` | The request cannot be applied as sent. |
| `401` | `unauthorized` | Missing or wrong token. |
| `404` | `not_found`, `transcript_missing` | The resource or derived artefact does not exist (yet). |
| `409` | `conflict` | The action conflicts with current state, such as deleting while a job runs. |
| `415` | `unsupported_media` | FFprobe could not validate the media. |
| `422` | `validation_failed` | Pydantic validation or domain validation failed. The message includes the first field location. |
| `503` | `dependency_missing`, `provider_unavailable` | FFmpeg or an optional provider is unavailable. |
| `507` | `insufficient_disk` | The data volume is below the required reserve. |
| `500` | `internal_error` | Unexpected failure; inspect the worker log. |

Validation errors report only the **first** field error, with its location joined by dots.

Job failures are not HTTP errors on the enqueue call. They are recorded on the job as `state: "failed"` plus `error_code` and `error_message`.

---

## Job model

Long-running work is queued and executed by one consumer thread. HTTP requests return a job; clients poll it.

```json
{
  "id": "job_…",
  "project_id": "proj_…",
  "type": "analysis",
  "state": "running",
  "stage": "transcribe",
  "progress": 42.5,
  "message": "Transcribing with small (CPU)",
  "error_code": null,
  "error_message": null,
  "attempts": 1,
  "payload": {},
  "result": {},
  "metrics": {},
  "cancel_requested": false,
  "created_at": "2026-11-19T10:00:00Z",
  "started_at": "2026-11-19T10:00:01Z",
  "finished_at": null
}
```

| Field | Notes |
| --- | --- |
| `type` | `analysis`, `render` or `url_import`. |
| `state` | `queued`, `running`, `waiting`, `completed`, `failed` or `cancelled`. `waiting` means a transient failure parked the job until `next_attempt_at`. |
| `stage` | Human-readable stage. Analysis stages are `prepare`, `audio`, `transcribe`, `candidates`, `rank`, `boundaries`, `done`. Render stages are `assets`, `compose`, `rendering`, `validating`, `done`; URL import uses `validating` and other import stages. |
| `progress` | `0`–`100`. Advisory, not a time estimate. |
| `cancel_requested` | Set when cancellation is requested; the handler checks it between stages and during subprocesses. |
| `payload` / `result` | Type-specific. Treat as opaque unless a route documents them. |

Rules:

- FIFO, one job at a time.
- Enqueueing a job of the same `project_id` + `type` while one is queued, running or waiting returns the active job with `"deduplicated": true` instead of creating another.
- A transient failure a handler marks as retryable parks the job as `waiting` until `next_attempt_at` instead of ending it. The worker retries it by itself, with the wait growing from a minute to half an hour; after six parked attempts it fails normally.
- A download interrupted by a worker restart is requeued and resumes from its partial file. Other interrupted job types are marked `failed` with `error_code: "worker_restarted"`, because their partial output cannot be trusted.
- Retry requeues a terminal or waiting job. Retrying a queued or running job returns `409 conflict`.
- Cancelling a finished job returns `409 conflict`.

---

## Response conventions

There is no pagination layer. Collection routes return all rows relevant to the resource. The project jobs route returns the 25 most recent jobs; the project detail response includes the 10 most recent.

| Shape | Example |
| --- | --- |
| Single resource | `{"project": {...}}`, `{"job": {...}}`, `{"render": {...}}` |
| Collection | `{"projects": [...]}`, `{"candidates": [...]}`, `{"renders": [...]}` |
| Composite | `GET /api/projects/{id}` returns project, source, transcript summary, latest analysis, candidates, renders and recent jobs |
| Binary | Renders and media return bytes, not JSON |

Timestamps are UTC ISO-8601 strings. Durations and timestamps in media logic are integer milliseconds. Scores and percentages such as `crop_x_pct` and `position_pct` are `0`–`100`. Render `Rect` fields and `fit_margin_pct` are normalised `0`–`1` fractions.

---

## System routes

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Public. Status, version and uptime. |
| `GET` | `/api/capabilities` | Live probe of FFmpeg, FFprobe, hardware encoders, transcription, diarisation, LLM, storage, limits and download/compression options. |
| `GET` | `/api/platforms` | Platform constraints and caption safe areas. |
| `GET` | `/api/platforms/{platform}/safe-area?aspect=9:16` | Safe area for one platform and aspect. |

`/api/capabilities` reports what is actually available on this host. In particular:

- `media.hwaccel.encoders` lists encoders FFmpeg reports; `acceleration_for_transcription` is always `false` in this release.
- `transcription.available` requires both the provider and FFmpeg.
- `diarisation.available` is `false`; the detail says speaker separation is not implemented.
- `storage` describes the active backend and free space.
- `compression.presets` is the server's labelled size estimate, not a guarantee.

---

## Projects

Prefix: `/api/projects`

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/projects` | `{name, goals?, profile_id?, brief?}` | `{project}` |
| `GET` | `/api/projects` | — | `{projects}` |
| `GET` | `/api/projects/{project_id}` | — | `{project, source, transcript, analysis, candidates, renders, jobs}` |
| `PATCH` | `/api/projects/{project_id}` | `{name?, goals?}` | `{project}` |
| `DELETE` | `/api/projects/{project_id}` | — | `{deleted: true, project_id}` |

Notes:

- `name` is 1–120 characters and trimmed.
- `goals` is an opaque object to the worker; the UI stores the analysis controls there.
- `brief` is validated against the `ClippingBrief` model at creation.
- `profile_id` must exist and must not be archived.
- Delete removes database rows, generated media and the project workspace. It returns `409 conflict` while an analysis or render job is active.
- The project detail response deliberately contains no filesystem paths.

---

## Sources, media and transcript

Prefix: `/api/projects`

### Import

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/{project_id}/sources/upload` | `multipart/form-data` with `file` | `{source}` |
| `POST` | `/{project_id}/sources/url/inspect` | `{url, quality?, processing?, proxy_height?}` | rendition metadata and estimate |
| `POST` | `/{project_id}/sources/url` | `{url, rights_confirmed, quality?, processing?, proxy_height?}` | `{job, deduplicated}` |

URL import requires `rights_confirmed: true`; otherwise it returns `422 rights_not_confirmed`. The import is a `url_import` job, not a synchronous download.

`quality` accepts `lowest`, `144p`, `240p`, `360p`, `480p`, `720p`, `1080p`, `1440p`, `2160p`, `best` or `audio`. `processing` accepts `fast`, `balanced`, `high` or `custom`. `proxy_height` is valid for custom processing.

Inspection reads metadata only and reports what the chosen quality would fetch.

### Media

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/{project_id}/media` | Source facts and media assets |
| `GET` | `/{project_id}/media/stream` | The source file with Range support |
| `GET` | `/{project_id}/media/proxy/stream` | The analysis proxy with Range support |
| `GET` | `/{project_id}/media/waveform` | Waveform PNG |
| `GET` | `/{project_id}/transcript` | `{transcript}` with segments and words |

Media responses are binary. Range requests are supported so the editor can seek without downloading the whole file. Exports always read the retained source, never the proxy.

### Transcript shape

```json
{
  "transcript": {
    "id": "tr_…",
    "language": "en",
    "language_probability": 0.98,
    "model": "small",
    "duration_ms": 3600000,
    "created_at": "2026-11-19T10:05:00Z",
    "segments": [
      {
        "start_ms": 0,
        "end_ms": 4200,
        "text": "…",
        "words": [{"start_ms": 0, "end_ms": 320, "word": "…", "probability": 0.94}]
      }
    ]
  }
}
```

If no transcript exists: `404 transcript_missing`.

---

## Analysis and candidates

Prefix: `/api` for analysis and candidates; `/api/projects` for the project-scoped routes below.

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/projects/{project_id}/analysis` | `AnalysisRequest` | `{job, deduplicated}` |
| `POST` | `/api/projects/{project_id}/scene-inspect` | `{start_ms, end_ms, height?}` | Re-sampled scene evidence |
| `GET` | `/api/projects/{project_id}/candidates` | — | `{candidates, analysis_run}` |
| `GET` | `/api/projects/{project_id}/candidates/{candidate_id}` | — | `{candidate}` |
| `PATCH` | `/api/projects/{project_id}/candidates/{candidate_id}` | `CandidatePatch` | `{candidate}` |
| `POST` | `/api/projects/{project_id}/candidates/{candidate_id}/boundary` | `{action, end_ms?}` | `{candidate}` |
| `POST` | `/api/projects/{project_id}/candidates/{candidate_id}/editorial` | `EditorialAction` | `{candidate}` |

### AnalysisRequest

```json
{
  "goal": "best_moments",
  "clip_count": 8,
  "duration_band": "auto",
  "quality": "balanced",
  "model": null,
  "language": null,
  "vad": true,
  "use_llm": false,
  "llm_consent": false,
  "weights": null,
  "instructions": null,
  "brief": null
}
```

- `goal`: `best_moments`, `social_clips`, `educational`, `funny`, `debate`, `emotional_stories`, `tutorials`, `highlights`, `condensed` or `custom`.
- `clip_count`: 1–20.
- `duration_band`: legacy field (`auto`, `short`, `medium`, `long`). The current UI sends a duration preset inside `brief`; both are accepted.
- `quality`: `fast`, `balanced` or `best`.
- `use_llm` is effective only when the provider is enabled/configured and `llm_consent` is true.
- `weights` overrides scoring component weights; unknown keys are ignored and the result is normalised.
- `brief` carries the editorial brief: duration preset, strategy, pacing, hook and related preferences. It is validated and persisted.

Starting analysis updates the project status and goals, then enqueues an `analysis` job. The transcript is reused when its fingerprint matches the source content hash, model, compute type, language and VAD setting.

### Candidate edits

```json
{
  "start_ms": 12000,
  "end_ms": 74000,
  "decision": "accepted",
  "title": "The real reason margins collapsed",
  "summary": "…",
  "hook": {
    "text": "…",
    "start_ms": 12000,
    "end_ms": 16000,
    "approved": true,
    "placement": "before",
    "source": "candidate"
  },
  "clear_hook": false,
  "reset_edits": false
}
```

All fields are optional. `reset_edits: true` clears user boundaries, hook and related edits for that candidate. `clear_hook: true` removes the hook. Start/end must remain a valid span inside the source, and the renderer enforces a two-second minimum.

### Boundary review

```json
{"action": "accept", "end_ms": 73500}
```

- `accept` with `end_ms` writes that out point and marks the review accepted.
- `reject` keeps the current out point and marks the review rejected.
- `reset` returns the review to pending.

### Editorial re-evaluation

```json
{
  "strategy": "cold_open",
  "pacing_profile": "energetic",
  "apply": true
}
```

Valid strategies: `automatic`, `chronological`, `cold_open`, `in_medias_res`, `result_first`, `question_answer`, `story_escalation`, `reaction_led`, `manual`. Pacing: `calm`, `conversational`, `balanced`, `energetic`, `fast`.

This re-plans one candidate without re-transcribing and preserves manual trims, captions, branding and composition overrides.

### Score payload

Candidates expose `overall` (0–100) and a `scores` object:

```json
{
  "overall": 78.4,
  "method": "baseline",
  "method_version": "…",
  "components": {
    "hook": {"score": 82.0, "detail": "…"},
    "completeness": {"score": 74.0, "detail": "…"}
  },
  "weights": {"hook": 0.16, "completeness": 0.12},
  "note": "Estimated editorial quality from transcript features, not a prediction of engagement."
}
```

Component keys include hook, quotability, completeness, coherence, density, emotion, novelty, context, boundary and risk, depending on what the feature extractor emits. Treat absent keys as not applicable, not zero.

---

## Jobs

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/api/jobs/{job_id}` | `{job}` |
| `POST` | `/api/jobs/{job_id}/cancel` | `{job}` |
| `POST` | `/api/jobs/{job_id}/retry` | `{job}` |
| `GET` | `/api/projects/{project_id}/jobs` | `{jobs}` (recent jobs, capped) |

Poll every 1–2 seconds. Cancellation is cooperative: a running FFmpeg/ffprobe subprocess is terminated, and the handler checks between stages. A cancel request on a finished job is a `409 conflict`.

---

## Project settings, briefs and corrections

Prefix: `/api/projects`

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `GET` | `/{project_id}/settings` | — | profile, overrides, resolved settings, provenance, brief, profiles |
| `PATCH` | `/{project_id}/settings` | `{profile_id?, overrides?}` | `{project, resolved, provenance}` |
| `GET` | `/{project_id}/brief` | — | current brief, source, history, version |
| `PUT` | `/{project_id}/brief` | `{brief}` | validated brief and version |
| `GET` | `/{project_id}/corrections` | — | `{corrections}` |
| `PATCH` | `/{project_id}/corrections/{word_index}` | `{text?, start_ms?, end_ms?}` | `{correction}` |
| `DELETE` | `/{project_id}/corrections?word_index=` | — | `{removed}` |
| `POST` | `/{project_id}/edit-specs` | `{spec, candidate_id?}` | `{edit_spec}` |
| `GET` | `/{project_id}/edit-specs` | — | `{edit_specs}` |
| `GET` | `/{project_id}/evaluations` | — | `{evaluations}` |

`overrides` accepts only `export`, `captions`, `audio`, `branding`, `brief`, `rules` and `packaging` sections. A merged document is validated; unknown sections or invalid values return `422 invalid_overrides`.

Effective render settings resolve in this order: project overrides over profile document over built-in defaults. `provenance` reports where each section came from.

Transcript corrections are stored separately from the immutable transcript. They affect caption text and timing where the renderer resolves word text; the analysis transcript is not rewritten.

---

## Renders

Prefix: `/api`

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/projects/{project_id}/renders/estimate` | `{aspect, resolution, resolution_mode?, custom_width?, custom_height?, fps?, duration_ms, compression?, enhancement?}` | size estimate and effective encoded dimensions |
| `POST` | `/api/projects/{project_id}/renders` | `RenderRequest` | `{render, job}` |
| `POST` | `/api/projects/{project_id}/renders/composition-preview` | `RenderRequest` + `preview_at_ms?` | one composed JPEG frame |
| `POST` | `/api/projects/{project_id}/renders/quality-preview` | `RenderRequest` + `preview_at_ms?` | `{original, enhanced, source, target, applied_model, …}` at the same timestamp and composition |
| `GET` | `/api/projects/{project_id}/renders` | — | `{renders}` |
| `GET` | `/api/renders/{render_id}` | — | `{render}` |
| `GET` | `/api/renders/{render_id}/download` | — | MP4 attachment |
| `GET` | `/api/renders/{render_id}/stream` | — | MP4 with Range support |
| `GET` | `/api/renders/{render_id}/draft` | — | Preserved draft MP4 when the gate blocked |
| `GET` | `/api/renders/{render_id}/subtitles` | — | SRT sidecar, if one was produced |

### RenderRequest

```json
{
  "candidate_id": "cand_…",
  "segments": null,
  "aspect": "9:16",
  "resolution": 1080,
  "resolution_mode": "class",
  "custom_width": null,
  "custom_height": null,
  "enhancement": {
    "profile": "standard",
    "model_id": "auto",
    "denoise": 0,
    "restoration": 1.0,
    "allow_fallback": true,
    "temporal_stabilize": false,
    "compare_first": false
  },
  "framing": "crop",
  "composition": "auto",
  "crop_x_pct": 50,
  "crop_y_pct": 50,
  "caption_preset": "clean",
  "captions_enabled": true,
  "captions": {
    "font_family": "Montserrat",
    "position": "lower",
    "position_pct": 82,
    "alignment": "center",
    "margin_v": 150
  },
  "captions_burn_in": true,
  "captions_sidecar": false,
  "audio": null,
  "logo": null,
  "include_hook": true,
  "apply_editorial": true,
  "approve_editorial": false,
  "normalize_audio": true,
  "override_policy": false,
  "fps": 30,
  "title": "…",
  "edit_spec_id": null,
  "compression": {"preset": "balanced"}
}
```

- Provide **either** `candidate_id` **or** explicit `segments`. Both missing is a `422`.
- `segments` is a list of `{start_ms, end_ms, role?}`; 1–40 spans, total length 2 seconds to 15 minutes. Roles are `hook`, `context`, `main`, `development`, `payoff` or `loop`. The render route ignores a `composition` key inside a segment; per-segment composition requires a saved edit spec.
- The request model also declares `composition_overrides`, but the current render route does not apply it. Do not build a client against it.
- `aspect`: `9:16`, `16:9`, `1:1`. `resolution`: `720`, `1080`, `1440` or `2160`.
- `resolution_mode`: `class` uses the aspect ladder; `source` preserves the source short side and is returned as concrete `custom_dimensions`; `custom` requires `custom_width`/`custom_height` that match the aspect within 1% and are even. A mismatched custom size returns `422 invalid_resolution` rather than stretching.
- `enhancement.profile`: `standard`, `clean`, `ai` or `ai_plus`. `model_id` is `auto`, `espcn-x2`, `espcn-x4` or `realesrgan-x2`. `denoise` is 0–100, `restoration` is 0–1. `allow_fallback` defaults true; when it is false and the model cannot run, the job fails with `enhancement_model_unavailable`. The render result and gate report `applied_model`; it is `null` when no AI model ran.
- `composition`: `auto`, `crop`, `fit`, `pip`, `split` or `manual`. `split` degrades to `fit` for one source and records a warning.
- `framing` is the legacy global switch; `composition` wins when both are present.
- `captions` accepts only the fields the client sends; omitted fields inherit the resolved profile/preset geometry.
- `audio` and `logo` override the profile's audio and branding sections. Logo assets must already be uploaded to a profile.
- `approve_editorial` must be true to apply a reordered editorial plan. Otherwise the render falls back to source order and says so in its review.
- `compression`: `{"preset": "smallest" | "balanced" | "higher"}` or `{"preset": "custom", "crf": 23, "audio_bitrate_kbps": 128, "resolution_cap": 1080, "max_fps": 30, "x264_preset": "veryfast"}`.
- `title` affects the download filename.

### Render record and review

```json
{
  "render": {
    "id": "rnd_…",
    "status": "completed",
    "duration_ms": 74200,
    "width": 1080,
    "height": 1920,
    "review": {
      "version": 1,
      "status": "warn",
      "blocking": false,
      "checks": [
        {"id": "dimensions", "status": "pass", "detail": "1080x1920; expected 1080x1920."},
        {"id": "caption_bounds", "status": "warn", "detail": "…"}
      ],
      "summary": "…"
    }
  }
}
```

`review.status` is `pass`, `warn` or `fail`. A blocking failure keeps the encoded file as a draft; `GET /renders/{id}/draft` returns it, while `download` and `stream` require a completed render.

The gate measures the actual output with FFprobe/FFmpeg. It does not infer success from the filters in the command line. Checks include streams, dimensions, source resolution, enhancement profile and applied model, frame rate, reported frame count, duration, editorial duration target, full decode, audio track, A/V sync, black frames, freezes, caption bounds, composition warnings, logo overlays and ending review.

### Enhancement models

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/enhancement/models` | — | installed models, licences, sizes, benchmarks and runtime devices |
| `POST` | `/api/enhancement/models/{model_id}/install` | — | `{model, installed, job}`; queues a verified weight download only |
| `DELETE` | `/api/enhancement/models/{model_id}` | — | `{model, installed: false}` for an optional model |
| `GET` | `/api/enhancement/benchmarks` | — | measured seconds per input megapixel per model |

The bundled ESPCN models need no download. `realesrgan-x2` is 67 MB and is
installed only after this explicit call. The download streams to a `.part` file,
checks the pinned SHA-256, then moves it into `data/models/enhancement/`.
No user media is sent to any service by these routes.

`GET /api/capabilities` includes the same model list under `enhancement`, with
`profiles`, `devices` (`providers`, `gpu_inference`, `opencv_dnn_superres`),
`gpu_required: false` and `frame_interpolation: false`.

---

## Packaging

Prefix: `/api/projects`

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `GET` | `/{project_id}/candidates/{candidate_id}/packaging` | — | `{candidate, packaging}` |
| `POST` | `/{project_id}/candidates/{candidate_id}/packaging` | `PackagingGenerateBody` | `{candidate, packaging}` |
| `PATCH` | `/{project_id}/candidates/{candidate_id}/packaging` | `PackagingPatchBody` | `{candidate, packaging}` |
| `POST` | `/{project_id}/candidates/{candidate_id}/packaging/trend-check` | `TrendCheckBody` | `{candidate, packaging}` |
| `GET` | `/{project_id}/packaging/config` | — | platform limits and packaging settings |

### Generate example

```json
{
  "platform": "youtube_shorts",
  "use_llm": false,
  "check_trend": false,
  "fields": ["titles", "description"],
  "title_angle": "contrarian",
  "more_titles": 1,
  "research_mode": "off",
  "reuse_brief": true,
  "edited_fields": ["hashtags"],
  "force_fields": []
}
```

- `fields` limits regeneration to those fields. `edited_fields` protects human-approved fields. `force_fields` explicitly allows overwriting them.
- `research_mode`: `off`, `free` or `enhanced`. Providers that are unconfigured are reported as unavailable; no metrics are invented.
- `use_llm` requires a configured provider. When enabled, the brief and clip transcript are sent to that provider; without a provider, the deterministic generator runs. The field defaults to `true` in the model, so a client that wants a strictly local run must send `false`.
- Generation uses the whole-transcript content brief by default (`reuse_brief: true`), not only the clip excerpt.

### Patch example

```json
{
  "metadata": {"titles": ["…"], "hashtags": [{"tag": "#example", "source": "manual"}]},
  "edited_fields": ["titles", "hashtags"],
  "trend_action": "use",
  "trend_id": "trend_…",
  "regenerate_with_trend": true
}
```

`trend_action` is `use`, `dismiss`, `restore`, `save` or `manual`. Manual trends are stored as references without invented volume or engagement values.

Packaging is a draft for a human. There is no publishing integration.

---

## Profiles and assets

Prefix: `/api`

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `GET` | `/profiles?include_archived=` | — | `{profiles}` |
| `POST` | `/profiles` | `{document}` | `{profile}` |
| `GET` | `/profiles/{profile_id}` | — | `{profile}` |
| `PATCH` | `/profiles/{profile_id}` | `{document}` | `{profile}` |
| `POST` | `/profiles/{profile_id}/duplicate?name=` | — | `{profile}` |
| `POST` | `/profiles/{profile_id}/archive?archived=` | — | `{profile}` |
| `DELETE` | `/profiles/{profile_id}` | — | `{deleted, profile_id}` |
| `GET` | `/profiles/{profile_id}/assets?kind=` | — | `{assets}` |
| `POST` | `/profiles/{profile_id}/assets` | multipart: `kind`, `licence_confirmed`, `file` | `{asset}` |
| `PATCH` | `/assets/{asset_id}?licence_confirmed=` | query only | `{asset}` |
| `GET` | `/assets/{asset_id}/file` | — | binary asset |
| `DELETE` | `/assets/{asset_id}` | — | `{deleted, asset_id}` |
| `GET` | `/caption-presets` | — | `{presets}` |
| `POST` | `/caption-presets` | `{name, config}` | `{preset}` |
| `DELETE` | `/caption-presets/{preset_id}` | — | `{deleted, preset_id}` |

Asset rules:

- `kind` is `logo` or `music`.
- The shared upload read caps assets at 12 MB before the music branch's 40 MB check, so a 13 MB music file is rejected. Keep brand assets small.
- Logos are inspected as images. SVG logos are stored but are not renderable by FFmpeg; a render using one is rejected unless `override_policy` is set. Use a transparent PNG.
- `licence_confirmed` is stored with the asset. When a music asset is used to replace audio, the render route refuses the asset unless its licence is confirmed.
- Deleting a profile deletes its stored assets.

The profile document validates sections for branding, captions, audio, export, brief, rules and packaging. Use `GET /api/projects/{id}/settings` to see the resolved result and provenance for a project.

---

## Library, evaluations and datasets

Prefix: `/api`

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `GET` | `/library/connectors` | — | connector availability |
| `POST` | `/library/search` | `{platform, query, max_results}` | `{results, platform, query}` |
| `GET` | `/library/sources?query=&platform=` | — | `{sources}` |
| `POST` | `/library/sources` | `{url, platform?, title?, creator?}` | `{source, rights}` |
| `GET` | `/library/sources/{source_id}` | — | source, rights, imports |
| `PATCH` | `/library/sources/{source_id}` | mutable metadata | `{source}` |
| `DELETE` | `/library/sources/{source_id}` | — | `{deleted, source_id}` |
| `POST` | `/library/sources/{source_id}/check` | — | source, availability, note |
| `GET` | `/library/sources/{source_id}/rights` | — | `{rights}` |
| `PUT` | `/library/sources/{source_id}/rights` | rights document | `{rights}` |
| `POST` | `/library/sources/{source_id}/revoke?remove_media=` | — | source, rights, removed dataset items |
| `POST` | `/library/sources/{source_id}/import` | import body | `{project, job, analysis_job, rights}` |
| `GET` | `/library/sources/{source_id}/dataset-items` | — | `{items}` |
| `POST` | `/projects/{project_id}/evaluations` | `{candidate_id?, render_id?, decision, ratings?, rejection_reason?, notes?}` | `{evaluation, dataset_note}` |
| `GET` | `/evaluations` | — | `{evaluations}` |
| `GET` | `/datasets` | — | `{datasets}` |
| `POST` | `/datasets` | `{name, filters?, train_ratio?, seed?}` | `{dataset, counts, excluded}` |
| `GET` | `/datasets/{dataset_id}` | — | `{dataset}` |
| `POST` | `/datasets/{dataset_id}/freeze` | — | `{dataset}` |
| `DELETE` | `/datasets/{dataset_id}` | — | `{deleted, dataset_id}` |

Design rules:

- Connectors use official APIs only. Unconfigured providers are unavailable rather than simulated.
- Search results are candidates, not imports. A library source records provenance and rights separately from a project.
- Revoking rights can remove derived dataset items; this is deliberate, so training data does not outlive permission.
- Evaluation decisions are `accepted`, `rejected` or `needs_revision`; ratings are integers from 1 to 5.
- Dataset export groups evaluations by source, honours rights, and records exclusions. It is not a claim that the data is sufficient to train a model.

There is no UI for these routes in this version.

---

## End-to-end example

The example assumes a running worker and a token. Replace IDs with values from responses.

```bash
BASE=http://127.0.0.1:8788
TOKEN=$(cat data/worker-token)   # omit in Docker; use the value from .env

# 1. Health is public
curl "$BASE/api/health"

# 2. Create a project
curl -X POST "$BASE/api/projects" \
  -H "x-clipforge-token: $TOKEN" \
  -H "content-type: application/json" \
  -d '{"name":"Podcast - episode 42"}'

# 3. Upload a source
curl -X POST "$BASE/api/projects/PROJECT_ID/sources/upload" \
  -H "x-clipforge-token: $TOKEN" \
  -F "file=@episode-42.mp4"

# 4. Start analysis (returns a job)
curl -X POST "$BASE/api/projects/PROJECT_ID/analysis" \
  -H "x-clipforge-token: $TOKEN" \
  -H "content-type: application/json" \
  -d '{
    "goal":"best_moments",
    "clip_count":8,
    "quality":"balanced",
    "vad":true,
    "brief":{
      "clip_count":8,
      "duration_preset":"extended",
      "editorial":{"preferred_strategy":"automatic","hook_preference":"auto"},
      "pacing":{"profile":"balanced"},
      "hook":{"enabled":true,"auto_select":true}
    }
  }'

# 5. Poll until state is terminal
curl "$BASE/api/jobs/JOB_ID" -H "x-clipforge-token: $TOKEN"

# 6. Read candidates
curl "$BASE/api/projects/PROJECT_ID/candidates" -H "x-clipforge-token: $TOKEN"

# 7. Render a candidate
curl -X POST "$BASE/api/projects/PROJECT_ID/renders" \
  -H "x-clipforge-token: $TOKEN" \
  -H "content-type: application/json" \
  -d '{
    "candidate_id":"CANDIDATE_ID",
    "aspect":"9:16",
    "resolution":1080,
    "composition":"auto",
    "caption_preset":"clean",
    "captions_enabled":true,
    "normalize_audio":true,
    "compression":{"preset":"balanced"},
    "approve_editorial":true
  }'

# 8. Poll the render job, then download
curl -L "$BASE/api/renders/RENDER_ID/download" \
  -H "x-clipforge-token: $TOKEN" \
  -o clip.mp4
```

The brief shape above is illustrative; `PUT /api/projects/{id}/brief` validates the full document and returns a field-level error if a key is wrong. Fetch `GET /api/projects/{id}/brief` first if you want the exact persisted shape.

---

## Contract tests

Python models and TypeScript Zod schemas are intended to agree. `apps/worker/tests/test_contract.py` checks both sides against fixtures in `packages/shared/fixtures/`. When a request or response changes, update the shared schema, the fixture and the contract test together.

There is no API version prefix. The project is pre-1.0; treat breaking changes as possible and pin a commit if you build an external client.
