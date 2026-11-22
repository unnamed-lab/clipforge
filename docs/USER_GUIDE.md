# User guide

Clipforge turns a long recording into short, self-contained clips. This guide follows the workflow in the web app and notes where a capability exists only in the worker API.

For installation, see the [README quick start](../README.md#quick-start). For routes and payloads, see the [API reference](API.md).

## Contents

1. [Before you start](#1-before-you-start)
2. [Import a recording](#2-import-a-recording)
3. [Run the analysis](#3-run-the-analysis)
4. [Review candidates](#4-review-candidates)
5. [Edit the clip](#5-edit-the-clip)
6. [Package the clip](#6-package-the-clip)
7. [Export](#7-export)
8. [Templates](#8-templates)
9. [Diagnostics](#9-diagnostics)
10. [Project management](#10-project-management)
11. [Troubleshooting](#11-troubleshooting)
12. [UI coverage and API-only features](#12-ui-coverage-and-api-only-features)

---

## 1. Before you start

Both processes must be running:

```bash
pnpm worker:dev   # http://127.0.0.1:8788
pnpm dev          # http://localhost:3000
```

Open <http://localhost:3000>. If the worker is unreachable, the app reports it; `/settings` shows the live capability report. Confirm that FFmpeg and the transcription provider are available before importing a long file.

The first analysis downloads the configured Whisper model (host default `small`). Model download time is not transcription time; both count against the first run.

---

## 2. Import a recording

From the home page choose **Import a video**, or open `/new`.

1. Give the project a name.
2. Choose a local file or paste a URL.
3. For a URL, confirm that you have the rights to reuse the source. Import is refused without that confirmation.
4. Optionally inspect a URL before downloading. Inspection reports the published renditions and an approximate size for the selected quality; it reads metadata only.

### URL import options

| Option | Meaning |
| --- | --- |
| Quality | `lowest`, `144p` … `2160p`, `best`, or `audio`. This is what the importer fetches, independent of the export frame. |
| Processing | `fast`, `balanced`, `high` or `custom`. Controls the analysis proxy resolution and scene sampling rate. |
| Proxy height | Custom processing only; the analysis proxy height, within the worker's allowed range. |

Import runs as a background job. The project shows the job state and progress, and the source facts appear when inspection finishes.

### What validation happens

The importer trusts FFprobe, not the file extension. It rejects unreadable media, enforces the upload and duration limits, checks free disk space, and applies URL safety checks before `yt-dlp` runs. A source without audio can be stored, but analysis is disabled because candidate discovery is transcript-driven.

### Replacing a source

**Replace source** imports a new file into the same project and clears derived analysis state. Generated media for the old source is cleaned up. The project keeps its name and settings.

---

## 3. Run the analysis

The analysis panel appears once a source with audio exists and no candidates are present. It also appears behind **Not finding the right moments?** for a re-run.

### Goal

The goal changes the weights of the explainable scoring components:

| Goal | Bias |
| --- | --- |
| Best moments | Balanced across the recording. |
| Short social clips | Strong opening, quotable, self-contained. |
| Educational highlights | Explanation density and complete reasoning. |
| Funny and memorable | Laughter, surprise, lines that land out of context. |
| Strong opinions | Disagreement and contestable claims. |
| Emotional stories | Personal anecdotes with a beginning and an end. |
| Tutorials and takeaways | Practical steps that stand alone. |

### Other analysis controls

- **Clip count** — 1 to 20 candidates.
- **Editorial target length** — Short (15–30 s), Standard (30–60 s), Extended (60–90 s), or Custom with a validated minimum and maximum. These are editorial targets, not platform limits. Candidates are never padded or truncated to fit.
- **Quality profile** — Fast (`base`), Balanced (default model), Best (`medium`). On CPU, `medium` is significantly slower.
- **Editorial direction** — style, strategy, genre, pacing, flashback preference, hook preference, pattern-interrupt intensity, reaction-shot preference and loop-ending preference.
- **Optional LLM rescoring** — off by default. Enabling it requires a configured OpenAI-compatible provider *and* consent for the run. Transcript text is sent only under those conditions.

The analysis is a background job with stages: prepare, audio, transcribe, candidates, rank, boundaries, done. Re-running with only goal or length changes reuses the stored transcript instead of re-transcribing.

### What the pipeline does

1. Probe the retained source and choose an analysis proxy.
2. Extract 16 kHz mono audio.
3. Transcribe with word timestamps and optional VAD.
4. Build sentence and word models from timings.
5. Generate boundary-aware candidate windows.
6. Extract transcript features and score them with the goal's weights.
7. Optionally rescore the shortlist with the configured LLM.
8. Remove near-duplicates.
9. Review clip endings for trailing shots.
10. Build the duration, hook and editorial plan.

The score is an **estimated editorial quality from transcript features**, not a prediction of views, retention or revenue.

---

## 3.1 Produce clips automatically

With a source imported, the **Automatic production** panel runs the whole project:

1. Reuses or creates the transcript and candidate moments.
2. Lets the editorial director choose clips and strategies, or uses the deterministic planner when no model is configured.
3. Configures each clip from the project/client-profile defaults. **Options** exposes only clip count and frame; everything else stays automatic unless you override it later.
4. Renders each clip, checks the actual file and repairs supported defects within a bounded loop.
5. Lists each clip with its findings, repair history, review status and current export version.

Controls:

- **Cancel** stops the run between clips and inside FFmpeg. Completed stages and valid exports are kept.
- **Resume** continues a failed or interrupted run from its last completed stage. It does not redo transcription when the transcript is still valid.
- **Approve** records your decision on a clip that still needs review. It is only available for a completed render whose technical gate passed. Approving also copies the file to the project export folder.
- **Remove** deletes a clip from the production list without deleting its export versions. Delete a specific export from **Export versions** if you also want the app-stored file gone. A copy already written to the export folder is left alone.
- **Save all** copies every current export to the export folder. **Save** on one version does the same for that file.
- **Export folder** is configurable in **Options**. The default is `Documents/Clipforge/<project>/`; relative paths and system folders are refused.
- **Produce again** starts a new run. Identical edits reuse the current export instead of duplicating it; a changed edit creates a new version.
- **Preview/MP4** use the same stored render that passed review.

Progress is reported by the worker, stage by stage. The panel never shows a success state before the final technical checks pass. A model-generated review note is labelled as such and never treated as a virality or audience score.

---

## 4. Review candidates

The candidate rail lists every proposal, ordered by rank or by position in the source.

- **Score** — 0–100, with the component breakdown available under **Why this score**.
- **Decision** — keep (accept), reject, or restore to proposed. Decisions are stored per candidate.
- **Warnings** — boundary risk, duration fit, low-confidence language, duplicate proximity, and similar evidence-backed notes.
- **Duration** — the effective clip length after trims.

Changing the selected candidate updates the editor, transcript window, packaging panel and export panel.

---

## 5. Edit the clip

The editor is the source of truth for the exported span.

### Preview and trim

- Playback is bounded to the selected clip. Stopping at the out point is intentional.
- The paused player doubles as the trim scrubber: inspect source footage without changing the clip.
- Set in and out points with the sliders, the nudge controls, or **Snap in** / **Snap out** to the nearest transcript boundary.
- **Save trim** writes the user boundaries. **Reset** restores the original proposal.
- A render needs at least two seconds of footage.

Manual trims win over generated editorial timing. The renderer exports the cut shown in the editor.

### Ending review

The pipeline checks the transcript and scene cuts around the proposed out point and may flag a possible trailing shot. The panel shows the evidence and alternative endings.

- **Use this ending** writes that out point.
- **Keep current out point** rejects the suggestion and leaves the clip untouched.
- **Reset review** returns the review to pending.

Nothing is trimmed automatically.

### Editorial plan

The plan shows the strategy, rationale, confidence, concerns and a source-to-timeline map. Captions, audio and video all follow the same order.

- A strategy that reorders source spans is marked, and **must be approved** before export. Without approval, the renderer falls back to a source-order cut rather than applying a reorder silently.
- **Re-evaluate** re-plans this candidate under a different strategy or pacing profile. It does not re-transcribe and does not touch manual trims or export settings.
- Manual ordering is an API concept; the workspace exposes strategy selection and approval, not a drag-and-drop timeline editor.

### Transcript and score detail

The transcript window seeks the source when you select a line. Corrections to word text or timings are available through the worker API; the workspace does not currently expose a correction editor.

**Why this score** expands the weighted component breakdown: hook, quotability, completeness, coherence, density, emotion, novelty, context, boundary and risk, as applicable to the goal. The note in the panel is deliberate: these are deterministic transcript signals, not a reach forecast.

---

## 6. Package the clip

Packaging starts from the whole-transcript brief for the source, not only the words inside the clip window. That lets the generator use context that arrives after the hook.

Controls include:

- **Platform** — cross-platform, TikTok, YouTube Shorts or Instagram Reels; this changes metadata limits and guidance.
- **Title angle** — generate against a stated angle or let the director choose.
- **Generate / regenerate** — regenerate one field (titles, description, hashtags, keywords, thumbnail text) without discarding approved edits in the others.
- **More titles** — request additional title candidates.
- **Editing** — all generated fields are editable; add or remove hashtags and keywords manually.
- **Copy** — copies a field, with a clear message if the browser blocks clipboard access.
- **Trend check** — optional; reports unavailable providers honestly and never invents volume or engagement numbers.
- **Manual trend** — add a reference you found yourself; no metric is fabricated.

Titles are scored on an editorial rubric; unsupported claims are rejected before ranking. The output is a publishing draft for a human, not an automatic post.

**Use the configured LLM when available** is on by default. With an LLM provider configured, enabling it sends the source brief and clip transcript to that provider. Turn it off for fully local, deterministic metadata. Without a provider, the deterministic generator runs regardless.

Research providers (YouTube Data API, Twitch, approved trends endpoints) are optional and disabled without credentials. Keyword suggestions from YouTube's suggestion endpoint are opt-in because they use an undocumented public endpoint.

---

## 7. Export

The export panel renders the selected clip from the retained source with FFmpeg.

### Frame and layout

| Setting | Options |
| --- | --- |
| Aspect | 9:16, 16:9, 1:1 |
| Target resolution | Preserve source, 720p, 1080p, 1440p, 4K or custom even dimensions |
| Composition | Auto, Fill/crop, Contain, Picture-in-picture, Split/stacked, Manual |
| Crop focus | X/Y sliders for Fill/crop and Manual |

**Auto** inspects scene changes before encoding. Slides, screen recordings and mixed scenes are contained so information is not cut off; a centred speaker is cropped to keep the subject large. Each scene gets a decision with a reason and, when relevant, a warning. The preview and the final render read the same decisions.

- **Contain** places the whole source over a solid fill and reports when evidence is scaled down enough to risk unreadable text.
- **Picture-in-picture** keeps the whole source over a blurred background.
- **Split/stacked** needs two simultaneous sources; for a single source the worker degrades it to contain with a warning. The option remains selectable in the workspace; read the render warnings.
- **Manual** in the workspace is a global crop with a chosen focus point. Per-scene composition overrides require a saved edit spec through the API; there is no per-scene override editor.

### Video quality

The **Video quality** section is optional. It never changes or deletes the
original source, and it runs locally.

| Profile | What it does |
| --- | --- |
| Standard | Lanczos resizing only. The default. |
| Clean | Conservative `hqdn3d` denoise, then lanczos resizing. No AI. |
| AI Enhance Fast | ESPCN 2×, no denoise or blend, native-size intermediate and a faster encode. May let composition lanczos the result up to the target. |
| AI Enhance | ESPCN/Real-ESRGAN super-resolution at a conservative blend, then resize. |
| AI Enhance+ | Full model strength, static-frame stabilisation, and a warning about altered texture. |

- The panel shows the real source dimensions, the target, the selected engine and
  whether GPU inference is available.
- **Preserve source** keeps the source short side. **1080p** on a 1280×720 source
  asks for 1920×1080; on 720×1280 it asks for 1080×1920. Other aspect ratios are
  fitted/cropped by the composition, never stretched.
- The compression preset can still cap the encoded frame. If it does, the panel
  says the encoded size, and you can choose Higher/Custom to keep 1440p or 4K.
- **Compare a frame** renders two stills from the same source timestamp and the
  same composition decision. Drag the divider. If the enhanced side is worse,
  switch back to Standard and render again.
- The AI runs on CPU when no GPU provider is available. The estimate shown is
  based on a measured benchmark on this machine; if no measurement exists, the
  panel says so instead of inventing one.
- Real-ESRGAN is an optional 67 MB download with an explicit button. Only model
  weights are downloaded; your video is never uploaded.
- A missing model either falls back to conventional resizing (with an explicit
  warning and `applied_model: null`) or fails if you turn fallback off.
- **AI Enhance+ requires a current comparison** before the Render button is
  enabled. Changing the target, profile, blend or composition invalidates it,
  because the evidence then belongs to a different frame.

Upscaling reconstructs pixels. It does not restore native detail that the source
never captured, and it can invent texture, especially at full strength. Captions,
crop, face-cam tracking and audio settings are not changed by the profile.

### Captions

- Burned-in captions are controlled by the **Burn into the video** toggle.
- Presets: clean, large, minimal and branded. A `custom` preset with explicit caption settings is accepted by the API; the workspace does not expose a custom-preset editor.
- Bundled faces: Montserrat (default), Inter, Archivo Black, Bebas Neue, Lato, Anton. Requested proprietary faces are mapped to bundled equivalents.
- Position, position percentage, alignment and vertical margin are adjustable.
- Captions come from stored word timings, so the highlighted word follows the audio.
- The quality gate measures the rendered caption width against the frame and safe area; it does not assume the subtitle filter produced a valid result.

A sidecar SRT can be requested through the API (`captions_sidecar`). The current export panel exposes burn-in only.

### Audio and compression

- **Normalise loudness** applies FFmpeg loudness normalisation.
- Compression presets: Smallest file (CRF 28, up to 720p, 96 kbps), Balanced (CRF 23, up to 1080p, 128 kbps), Higher quality (CRF 18, full resolution, 192 kbps), or Custom CRF / audio bitrate / width / fps.
- Compression is independent of the target resolution. A 720p render can use a high-quality encode, and a 1440p target needs a preset whose cap allows it.

### Render and review

Submitting queues a render job; the panel polls its state. A completed render can be streamed or downloaded.

Before publication the worker runs an export quality gate over the actual file:

- container streams (H.264 video / AAC audio), dimensions, frame rate and reported frame count;
- source resolution support, the enhancement profile/model that actually ran, and actual duration versus the editorial target;
- full decode, audio track presence and A/V sync;
- black-frame and freeze detection;
- caption bounds, composition warnings, logo overlay behaviour and ending review.

A **fail** is blocking. The encoded draft is preserved and can be downloaded for inspection, but it is not advertised as a finished render. A **warn** is reported and the render completes. This distinction is why the render history can show a review even when a file exists.

### Hooks and editorial application

Exports include the approved hook and apply the editorial plan by default. A reordered plan that has not been approved is not applied; the render falls back to source order and says so.

---

## 8. Templates

`/templates` lists recipes: a goal, a duration target, a caption preset and editorial guidance. Recipes are starting points; the analysis panel still shows and stores the actual settings used for a project.

The templates page also previews each caption preset so the export choice is not blind.

---

## 9. Diagnostics

`/settings` is a live capability report, not a static help page. It shows:

- worker version, uptime and reachability;
- FFmpeg and FFprobe availability and versions;
- available hardware encoders (this does **not** mean GPU transcription);
- the transcription provider, device, compute type, default model and model sizes;
- diarisation availability, currently reported as not implemented;
- LLM configuration and whether consent is required;
- storage backend, free space and the disk floor;
- upload and duration limits;
- platform constraints and safe areas.

Use **Refresh diagnostics** after changing `.env`, installing FFmpeg, or switching storage backends.

---

## 10. Project management

- **Re-run analysis** from the project workspace after changing the goal or length. The transcript is reused when its fingerprint matches.
- **Replace source** imports a new recording and clears derived state.
- **Delete permanently** removes the project, its database rows and its generated files. It is disabled while a job is active. This cannot be undone.
- Renders are retained per project. Deleting a render removes its file through the storage backend.

---

## 11. Troubleshooting

| Symptom | Check |
| --- | --- |
| Settings shows the worker offline | Is `pnpm worker:dev` running? Does `curl http://127.0.0.1:8788/api/health` work? Is `CLIPFORGE_WORKER_URL` correct? |
| `401 unauthorized` through the web app | `CLIPFORGE_WORKER_TOKEN` must match between web and worker. In host development, leave it empty and let `data/worker-token` be generated, or set the same value for both. |
| FFmpeg missing or filters unavailable | Install a full FFmpeg build with `libx264`, AAC, `libass`, `subtitles` and `showwavespic`; restart the worker. |
| Analysis never starts | The source must contain audio. Check the source facts and the worker log. |
| Transcription is very slow | CPU inference is expected to be slow. Try `base`, a shorter source, or a machine with more cores. GPU transcription is not supported in this release. |
| Model download fails | The first run needs network access to the model host and enough free disk. Check `/settings` for the disk floor. |
| Render fails with `source_missing` | The source file is not in the project workspace. Re-import it; deleting data outside the app breaks the reference. |
| Export gate fails | Open the review report. A blocking failure keeps the draft; inspect it, adjust trims/composition/captions and retry. |
| URL import fails | Confirm rights, check that the URL is supported, and inspect first. URL import depends on the external site and `yt-dlp`. |
| Disk full | Clear unused projects/renders, raise the data volume, or lower `CLIPFORGE_WHISPER_MODEL`. The reserve floor protects the database and the OS. |

Worker logs are the first place to look for job failures. The UI surfaces the job's `error_code` and `error_message`, but the log carries the stack trace and FFmpeg stderr.

---

## 12. UI coverage and API-only features

The web workspace covers the main path: import, analysis, candidate review, trim, boundary review, editorial approval, packaging, export and diagnostics.

These worker capabilities do not have a dedicated screen in this version:

- reusable profiles (branding, caption, audio and export defaults) and profile assets such as logos or music;
- saved caption presets beyond the built-in set;
- content library connectors, source rights and revocation;
- evaluation records and training datasets;
- explicit multi-segment render specs and per-scene composition overrides;
- transcript word corrections;
- audio modes beyond loudness normalisation (music beds, ducking, fades) and logo overlays;
- sidecar subtitle export from the UI.

They are usable through the API. Do not assume a feature is unsupported merely because the UI does not expose it; check the [API reference](API.md) first.
