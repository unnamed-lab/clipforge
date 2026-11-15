#!/usr/bin/env node
/**
 * End-to-end proof of the local workflow, against the real worker.
 *
 *   node scripts/e2e.mjs                     import → analyse → edit → render → download
 *   node scripts/e2e.mjs --source <file>     use a different video
 *   node scripts/e2e.mjs --quality fast      use the smaller speech model
 *
 * This drives the HTTP API exactly as the web app does, then verifies the
 * exported MP4 with ffprobe. It intentionally fails loudly: a missing model, a
 * bad render or a truncated download must never be reported as success.
 */

import { execFileSync } from "node:child_process";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const baseUrl = (process.env.CLIPFORGE_WORKER_URL ?? "http://127.0.0.1:8788").replace(/\/+$/, "");

const args = process.argv.slice(2);
function flag(name, fallback) {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}

const sourcePath = path.resolve(
  repoRoot,
  flag("source", "data/samples/clipforge-sample.mp4"),
);
const quality = flag("quality", "fast");
const outDir = path.join(repoRoot, "data", "e2e");
const pollMs = Number(flag("poll", "2000"));

let token;
try {
  token = (await readFile(path.join(repoRoot, "data", "worker-token"), "utf8")).trim();
} catch {
  console.error("No data/worker-token found. Start the worker once so it can generate one.");
  process.exit(1);
}

const started = Date.now();
const checks = [];

function ok(name, detail = "") {
  checks.push({ name, ok: true, detail });
  console.log(`  PASS  ${name}${detail ? `  ${detail}` : ""}`);
}

function fail(name, detail) {
  checks.push({ name, ok: false, detail });
  console.log(`  FAIL  ${name}  ${detail}`);
}

async function api(pathname, init = {}) {
  const response = await fetch(`${baseUrl}${pathname}`, {
    ...init,
    headers: {
      "x-clipforge-token": token,
      ...(init.body instanceof FormData ? {} : { "content-type": "application/json" }),
      ...init.headers,
    },
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { raw: text };
  }
  if (!response.ok) {
    const message = payload?.error?.message ?? payload?.detail ?? payload?.raw ?? response.statusText;
    throw new Error(`${init.method ?? "GET"} ${pathname} → HTTP ${response.status}: ${message}`);
  }
  return payload;
}

async function waitForJob(jobId, label) {
  const deadline = Date.now() + 30 * 60_000;
  let lastLine = "";
  for (;;) {
    const { job } = await api(`/api/jobs/${jobId}`);
    const line = `${job.state} ${Math.round(job.progress)}% ${job.stage}`;
    if (line !== lastLine) {
      console.log(`    ${label}: ${line} — ${job.message}`);
      lastLine = line;
    }
    if (job.state === "completed") return job;
    if (job.state === "failed" || job.state === "cancelled") {
      throw new Error(`${label} ${job.state}: ${job.error_message ?? job.message}`);
    }
    if (Date.now() > deadline) throw new Error(`${label} did not finish within 30 minutes`);
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
}

function probe(file) {
  const raw = execFileSync(
    "ffprobe",
    [
      "-v", "error",
      "-show_entries", "format=duration,size,format_name",
      "-show_entries", "stream=codec_type,codec_name,width,height,duration",
      "-of", "json",
      file,
    ],
    { encoding: "utf8" },
  );
  return JSON.parse(raw);
}

async function main() {
  console.log(`Clipforge end-to-end · worker ${baseUrl}\n`);

  const health = await api("/api/health");
  ok("worker health", `v${health.version}, up ${health.uptime_seconds}s`);

  const capabilities = await api("/api/capabilities");
  if (!capabilities.transcription.available) {
    fail("transcription available", "faster-whisper reports unavailable");
    throw new Error("Transcription is unavailable, so the workflow cannot be proven.");
  }
  ok(
    "transcription available",
    `${capabilities.transcription.provider} · ${capabilities.transcription.device}/${capabilities.transcription.compute_type}`,
  );
  if (!capabilities.media.ffmpeg.available) throw new Error("ffmpeg unavailable");
  ok("ffmpeg available", capabilities.media.ffmpeg.version);

  const sourceInfo = await stat(sourcePath);
  const sourceProbe = probe(sourcePath);
  ok("fixture readable", `${path.basename(sourcePath)}, ${(sourceInfo.size / 1024 / 1024).toFixed(1)} MB, ${sourceProbe.format.duration}s`);

  const { project } = await api("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name: `E2E ${new Date().toISOString().slice(0, 16).replace("T", " ")}` }),
  });
  ok("project created", project.id);

  const form = new FormData();
  form.append("file", new Blob([await readFile(sourcePath)]), path.basename(sourcePath));
  const uploadStarted = Date.now();
  const { source } = await api(`/api/projects/${project.id}/sources/upload`, {
    method: "POST",
    body: form,
  });
  ok(
    "source imported and probed",
    `${source.width}×${source.height} · ${(source.duration_ms / 1000).toFixed(1)}s · audio=${source.has_audio} · ${(
      (Date.now() - uploadStarted) / 1000
    ).toFixed(1)}s`,
  );
  if (!source.has_audio) throw new Error("Fixture has no audio track");

  const analysisStarted = Date.now();
  const { job: analysisJob } = await api(`/api/projects/${project.id}/analysis`, {
    method: "POST",
    body: JSON.stringify({
      goal: "best_moments",
      clip_count: 8,
      duration_band: "auto",
      quality,
      vad: true,
    }),
  });
  console.log(`  ....  analysis queued as ${analysisJob.id}`);
  const finishedAnalysis = await waitForJob(analysisJob.id, "analyse");
  ok(
    "analysis completed",
    `${((Date.now() - analysisStarted) / 1000).toFixed(1)}s · ${finishedAnalysis.message}`,
  );

  const detail = await api(`/api/projects/${project.id}`);
  const media = await api(`/api/projects/${project.id}/media`);
  if (!media.waveform) fail("waveform generated", "analysis/waveform.png missing");
  else ok("waveform generated", "ffmpeg showwavespic output present");

  const candidates = detail.candidates;
  if (candidates.length < 1) throw new Error("Analysis returned no candidates");
  ok("candidates proposed", `${candidates.length} ranked moments`);

  const transcript = await api(`/api/projects/${project.id}/transcript`);
  const words = transcript.transcript.segments.reduce((sum, segment) => sum + segment.words.length, 0);
  ok(
    "transcript stored",
    `${transcript.transcript.segments.length} segments · ${words} word timings · ${transcript.transcript.language ?? "?"}`,
  );
  if (transcript.transcript.provider) ok("transcript provider", transcript.transcript.provider);

  const invalid = candidates.filter(
    (candidate) =>
      candidate.start_ms < 0 ||
      candidate.end_ms > source.duration_ms ||
      candidate.end_ms <= candidate.start_ms,
  );
  if (invalid.length > 0) fail("timestamps within the source", `${invalid.length} invalid candidates`);
  else ok("timestamps within the source", "every candidate sits inside 0..duration");

  const overlapping = candidates.some((candidate, index) =>
    candidates.some(
      (other, otherIndex) =>
        otherIndex > index &&
        candidate.start_ms < other.end_ms &&
        other.start_ms < candidate.end_ms &&
        Math.min(candidate.end_ms, other.end_ms) - Math.max(candidate.start_ms, other.start_ms) > 2000,
    ),
  );
  if (overlapping) fail("candidates distinct", "two candidates overlap by more than 2s");
  else ok("candidates distinct", "no overlapping windows");

  console.log("\n  Ranked moments");
  for (const candidate of candidates.slice(0, 5)) {
    console.log(
      `    #${String(candidate.rank).padStart(2)} ${String(Math.round(candidate.overall)).padStart(3)}/100  ${(
        candidate.duration_ms / 1000
      ).toFixed(1)}s  ${candidate.title}`,
    );
    console.log(`          ${candidate.reason}`);
  }

  const chosen = candidates[0];
  if (!chosen.reason || chosen.reason.length < 20) fail("rank explanation present", "reason too short");
  else ok("rank explanation present", `${chosen.reason.length} chars`);

  const patched = await api(`/api/projects/${project.id}/candidates/${chosen.id}`, {
    method: "PATCH",
    body: JSON.stringify({
      start_ms: chosen.start_ms + 100,
      end_ms: chosen.end_ms,
      title: `${chosen.title} (edited)`,
      decision: "accepted",
    }),
  });
  if (
    patched.candidate.start_ms === chosen.start_ms + 100 &&
    patched.candidate.end_ms === chosen.end_ms &&
    patched.candidate.decision === "accepted"
  ) {
    ok("boundaries and decision persisted", `in ${patched.candidate.start_ms}ms · out ${patched.candidate.end_ms}ms`);
  } else {
    fail("boundaries and decision persisted", JSON.stringify(patched.candidate).slice(0, 200));
  }

  let rejected = false;
  try {
    await api(`/api/projects/${project.id}/candidates/${chosen.id}`, {
      method: "PATCH",
      body: JSON.stringify({ start_ms: chosen.end_ms + 5000 }),
    });
  } catch {
    rejected = true;
  }
  if (rejected) ok("invalid boundary rejected", "server refused an out-of-range span");
  else fail("invalid boundary rejected", "server accepted an impossible span");

  console.log("\n  Rendering…");
  const renderStarted = Date.now();
  const renderResponse = await api(`/api/projects/${project.id}/renders`, {
    method: "POST",
    body: JSON.stringify({
      candidate_id: chosen.id,
      aspect: "9:16",
      resolution: 720,
      framing: "crop",
      caption_preset: "clean",
      captions_enabled: true,
      normalize_audio: true,
    }),
  });
  const finishedRender = await waitForJob(renderResponse.job.id, "render");
  ok("render job completed", `${((Date.now() - renderStarted) / 1000).toFixed(1)}s · ${finishedRender.message}`);

  const render = renderResponse.render;
  await mkdir(outDir, { recursive: true });
  const downloadPath = path.join(outDir, "export.mp4");
  const download = await fetch(`${baseUrl}/api/renders/${render.id}/download`, {
    headers: { "x-clipforge-token": token },
  });
  if (!download.ok) throw new Error(`download failed: HTTP ${download.status}`);
  const bytes = Buffer.from(await download.arrayBuffer());
  await writeFile(downloadPath, bytes);
  ok("export downloaded", `${(bytes.length / 1024 / 1024).toFixed(2)} MB → ${path.relative(repoRoot, downloadPath)}`);

  const exported = probe(downloadPath);
  const video = exported.streams.find((stream) => stream.codec_type === "video");
  const audio = exported.streams.find((stream) => stream.codec_type === "audio");
  if (!video) throw new Error("export has no video stream");
  if (!audio) throw new Error("export has no audio stream");
  ok("export has video and audio", `${video.codec_name} ${video.width}×${video.height} + ${audio.codec_name}`);

  if (video.width !== 720 || video.height !== 1280) fail("vertical 720×1280 output", `${video.width}×${video.height}`);
  else ok("vertical 720×1280 output", "aspect conversion applied");

  const expectedSeconds = (chosen.end_ms - chosen.start_ms) / 1000;
  const actualSeconds = Number(exported.format.duration);
  const delta = Math.abs(actualSeconds - expectedSeconds);
  if (delta > 1.5) fail("duration within tolerance", `expected ≈${expectedSeconds.toFixed(1)}s, got ${actualSeconds.toFixed(2)}s`);
  else ok("duration within tolerance", `≈${expectedSeconds.toFixed(1)}s expected, ${actualSeconds.toFixed(2)}s actual`);

  const framePath = path.join(outDir, "frame.png");
  execFileSync("ffmpeg", [
    "-hide_banner", "-loglevel", "error", "-y",
    "-ss", String(Math.min(2, actualSeconds / 2)),
    "-i", downloadPath,
    "-frames:v", "1",
    framePath,
  ]);
  ok("frame extracted for inspection", path.relative(repoRoot, framePath));

  const projects = await api("/api/projects");
  if (projects.projects.some((entry) => entry.id === project.id)) ok("project persisted", "listed after the run");
  else fail("project persisted", "project missing from the list");

  console.log(
    `\nWorkflow finished in ${((Date.now() - started) / 1000).toFixed(1)}s. Open the frame to confirm the burned-in caption.`,
  );
}

try {
  await main();
} catch (error) {
  console.error(`\nE2E aborted: ${error.message}`);
  checks.push({ name: "workflow", ok: false, detail: error.message });
}

const failed = checks.filter((check) => !check.ok);
console.log(`\n=== E2E summary: ${checks.length - failed.length}/${checks.length} checks passed ===`);
if (failed.length > 0) {
  for (const check of failed) console.log(`  FAILED  ${check.name} — ${check.detail}`);
  process.exit(1);
}
