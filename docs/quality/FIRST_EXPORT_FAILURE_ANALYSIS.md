# First vertical export: failure analysis

This document records what went wrong in the first real end-to-end vertical export and what
changed in the pipeline so the failures are fixed by construction, not by patching timestamps
in the sample.

Source inspected: `1920x1080`, `29.97 fps`, `22:58`, AV1 video, Opus stereo.
Outputs inspected: rank 0 ad render (`56.3s`, `1080x1920`) and rank 3 editorial render
(`23.4s`, `1080x1920`), both H.264/AAC.

## Observed failures

| # | Symptom | Evidence | Root cause |
|---|---|---|---|
| 1 | Slides and screen content were centre-cropped, losing edge text and tables | Contact-sheet frames for the ad and editorial renders showed the middle of 16:9 slides filling the vertical frame | The renderer had one global `framing` switch; `crop` scaled to fill and there was no per-scene layout |
| 2 | Captions ran off both sides of the frame | 12s “nothing in society is likely”; 20s “mirrors what Harvard's youth polls” | ASS style was 96px with 80px margins at 1080 wide, `WrapStyle: 2` disabled libass wrapping, and the Python helper only limited words per line, not measured width |
| 3 | Titles ended in `...it.` / `...the.` | Candidate cards displayed mid-word truncation | Title text was cut by character count after sentence generation |
| 4 | The clip ended on unrelated archival footage and a “blackstone” logo | Final seconds of the ad render cut away from the speaker while speech continued into the next editorial segment | Candidate end times were sentence-aligned only; no scene-aware ending review and no user-facing boundary choice |
| 5 | A URL import showed progress going from 92% back to 6% | Import log when switching a YouTube import from video to audio | Progress was emitted as two independent phases using the same job counter |
| 6 | YouTube URLs became `..._` in `display_name` | Source record for the test project | `?` was replaced during filename sanitisation after the query string was kept |

## Why the first architecture allowed this

- Composition was one global `framing` value. There was no data model for “this scene is a
  slide” versus “this scene is a speaker”, so the renderer could not make a different choice.
- Captions were generated from word counts. The actual font file, size, and rendered width
  were never measured before encoding.
- Candidate generation optimised for transcript quality. Scene changes and trailing shots
  were outside its model, so a sentence-aligned end could still be visually wrong.
- The only validation after encoding was “file exists and FFprobe reads it”. Nothing compared
  dimensions, streams, duration, A/V sync, black/freeze frames, caption bounds, or overlays.
- Source time and output time were implicit in list order. There was no exported edit decision
  list that a reviewer or another tool could inspect.

## Changes in this pass

### 1. Per-segment composition

- `RenderSpec` version 3 stores a `CompositionDecision` on every `SegmentSpan`.
- `media/visual.py` detects cuts with FFmpeg scene detection and samples frames for cheap,
  explainable features (edge energy, monochrome/flat regions, skin-tone share, subtitle-like
  bands). It classifies scenes as `slide`, `screen_recording`, `talking_head`, `mixed`, or
  `unknown`.
- `rendering/composition.py` chooses `fit` for slides, screen content, unknown information-dense
  scenes, and vertical targets; `crop` for centred talking heads. `pip` and `split` are
  modelled. A single source cannot honestly produce a split/stacked view, so that request
  degrades to `fit` with a warning.
- `fit` keeps the whole source visible and fills the rest with a blurred, slightly darkened
  copy of the same frame. A solid colour is available. No large model, OCR, or face tracker is
  required; those can be added later as optional evidence.
- Manual modes are stored on the spec. `crop_x_pct` / `crop_y_pct` provide a global focus,
  and `composition_overrides` can replace individual segment decisions.

### 2. Measured captions

- Every candidate line is measured with Pillow against the actual bundled font file.
- Wrapping uses `max_line_width_pct`, horizontal margins, `max_words_per_line`,
  `max_chars_per_line`, and `max_lines`; a too-wide phrase is split at a natural boundary,
  never truncated.
- If a span still does not fit, the whole span is reduced down to `min_font_scale` and the
  report warns. A single over-wide word is reported instead of being silently clipped.
- ASS now uses `WrapStyle: 0` and real `PlayResX`/`PlayResY`. Each word is its own event, but
  every event keeps the whole chunk visible until the next word starts, so highlighting is
  exact without one-frame gaps.
- The selected font file is copied into `fonts/` in the render workspace and referenced with
  `ass=...:fontsdir=fonts`. Only the selected file is copied, avoiding libass family/weight
  ambiguity.
- The requested proprietary faces are substituted with redistributable binaries:
  Montserrat, Inter (for SF Pro Display), Archivo Black (for The Bold Font), Bebas Neue,
  Lato (for Helvetica/Arial), and Anton (for Impact). The substitution is recorded in the
  caption report, not hidden.

### 3. Ending review as evidence

- `quality/boundaries.py` looks at the last sentence, the next sentence, nearby scene cuts,
  and the content type on each side of the out point.
- It returns a suspicion score, plain-language evidence, and alternative source-time
  endpoints (current, last scene cut, previous sentence, finish the next sentence).
- A cut within a few frames of the out point is offered as “Trim the final cut” so the last
  stray frames of the next shot can be removed without discarding the sentence.
- The review also runs at render time for older candidates, so an export cannot silently
  publish a suspicious ending just because analysis predates this check. The gate reports it
  as a warning and never trims automatically.
- It never changes the candidate. The editor can accept an alternative, keep the current out
  point, or reset the review. Acceptance writes a user end time; rejection leaves the clip
  untouched.

### 4. Versioned EDL and export gate

- `RenderSpec.to_edl()` emits source-time and output-time coordinates per segment, the
  composition decision, and caption-band data.
- `quality/gate.py` probes the encoded file and checks: H.264/AAC streams, dimensions,
  frame rate, duration, full decode, audio presence, A/V stream start and duration delta,
  black segments, freeze segments, measured caption bounds, logo overlay application, and
  the EDL. A failure blocks publication but preserves the encoded draft and review JSON;
  warnings are shown and do not block.

### 5. Review UI

- The render panel previews crop/fit/pip/split schematically, shows the measured caption
  band, exposes composition and crop focus, selects the caption face, and shows the review
  report inline.
- The review report includes check details, black/freeze timestamps, caption measurements,
  source → output EDL rows, and a draft download when the gate blocked publication.
- The clip editor shows ending evidence and alternatives with accept/reject/reset controls.

## Regression coverage

`apps/worker/tests/test_composition.py` covers:

- 16:9 inside 9:16 geometry;
- slide → `fit` with a caption band below the picture;
- talking head → `crop` with subject focus;
- user crop focus overriding scene subject;
- measured caption width never exceeding the safe width and no truncation;
- every spoken word surviving segmentation;
- font substitution resolution and real metric measurement;
- unique FFmpeg filter labels for two contained segments and `fontsdir`;
- crop focus → exact `crop` x/y expressions;
- ending review flagging continued speech and a scene cut, while never mutating the end;
- quiet ending review at the end of the source.

## Remaining follow-ups

- Run one full export against the sample project with the new gate and inspect the preserved
  review JSON and draft. The pipeline is unit-verified; this is the production acceptance run.
- Optional OCR/vision evidence for slides with unusual layouts. The current first pass is
  deterministic and explainable on purpose.
- Frame-level PSNR/SSIM comparison between source and output around cuts as extra evidence.
- Rotated and variable-frame-rate source fixtures in slow/integration tests.
