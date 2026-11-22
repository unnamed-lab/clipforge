# Clipforge — Processing Pipeline

Every stage below is implemented in `apps/worker/src/clipforge`. Each stage reports real progress; none of them estimate progress from elapsed time.

For the project-level automatic workflow that sequences these stages, reviews the
rendered file and versions exports, see [Automatic production](PROJECT_PIPELINE.md).

## 0. Import

### Local upload

```
POST /projects/{id}/sources/upload   (multipart, streamed to disk)
  → size cap check while streaming (configurable, default 2 GiB)
  → FFprobe content probe (ffprobe -show_format -show_streams -print_format json)
  → allow only container/codec combinations FFmpeg can actually decode
  → require a video stream; audio optional (flagged, not fatal)
  → duration must be > 0 and < cap (default 6 h)
  → write sources row, copy nothing (file already in the project workspace)
```

The upload target path is `data/projects/<uuid>/source/<uuid>.<ext-from-probe-container>`.
The extension comes from the probed container, never from the client filename.

### URL import

```
POST /projects/{id}/sources/url {url}
  → scheme allowlist (http, https)
  → resolve host: every A/AAAA must be public (no private, loopback, link-local, CGNAT,
    multicast, reserved, or cloud-metadata addresses)
  → yt-dlp -J (single item, no playlist) to read title/duration/formats — 45 s timeout
  → reject duration > cap, reject live streams
  → yt-dlp download with --max-filesize, --no-playlist, progress hook, --max-downloads 1
  → re-probe the downloaded file exactly like an upload
```

The UI requires a rights confirmation before this call. yt-dlp does not bypass DRM, logins or paywalls; when extraction fails the API returns `url_unsupported` with yt-dlp's reason, not a generic "failed".

## 1. Media inspection

`ffprobe -v error -print_format json -show_format -show_streams <path>` normalised into:

```jsonc
{
  "duration_ms": 3_721_000,
  "width": 1920, "height": 1080, "fps": 29.97,
  "video_codec": "h264", "audio_codec": "aac", "audio_channels": 2,
  "has_audio": true, "size_bytes": 812_000_000, "container": "mp4"
}
```

Rotation metadata is honoured (display dimensions, not coded dimensions) so portrait phone footage is not reported sideways.

## 2. Audio extraction

```
ffmpeg -nostdin -hide_banner -loglevel error
       -i <source> -vn -sn -dn
       -ac 1 -ar 16000 -c:a pcm_s16le
       -y <project>/audio/audio.wav
```

16 kHz mono PCM is what Whisper wants; no resampling happens inside the model.

## 3. Transcription

Primary provider: **faster-whisper**, CPU, `compute_type=int8`.

| Setting | Default | Notes |
| --- | --- | --- |
| model | `small` | `tiny`/`base`/`small`/`medium` selectable; download is on first use |
| language | auto-detect | or forced |
| VAD | Silero VAD v5, threshold 0.5, min silence 400 ms | removes music/noise spans |
| word timestamps | on | required for captions and sentence boundaries |
| beam size | 5 (`fast` profile: 1) | |

Output, persisted as one JSON document:

```jsonc
{
  "language": "en", "language_probability": 0.98, "model": "small",
  "duration_ms": 3721000,
  "segments": [
    { "id": 0, "start_ms": 4200, "end_ms": 9100,
      "text": " Most people think hiring is about...",
      "avg_logprob": -0.31, "no_speech_prob": 0.02,
      "words": [ { "start_ms": 4200, "end_ms": 4480, "word": " Most", "probability": 0.91 } ] }
  ]
}
```

Honesty rules encoded here:

- `avg_logprob` and `no_speech_prob` are preserved, not discarded, and used as a confidence proxy in the UI (`low confidence` tag on segments below threshold).
- No speaker labels are invented. Without diarisation every segment has `speaker: null`.
- Nothing is text-completed or "cleaned up" into plausible-sounding speech.

Transcript reuse: the analysis job hashes `(source sha256, model, compute_type, language, vad)` into `fingerprint`. If a stored transcript matches, ASR is skipped and the job says so.

Progress during ASR: `max(segment.end_ms) / source_duration_ms`. It can stall for a few seconds between segments on this CPU; that is real, and it is better than a fake linear bar.

## 4. Sentence segmentation

Words → sentences with these rules, in order:

1. Split at terminal punctuation (`. ! ? …`) when the word ends with it.
2. Split at a pause ≥ 700 ms between consecutive words.
3. Split when a segment boundary coincides with a pause ≥ 400 ms.
4. Merge fragments shorter than 1.2 s into the neighbour unless they end a sentence.
5. Cap sentences at 45 s (hard split at the longest word gap inside).

Each sentence keeps `start_ms`, `end_ms`, `text`, `word_count`, `ends_with_terminator`, `leading_words` (first 3), and `gap_before_ms`.

## 5. Candidate generation

Goal: plausible *clip* boundaries, not fixed windows.

```
bands = [(15s,30s), (30s,60s), (60s,90s)]
for each band (min_d, max_d):
  for each sentence index i that is a legal start:
      grow j from i while (sentence[j].end - sentence[i].start) < max_d
      if duration in [min_d, max_d] and sentences[i..j] ends a sentence:
          emit candidate(i, j)
      also emit the largest window ≤ max_d whose duration ≥ min_d
```

A **legal start** requires the sentence to begin after a real pause (≥ 300 ms) *or* to start a segment *or* to start with a discourse marker that opens a thought ("So", "Now", "Here's"). This is what stops the generator from producing clips that begin mid-sentence.

Boundary refinement after windowing: extend the start back by up to 400 ms of leading silence and the end forward by up to 600 ms if the next word starts a new sentence, so the clip does not cut on the first phoneme.

Hard limits: 400 candidates per band, 1200 total, chosen by even distribution across the timeline so a long tail of the video is not starved.

## 6. Ranking

### 6.1 Baseline (default, deterministic, offline)

Ten components, each normalised to `0..1`, each with a `reason` string and the evidence that produced it.

| Component | Signal | Direction |
| --- | --- | --- |
| `hook` | first sentence: question, number, second person, curiosity marker, short punchy clause | higher better |
| `coherence` | starts at sentence boundary, ≥ 80 % of sentences complete, no dangling conjunction start | higher |
| `completeness` | ends with terminal punctuation, ends a thought, no "and then…" tail | higher |
| `novelty` | type/token ratio + rare-word ratio (inverse document frequency over this transcript) + named-entity/number density | higher |
| `information_density` | non-stopword tokens per second, penalised by silence ratio inside the clip | higher |
| `emotion` | affect lexicon hits, exclamation, laughter markers, intensifiers, anecdote markers ("I remember", "the day") | higher |
| `quotability` | presence of a ≤ 12-word standalone assertive sentence, imperative or aphorism shape | higher |
| `context_dependency` | anaphora in the first two sentences ("this", "that", "it", "they", "he", "she", "as I said", "earlier"), unresolved proper nouns | **lower better** |
| `boundary_quality` | silence before start / after end, alignment to sentence edges | higher |
| `misleading_risk` | starts with a contrastive ("But", "However") or a conditional whose consequence is outside the clip; clip is a strict substring of a longer argument | **lower better** |

```
overall = 100 * Σ w_i · c_i        (context_dependency and misleading_risk enter as 1 - c)
```

Goal-specific weights are configuration data, not code branches:

| Goal | Heavy weights |
| --- | --- |
| `best_moments` | hook, emotion, quotability |
| `social_clips` | hook, quotability, boundary_quality |
| `educational` | information_density, completeness, coherence |
| `funny` | emotion, quotability, boundary_quality |
| `debate` | novelty, hook, misleading_risk |
| `emotional_stories` | emotion, completeness, coherence |
| `tutorials` | information_density, completeness, context_dependency |
| `highlights` / `condensed` | coherence, information_density, novelty |

Scores are **estimates of content quality**, never engagement predictions. The UI labels them "estimated appeal" with a tooltip explaining the components.

### 6.2 Dedupe and diversity

1. Sort by `overall`.
2. Walk the sorted list; suppress a candidate whose time overlap with an already-kept candidate exceeds `min(60 %, intersection/union)` *or* whose text similarity (token Jaccard) exceeds 0.7.
3. Optionally enforce timeline spread: at most `ceil(n / 3)` kept candidates per 10 % of the video, so the results are not twelve versions of the same story.
4. Return the requested count + the suppressed ones flagged `near_duplicate_of`, so the user can see what was merged rather than wondering where a moment went.

### 6.3 Optional LLM rescoring

- Opt-in. Requires `CLIPFORGE_LLM_BASE_URL`, `CLIPFORGE_LLM_MODEL`, `CLIPFORGE_LLM_API_KEY` (key optional for local servers).
- Sends **only the shortlist** (top 24 by baseline, plus 4 diverse ones), as `{id, start, end, text}`.
- The prompt asks for JSON only. Response is parsed against a strict Pydantic schema; any of: non-JSON, wrong ids, out-of-range scores, timestamps outside the candidate, or a timeout → the whole batch is discarded, baseline scores stay, and `warnings` records `llm_fallback`.
- The response cannot change `start_ms`/`end_ms`. Model output is never trusted for time.
- UI has a consent checkbox that states, in plain language, that transcript text will be sent to the configured endpoint. Off by default.

### 6.4 Context and quality checks before display

Every candidate is checked and annotated:

- `start_ms >= 0`, `end_ms <= duration`, `start < end`, `duration >= min_duration`.
- Snapped to transcript boundaries within ±300 ms where a boundary exists.
- `silence_ratio > 0.4` → warning `mostly_silence`.
- First sentence < 4 words and starts with a pronoun → warning `weak_opening`.
- No terminal punctuation at the end → warning `unresolved_ending`.
- Anaphora in the first sentence → warning `needs_context`.
- Overlap with an already-selected candidate → warning `overlaps_selected`.

Warnings never remove a candidate automatically; the user decides.

## 7. Enhancement (optional, before composition)

The optional enhancement pass is documented in full in
[ENHANCEMENT.md](ENHANCEMENT.md). Its position in the job is:

```
retained source -> probe -> optional hqdn3d -> AI super-resolution (per frame)
  -> resize to a source-aspect intermediate -> video-only x264 CRF 12
  -> existing composition graph -> audio from the retained original -> gate
```

- **Standard** uses the existing FFmpeg graph with lanczos resizing and no
  model.
- **Clean** adds one `hqdn3d` node before the composition split.
- **AI Enhance / AI Enhance+** build one cached intermediate per selected
  segment. Composition, captions and face-cam rectangles stay in normalised
  source coordinates, so they remain valid.
- **AI Enhance Fast** uses ESPCN 2×, no denoise/blend, and the model's native
  intermediate size with a CRF 16 ultrafast encode. When the target is larger
  than the model output, composition applies one final lanczos scale; that is
  the explicit speed/quality trade-off.
- The pass samples at the final output frame rate. A 60 fps source rendered at
  30 fps is decoded and enhanced 30 times per second, not 60; the alternative
  doubled CPU time and intermediate size for frames the final `fps` filter
  would discard.
- Audio is never written into the intermediate. The final renderer still reads
  the original source's audio, so a second video encode cannot move it.
- The quality preview calls the same frame function and framing chain, at the
  same source timestamp, so a comparison differs only in pixels.
- A missing model yields `applied_model: null` and an explicit gate warning, or
  a structured `enhancement_model_unavailable` failure when fallback is refused.

Progress is measured in decoded frames. Cancellation is checked per frame and
partial intermediates are deleted. The cache key includes the source content
hash, span, model version/weight hash, denoise, blend, target intermediate size
and source fps.

## 8. Rendering

Spec (abridged):

```jsonc
{
  "version": 1,
  "source_id": "…",
  "segments": [{ "start_ms": 121000, "end_ms": 168500 }],
  "video": { "aspect": "9:16", "resolution": 1080, "framing": "crop",
             "fps": null },
  "captions": { "enabled": true, "preset": "clean", "font_family": "Arial",
                "font_size": 64, "position": "bottom", "margin_v": 120,
                "primary_color": "#FFFFFF", "highlight_color": "#FFD166",
                "uppercase": false, "max_words_per_line": 5, "max_lines": 2 },
  "audio": { "normalize": false }
}
```

Filter graph (single segment, crop framing):

```
[0:v]trim=start=121:end=168.5,setpts=PTS-STARTPTS,
     scale=-2:1080,crop=608:1080:(iw-608)/2:0,
     subtitles=captions.ass[v]
[0:a]atrim=start=121:end=168.5,asetpts=PTS-STARTPTS[a]
```

Fit-to-blur framing instead splits into a blurred background and a fitted foreground:

```
[0:v]trim=…,setpts=…,split=2[bg][fg];
[bg]scale=608:1080:force_original_aspect_ratio=increase,crop=608:1080,
    gblur=sigma=32,eq=brightness=-0.08[bgz];
[fg]scale=608:-2[fgs];
[bgz][fgs]overlay=(W-w)/2:(H-h)/2,subtitles=captions.ass[v]
```

Implementation details that matter:

- FFmpeg runs with `cwd=<render tmp dir>` and relative filenames, so no path escaping in the filter graph (a real Windows hazard).
- `-progress pipe:1 -nostats`; progress = `out_time_us / expected_output_duration_ms`.
- `-movflags +faststart`, `-pix_fmt yuv420p`, `-c:v libx264 -preset veryfast -crf 20`, `-c:a aac -b:a 160k -ar 48000`.
- Even dimensions are forced (H.264 requirement).
- Cancel → terminate the process, delete the partial file, job `cancelled`.
- On success: re-probe output (streams exist, duration within ±1.5 s of expected), write `renders` row, move from tmp to `renders/`.

## 9. Cleanup

- `tmp/` inside a project is cleared at the start of every render.
- Deleting a project removes `data/projects/<id>` (after confirmation in the UI) and cascades DB rows.
- The source is never deleted by a failed job, a cancel, or a re-analysis.
- Model caches live in `data/models/` (faster-whisper cache), deliberately outside project dirs so they survive project deletion and are reclaimed by deleting that one folder.
