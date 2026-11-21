# Render performance: baseline, optimisation and 5 ms/frame verdict

Status: measured on the reference Windows laptop. The 5 ms/frame target was
**not achieved for the defined 1080p standard workload**. It was achieved only
for a different, smaller 720p workload with an ultrafast encoder. The numbers
and the exact conditions are below; no benchmark result is extrapolated without
saying so.

## 1. Defined workload

| Item | Value |
| --- | --- |
| Source | 1280×720, 30 fps, H.264/AAC, 10 s, representative gameplay from project 732a… at 2545 s |
| Standard landscape output | 1920×1080, 30 fps, H.264/AAC, x264 `veryfast`, CRF 23, no captions/logo |
| Standard portrait output | 1080×1920, same codec/preset |
| Audio | Original AAC, 48 kHz stereo, re-encoded AAC 128 kbps |
| Frames | 300 output frames per 10 s scenario |
| Target | **5 ms per output frame (200 fps)** |
| Measurement | warm run after a cold run; FFmpeg `-benchmark`; media process wall time |

Cold start is reported separately. Startup and finalisation are not divided
into the warm per-frame number. The suite also reports decode-only and
filter-only variants so encode cost is visible.

Hardware:

- Intel Core i7-6820HQ, 4 cores / 8 threads, 2.7 GHz
- NVIDIA Quadro M1000M, 2 GB, driver 573.71
- Intel HD Graphics 530
- Windows, Python 3.13.3, FFmpeg N-128433

## 2. Reproduce

```powershell
cd apps/worker
$env:PYTHONPATH="src"
.venv\Scripts\python -m clipforge.benchmark.render `
  --source ../../data/benchmark/gameplay-project-2545-30fps-10s.mp4 `
  --duration 10 --repeat 1 `
  --out ../../data/benchmark/render-results.json `
  --save-baseline ../../data/benchmark/render-baseline.json
```

Regression comparison:

```powershell
.venv\Scripts\python -m clipforge.benchmark.render `
  --source ../../data/benchmark/gameplay-project-2545-30fps-10s.mp4 `
  --repeat 1 --check ../../docs/performance/render-baseline.json --tolerance 0.25
```

The committed baseline is a reference for this hardware, not a universal
expectation. A different machine will legitimately fail a 25% gate.

## 3. Measured results

Warm wall time per output frame on the 10 s, 300-frame workload:

| Scenario | ms/frame | fps | Notes |
| --- | ---: | ---: | --- |
| Standard landscape 1080p | **28.50** | 35.1 | 720p→1080p, x264 veryfast |
| Standard portrait 1080p | **23.67** | 42.2 | crop-first path |
| Gameplay only 1080p | **24.39** | 41.0 | explicit crop |
| Gameplay + stacked webcam | **24.93** | 40.1 | two panels + vstack |
| Captions + logo | **43.02** | 23.2 | libass + overlay |
| Complex (stack + captions + logo + loudness) | **42.88** | 23.3 | most expensive standard case |
| Conventional 720→1080 resize | **25.78** | 38.8 | same as landscape |
| **AI Enhance Fast** | **1,820 ms/frame enhancement + 31 ms final render = ~1,851 ms/frame** | 0.54 | ESPCN 2×, 1080×1920; separate workload |

Cold-start range was **7.3–12.0 s** for the 10 s standard scenarios. The cold
run includes FFmpeg process start and the first input/output initialisation; it
is not representative of steady-state throughput.

### 5 ms/frame verdict

| Workload | Best measured | 5 ms? |
| --- | ---: | --- |
| 1080p standard, software x264 veryfast CRF23 | 23.7–28.5 ms/frame | **No** |
| 1080p standard, software x264 ultrafast CRF23, bicubic, no audio | ~13.6 ms/frame | **No** |
| 720p standard, software x264 ultrafast CRF23, no audio | **4.68 ms/frame** | Yes, but a different and lower-quality workload |
| 1080p AI Enhance Fast | ~1.2–1.9 s/frame | No, and not expected |

On this hardware, **5 ms/frame at 1080p is not achievable with software
decode + scale + H.264 encode**. The encoder alone is roughly 8–14 ms/frame at
1080p; 720p60 decode+scale is another 2.5–9 ms/frame depending on filter.
Reaching 5 ms at 1080p needs a hardware encoder/decoder path that is not
available in this installation.

## 4. Bottlenecks found

1. **Scale-before-crop on portrait exports.** The old chain scaled a 1280×720
   source up to 3413×1920 and then cropped 1080×1920. That is 6.55 MP of work
   for a 2.07 MP output. Changing to crop-first reduced a controlled portrait
   render from **41.5 ms/frame to 21.4 ms/frame** (about 49% faster) at the same
   output dimensions and focus.
2. **Lanczos on every standard upscale.** Standard/clean upscales now use
   bicubic; AI and downscales keep lanczos. Controlled 1080p encode dropped
   from 25.8 to 23.9 ms/frame (~7%).
3. **60 fps sources rendered at 30 fps were enhanced twice.** AI now samples at
   the final output rate. The project source is 60 fps; a 30 fps export went
   from 5292 to 2646 frames.
4. **Intermediate encode on AI.** Fast AI writes the model-native 2560×1440
   intermediate at CRF 16 `ultrafast` instead of 3413×1920 CRF 12 `veryfast`.
   This is still an avoidable encode/decode, and remains the largest AI
   overhead after inference.
5. **Captions/logo cost.** Adding captions and a logo to the portrait stack
   raises ~25 ms to ~43 ms/frame. The subtitle filter and overlay are per-frame
   work; caption generation itself is sub-millisecond for this clip.
6. **Cold start.** 7–12 s on the 10 s runs. A 60 s render amortises it, but a
   short render should not be judged as steady-state per-frame throughput.
7. **Hardware acceleration is not usable here.** NVENC fails with
   `Driver does not support the required nvenc API version. Required: 13.1
   Found: 13.0`; the installed driver is 573.71. QSV hardware frames failed to
   initialise and the software QSV path was slower (28–31 ms/frame). AMF found
   no AMD device. The renderer therefore keeps libx264 and reports the fallback.

## 5. Optimisations landed

| Change | Evidence | Trade-off |
| --- | --- | --- |
| Crop to output aspect before scaling | Portrait controlled run 41.5 → 21.4 ms/frame | None for the same crop focus; less oversampling |
| Bicubic for standard/clean upscales | 1080p control 25.8 → 23.9 ms/frame | Slightly less ringing/sharpening than lanczos |
| Sample AI at the final output fps | 60→30 fps clips process half the frames | Correct output timing preserved |
| Fast AI profile: native intermediate, CRF16 ultrafast, no denoise/blend, 6 OpenCV workers | 88 s project clip measured 1.2–1.9 s/frame depending on content/load | Final composition may lanczos the model result up to target |
| Per-render stage metrics | `metrics.stages`, frame count, ms/frame in the render result | Adds negligible DB/JSON size |
| Benchmark suite and baseline check | See section 2 | Not run in default CI; hardware-specific |

The crop-first change is covered by composition tests and renders in the
existing integration suite. The benchmark baseline is separate from correctness
tests.

## 6. AI enhancement, measured separately

For the 88 s project candidate, ESPCN 2× Fast AI on the reference CPU:

- Model/pipeline: **~1.2–1.9 s per output frame** depending on content and CPU
  load; the darker 2545 s segment measured 1.22–1.46 s/frame in short samples,
  while a 10 s high-detail sample measured ~1.8 s/frame.
- Final composition: ~30–65 ms/frame.
- At 24 fps (2117 frames), this is **~45–65 minutes** for the 88 s clip; it can
  cross 60 minutes under load. A 60 s clip is ~30–45 minutes.
- 5 ms/frame is not a target for this path. It is reported separately and never
  mixed into standard-render claims.
- Real-ESRGAN is much slower on CPU (~150 s/frame at 720p); it remains optional.

## 7. Regression gate

`clipforge.benchmark.render` records per scenario:

- cold and warm wall time, ms/frame, p95 where enough samples exist
- decode-only and filter-only ms/frame
- FFmpeg CPU seconds → approximate cores used
- peak RSS from FFmpeg `-benchmark maxrss`
- output dimensions, frame count, audio presence, audio/video duration delta
- caption build time
- AI enhancement wall time and model identity

`--check` compares the current run against a JSON baseline and fails on a
>25% per-scenario slowdown or invalid output. It is deliberately not wired into
the default CI gate because CI runners are noisy and differ from the reference
machine; it should be run on a controlled host before claiming a performance
change.

Correctness remains covered by the existing suite: render dimensions, frame
rate/count, duration, audio, caption bounds, composition chains, preview/export
parity and the export gate.

## 8. Remaining bottlenecks and next steps

1. **Hardware encoder driver.** NVENC is unavailable until the NVIDIA driver
   supports the FFmpeg build's NVENC API (610+); this GPU may not receive it.
   A machine with a current NVENC/QSV/AMF encoder should be benchmarked before
   claiming 5 ms at 1080p.
2. **Direct AI-to-composition pipe.** Fast AI still writes and re-decodes a
   video-only intermediate. A single-segment raw-frame pipe would remove one
   encode/decode, likely saving 0.2–0.4 s/frame in Fast AI. It is a larger
   change and not needed for standard render.
3. **Captions/logo cost.** Those scenarios are 43 ms/frame. Reducing per-word
   ASS events would break word-level timing; the only safe next step is a
   measured alternative caption renderer.
4. **Noise.** The reference laptop was running a browser and the dev server;
   repeated runs varied by 20–40%. The committed baseline records the workload
   and hardware so future comparisons are honest, but a quiet host is required
   for tight gates.
