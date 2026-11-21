# Video enhancement: audit, model evaluation and implementation

Status: implemented for Standard, Clean, AI Enhance and AI Enhance+. The original
source is never modified. AI runs only when a model is present and the user asks
for it; otherwise the renderer falls back to conventional resizing and says so.

This document records the Phase 1 audit, the model/licence decision, the measured
baseline, and the limits that remain. It is deliberately more specific than a
feature announcement.

---

## 1. Root-cause audit

### 1.1 Why 720p appeared to be the ceiling

The ceiling was **not** source retention, storage or analysis. It came from the
download policy:

| Location | Value | Effect |
| --- | --- | --- |
| `media/policy.py` | `DEFAULT_QUALITY = "720p"` | Any URL import with no explicit quality asks for 720p. |
| `media/policy.py` | `PROCESSING_MODES["balanced"]["quality"] = "720p"` | The default processing mode inherits the 720p request. |
| `api/models.py` | `processing: ... = "balanced"` | A URL import that omits processing gets the balanced/720p default. |
| `api/routes_renders.py` / `rendering/spec.py` | output `resolution` was `720 | 1080` | The editor could only ask for a 720p or 1080p *output*. |

The retained source itself was already preserved correctly:

- `persist_source` writes the downloaded or uploaded bytes once, records a
  SHA-256 content hash, and never overwrites the source row from a proxy.
- `media/proxy.py` builds a separate video-only analysis proxy. `balanced`
  defaults to a 480p proxy; `high` to a 720p proxy. The proxy is explicitly
  “scene analysis only; never an export source”.
- `rendering/service.py` reads the retained source, not the proxy, for every
  export.
- The sample in `data/samples/clipforge-sample.mp4` is 1280×720, 30 fps,
  70.97 s. Its analysis proxy is a different file.

So a 720p project usually meant **the downloader fetched 720p**, not that the
pipeline had thrown away a 1080p source. A 1080p source was always available by
choosing `high`, `1080p`, `best` or custom 1080p+ at import time. The missing
piece was that a 720p source could only be enlarged by FFmpeg's implicit
bicubic `scale`, and the UI called that "1080p" without distinguishing it from
enhancement.

### 1.2 Existing processing pipeline

| Area | Current behaviour before this change |
| --- | --- |
| Probe | `ffprobe -show_format -show_streams`, rotation-aware display dimensions, codec allowlist. |
| Source retention | `data/projects/<id>/source/<uuid>.<container>` plus `sources.content_hash`; never overwritten by derived files. |
| Proxy | `scale`-only H.264 CRF 30, audio dropped, timestamps preserved. |
| Transcription | 16 kHz mono PCM; faster-whisper int8 on CPU; model cache in `data/models`. |
| Preview | One real frame rendered through the same `_framing_chain` used by export. |
| Export | One FFmpeg filter graph: trim/scale/crop/stack, captions, audio chain, logo, concat, x264/AAC. |
| Video denoise | None. |
| Video sharpening | None. |
| Audio denoise | Optional `afftdn` (`AudioSettings.denoise`). |
| Resize | `scale=...:force_original_aspect_ratio=...` with FFmpeg's default filter. |
| Progress | FFmpeg `-progress pipe:1`, `out_time_us / expected_duration`. |
| Cancellation | Kill the FFmpeg process group; delete the partial output. |
| Retries | Queue-level park for transient failures; render itself had no internal retry. |
| Storage | Local or Supabase; `data/projects/<id>/tmp` cleared at the start of every render. |
| Output profiles | 720p/1080p class, 9:16/16:9/1:1; compression presets smallest/balanced/higher/custom. |
| Tests | FFmpeg argv, composition, captions, gate, import and render integration; no model tests. |

### 1.3 Hardware and runtimes observed on the reference machine

| Item | Value |
| --- | --- |
| CPU | Intel Core i7-6820HQ, 4 cores / 8 threads |
| GPU present | NVIDIA Quadro M1000M (2 GB), Intel HD Graphics 530 |
| ONNX Runtime | 1.32, providers `AzureExecutionProvider`, `CPUExecutionProvider` |
| OpenCV | 4.14 contrib headless, CPU DNN target |
| FFmpeg | N-128433, H.264/HEVC NVENC/QSV/AMF encoders listed |
| GPU inference for enhancement | **Not available by default**; no CUDA or DirectML execution provider is installed |

The renderer therefore treats CPU as the guaranteed path. If an ONNX runtime
with CUDA or DirectML is installed later, the Real-ESRGAN engine selects it and
reports the device it actually used.

### 1.4 Implementation risks identified before changing code

1. **Composition coordinates.** Crop and face-cam rectangles are normalised
   source coordinates. Enhancement must scale the whole frame, not crop first,
   or every stored rectangle would need remapping.
2. **Audio drift.** A second video encode can move audio if it is re-muxed
   carelessly. The intermediate is video-only; audio is always read from the
   retained original in the final render.
3. **Preview/export divergence.** The existing preview already shares the
   composition chain. Enhancement had to share the *frame* path too, or the
   comparison would show a different image from the export.
4. **Cost on CPU.** Real-ESRGAN is roughly two orders of magnitude slower than
   ESPCN on this CPU. A single default model would have made AI unusable.
5. **Silent AI claims.** A missing model must produce a warning and an explicit
   `applied_model: null`, not a 1080p file labelled “AI”.
6. **Cache identity.** A cache keyed only by project or clip id would return
   stale pixels after a model or strength change.
7. **Disk.** A 1440p/2160p intermediate is larger than the source. The pass
   checks free space before writing and deletes partial files on failure.

---

## 2. Pipeline order

```
retained source
  -> probe (width, height, fps, duration, audio)
  -> optional hqdn3d denoise (FFmpeg decode filter)
  -> sample at the final output fps (a 60 fps source exported at 30 fps
     is enhanced once per output frame, not twice)
  -> AI super-resolution (per frame, tiled, bounded memory)
  -> resize once to an intermediate that covers the composition
  -> encode video-only intermediate (x264 CRF 12)
  -> existing composition / captions / logo graph
  -> audio from the retained original (AAC 48 kHz)
  -> probe + export gate + quality review
```

**Why before composition.** The composition plan is already computed from the
analysis proxy. Enhancing the full source frame preserves every normalised
rectangle (crop, face-cam panel, caption band). Advancing the pipeline also
means changing the enhancement strength does not re-run transcription, moment
discovery or face-cam tracking.

**Why video-only intermediates.** Audio is mapped from the original source in
the final command. The intermediate cannot drift, and an audio re-encode is not
duplicated.

**Why one intermediate size.** The intermediate keeps the source aspect and is
at least as large as the output frame in both dimensions whenever the selected
model can cover it. Composition then crops/downscales once. A landscape source
composed into portrait can still need more than 2×; the auto model selector
chooses ESPCN 4× in that case. If the cap is reached, the remaining resize is
conventional and the report still says so.

---

## 3. Models and licences

| Model | Role | Scale | Size | Licence | Runtime | Redistribution |
| --- | --- | --- | --- | --- | --- | --- |
| ESPCN x2 | Default AI Enhance | 2× | 86 KB | Apache-2.0 | OpenCV `dnn_superres`, CPU | Bundled with `LICENSE-ESPCN.txt` |
| ESPCN x4 | Auto when the frame needs >2.25×, AI Enhance+ | 4× | 100 KB | Apache-2.0 | OpenCV `dnn_superres`, CPU | Bundled with `LICENSE-ESPCN.txt` |
| Real-ESRGAN x2 (RRDBNet, 23 blocks) | Optional maximum-quality model | 2× | 67,073,434 bytes | BSD-3-Clause (upstream weights) | ONNX Runtime, CPU/CUDA/DirectML | Downloaded only after an explicit user action; pinned SHA-256 |

The Real-ESRGAN ONNX export is the 1:1 export of the official
`RealESRGAN_x2plus.pth` weights published by SceneWorks, with a documented
interface:

- input `input` `[1,3,h,w]`, float32 RGB in 0–1, dynamic height/width;
- output `output` `[1,3,2h,2w]`;
- SHA-256 `7115ba92e8a1bfa63d68558ef006ef3d91273a068d321b1439f8bb1c9179002c`.

The model is **not** committed to the repository. `POST
/api/enhancement/models/realesrgan-x2/install` enqueues a download job; bytes are
streamed to a `.part` file, hashed, and only then moved into
`data/models/enhancement/`. The licence and source URL are exposed in
`GET /api/capabilities`. No user video is sent anywhere by this operation.

No face-restoration model (GFPGAN/CodeFormer) is integrated. Running one across
a full frame can change identity-relevant details, and the evaluation below did
not justify that risk.

### 3.1 Measured throughput

Measured on the reference machine. ESPCN’s number comes from a real 30-frame
pipeline pass; Real-ESRGAN’s from the install benchmark (a 480×270 warm pass).
“Full pipeline” includes decode, denoise, model, blend, resize and encode.

| Model | Input throughput | 720p frame (0.92 MP) | Result |
| --- | --- | --- | --- |
| ESPCN x2 | 0.48 MP/s (measured full pass) | ≈ 1.4–1.9 s | Usable for short clips on CPU |
| ESPCN x4 | similar compute, larger output | ≈ 1.5–2.1 s | Used for >2.25× composition |
| Real-ESRGAN x2 | 0.0061 MP/s (162.6 s/MP measured at install) | ≈ 150 s | Not a usable CPU default; optional for short clips/GPU |

These numbers are why AI Enhance defaults to ESPCN, not Real-ESRGAN. The user can
still select Real-ESRGAN after installing it; the UI then shows a measured
estimate rather than pretending it is fast.

---

## 4. Profiles

| Profile | What runs | AI | Honest description |
| --- | --- | --- | --- |
| Standard | FFmpeg lanczos resize | No | Default. No denoise, no model. |
| Clean | `hqdn3d` (default 30) + lanczos | No | Conservative denoise; preserves natural texture. |
| AI Enhance | ESPCN/Real-ESRGAN, default blend 0.90, optional denoise 15 | Yes | Reconstructs plausible pixels; does not restore native detail. |
| AI Enhance+ | Model, blend 1.00, denoise 30, static-frame stabilisation | Yes | Stronger, slower; warns that faces/texture can change. Requires a comparison before export in the UI. |

Profiles are processing choices, not quality guarantees. The export gate marks
every AI render as a warning with the model and the real source→output scale.

### 4.1 Resolution semantics

Four separate ideas are now distinct:

- **source resolution** — probed from the retained file;
- **analysis resolution** — the proxy (480p/720p by default), never an export
  input;
- **composition resolution** — the concrete `RenderSpec.video` dimensions;
- **export resolution** — the composition dimensions after the compression cap.

Targets:

| Target | Behaviour |
| --- | --- |
| Preserve source | Keeps the source short side; 1280×720 → 1280×720 in 16:9, 720×1280 in 9:16. |
| 720p | 1280×720 / 720×1280 / 720×720. |
| 1080p | 1920×1080 / 1080×1920 / 1080×1080. |
| 1440p | 2560×1440 / 1440×2560 / 1440×1440. |
| 2160p (4K) | 3840×2160 / 2160×3840 / 2160×2160. |
| Custom | Any even dimensions that match the selected aspect within 1%; otherwise rejected with `invalid_resolution`. |

The compression cap remains independent. A 1440p composition with the Balanced
preset is encoded at 1080p and the UI says so; choose Higher/Custom to keep
1440p. Nothing is stretched.

---

## 5. Cache, cancellation and storage

- Cache key: SHA-256 of source content hash, segment span, profile, model id +
  version + weight hash, denoise, restoration blend, temporal flag, intermediate
  dimensions, source fps and intermediate encoder version.
- Project id is **not** part of the key. Copying a project does not force
  identical work to be redone.
- Completed intermediates live in
  `data/cache/projects/<project_id>/enhancement/`; deleting the project removes
  them, and a per-project 2 GiB pruning pass removes the oldest files.
- Partial `.part.mp4` files are deleted on cancel or failure.
- Cancellation is checked per decoded frame and before each attempt. Two
  attempts are allowed per segment; a model failure that repeats is reported as
  `enhancement_failed`, with the source untouched.
- Disk space is checked before each segment using a conservative bytes-per-pixel
  estimate.
- Model sessions are process-wide singletons keyed by path and device.

---

## 6. Quality evaluation

### 6.1 Set

| Clip | Why |
| --- | --- |
| `data/samples/clipforge-sample.mp4` | Dark, low-bitrate 1280×720 gameplay/abstract source, 30 fps. |
| Webcam frame (`baseline-600.png`, 1 s 720p pan) | Face, hair, skin, foliage, soft background. |
| Text/HUD frame (`forbes-frame-32.png`, 1 s 720p vertical) | Large and small text, flat panel, compression ringing. |

The 1 s evaluation clips were made from existing test imagery, not downloaded
user projects. Full-length synthetic test clips are generated inside
`tests/test_enhancement.py` and `tests/test_enhancement_api.py`.

### 6.2 Observations

- **Text/HUD:** AI Enhance produced the clearest letter edges. Standard 1080p
  was visibly softer; Clean reduced ringing but did not create edge detail.
  This is the strongest case for AI Enhance.
- **Webcam/face:** AI sharpened eyes and hair, but skin looked slightly more
  plastic and contrasty. Clean was the most natural for a talking head. This is
  why face restoration is not automatic and why the blend defaults to 0.90.
- **Foliage/texture:** AI added crispness to leaves, with some invented
  micro-texture. Standard was softer but more faithful.
- **Dark low-detail footage:** little visible benefit; the sample’s noise and
  blockiness can be amplified. Standard/Clean are the better defaults there.
- **Fast motion:** not separately modelled. Frame rate is preserved; no
  interpolation is performed. Static-frame stabilisation is gated on a
  near-zero inter-frame difference, so it cannot ghost ordinary motion.
- **Scene cuts:** a large inter-frame difference resets the stabiliser. No
  temporal model is used across cuts.

### 6.3 Measured wall time and size

| Clip | Profile | Wall time | Output size | Output |
| --- | --- | --- | --- | --- |
| 2 s sample, 1280×720 → 1920×1080 | Standard | 1.3 s | 155 KB | 60 frames, audio preserved |
| same | Clean (30) | 1.0 s | 90 KB | 60 frames |
| same | AI Enhance (cold) | 83.7 s | 100 KB | 60 frames, 2560×1440 intermediate |
| same | AI Enhance (cached intermediate) | 1.5 s | 100 KB | 60 frames |
| 1 s webcam, 1280×720 → 1920×1080 | Standard | 1.9 s | 152 KB | 30 frames |
| same | Clean (30) | 1.5 s | 123 KB | 30 frames |
| same | AI Enhance (cold) | 42.3 s | 135 KB | 30 frames |
| 1 s text, 720×1280 → 1080×1920 | Standard | 1.5 s | 139 KB | 30 frames |
| same | Clean (30) | 1.2 s | 79 KB | 30 frames |
| same | AI Enhance (cold) | 37.5 s | 102 KB | 30 frames, 1440×2560 intermediate |

Sizes are small because the evaluation clips are 1–2 s and the sources are
synthetic/low-bitrate. They are not a prediction for a real 30 s clip.

Generated comparison sheets live outside version control under
`data/test-output/enh-quality/` (`webcam-comparison.png`,
`webcam-detail.png`, `text-comparison.png`, `text-detail.png`). They are
reproducible from the script in the completion report; they are not committed
because they contain evaluation imagery.

Objective metrics were not treated as the decision. In particular, a higher PSNR
against the 720p source would reward blur, because the source is the reference.
The decision was made on paired visual inspection at 1:1.

---

## 7. Remaining limitations

1. **CPU AI is slow.** ESPCN is roughly 35–85 s per second of 720p footage on
   the reference 4-core CPU. A 60 s clip is a coffee break, not an interactive
   operation. The UI shows a measured estimate and the job reports real frames.
2. **No GPU inference by default.** ESPCN uses OpenCV CPU DNN. Real-ESRGAN can
   use CUDA/DirectML only if the installed ONNX Runtime provides it. There is no
   bundled CUDA runtime.
3. **Real-ESRGAN is optional and large.** 67 MB download, explicit consent,
   SHA-256 verified.
4. **Landscape→portrait composition can still upscale after the model.** The
   auto selector uses 4× when needed, but a cap can leave a conventional resize
   in the chain. The report states the model and effective scale.
5. **Intermediate encode is CRF 12, not lossless.** This bounds disk and encode
   time. It is a small generation loss, not a claim of zero artefacts.
6. **One model setting per render.** No per-scene model switching, no automatic
   “text scene vs face scene” classifier.
7. **No temporal super-resolution or frame interpolation.** Frame rate is kept
   by default. AI Enhance+ only averages near-static frames.
8. **No face restoration.** Identity-relevant detail is left to the general
   model and a conservative blend.
9. **Comparison is a still frame.** It proves crop/timestamp parity but cannot
   prove full-motion stability. Full-motion review still needs the exported clip.
10. **Cache is per project and pruned at 2 GiB.** A very large project may
    redo old segments; the key prevents stale reuse, not eviction.
11. **URL imports still default to 720p.** That default was not changed: it is a
    bandwidth choice. Import at 1080p/high when the source publishes it; AI is
    not a substitute for a better source.
12. **Standard output with the Balanced preset caps at 1080p.** 1440p/4K needs a
    compression preset with a higher cap; the UI says so.
13. **Gate frame-count check can warn** when a container omits `nb_frames`.
    Duration and fps are still checked.

---

## 8. Recommended defaults

- **Standard** for most exports, especially dark, grainy or already-1080p
  sources.
- **Clean (30)** for compressed 720p webcam/podcast footage where blocking is
  visible and speed matters.
- **AI Enhance (ESPCN x2, blend 0.90, denoise 15)** for text/HUD-heavy gameplay
  or screen recordings where edge clarity is the main complaint.
- **AI Enhance+** only for short clips and after comparing a frame; it is the
  most likely to alter faces and fine texture.
- **Real-ESRGAN** only after installing it and measuring a short clip; on CPU it
  is an overnight job, not a preview tool.
- **Import at the highest rendition the source publishes** before relying on
  enhancement. Upscaling is reconstruction, not recovery.

---

## 9. Verification

Automated:

- `apps/worker/tests/test_enhancement.py` — profile defaults, target geometry,
  aspect validation, intermediate sizing, cache identity, fallback, refusal,
  cancellation, preview/export chain parity, and **real rendered frames** for
  landscape, portrait, AI and cache reuse. Two tests decode actual pixels: the
  AI output is compared against a conventional resize of the same source, and
  a preview still is compared against the exported frame at the same timestamp.
- `apps/worker/tests/test_enhancement_api.py` — upload → AI render → completed
  job → 320×180 output with audio; quality-preview parity; invalid custom size;
  capabilities.
- `apps/web/tests/quality.test.ts` — target resolution, preserve-source,
  custom-size validation, and measured-time estimation.

Manual:

- `GET /api/capabilities` reports the real providers, installed models, licences
  and benchmark entries.
- `POST /api/projects/{id}/renders/quality-preview` returns two JPEG stills from
  the same source timestamp and composition decision.
- A render whose AI model is missing writes `applied_model: null` and a gate
  warning; it never claims AI ran.
