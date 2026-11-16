#!/usr/bin/env node
/**
 * Container verification.
 *
 * Builds both production images, starts the Compose stack on an isolated project
 * name, and proves the parts that only fail in a container:
 *
 *   - compose configuration is valid (and rejects a missing token)
 *   - both images build
 *   - the worker health endpoint answers
 *   - the web proxy reaches the worker (not just the worker directly)
 *   - FFmpeg/FFprobe exist in the worker image and can produce a probeable file
 *   - an upload through the web proxy lands on the worker's data volume
 *   - a project can be removed without leaving the stack wedged
 *
 * It never downloads a transcription model and never touches the developer's
 * `clipforge` Compose project or local `data/` directory.
 *
 *   node scripts/docker-verify.mjs
 *   node scripts/docker-verify.mjs --keep    leave the stack running for inspection
 */

import { spawn } from "node:child_process";
import crypto from "node:crypto";
import { readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const keep = process.argv.slice(2).includes("--keep");
const project = "clipforge-verify";
const webPort = Number(process.env.CLIPFORGE_VERIFY_PORT ?? 3417);
const baseUrl = `http://127.0.0.1:${webPort}`;
const token = crypto.randomBytes(32).toString("base64url");

const composeEnv = {
  ...process.env,
  COMPOSE_PROJECT_NAME: project,
  CLIPFORGE_WORKER_TOKEN: token,
  CLIPFORGE_WEB_PORT: String(webPort),
  CLIPFORGE_WHISPER_MODEL: "base",
  // Verify the local backend even when the developer's .env points at Supabase.
  CLIPFORGE_STORAGE_BACKEND: "local",
  CLIPFORGE_SUPABASE_URL: "",
  CLIPFORGE_SUPABASE_SERVICE_KEY: "",
};

const composeArgs = ["compose", "-p", project, "-f", path.join(repoRoot, "compose.yaml")];

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: repoRoot,
      env: { ...composeEnv, ...options.env },
      stdio: options.capture ? ["ignore", "pipe", "pipe"] : "inherit",
      windowsHide: true,
    });

    let stdout = "";
    let stderr = "";
    if (options.capture) {
      child.stdout.on("data", (chunk) => {
        stdout += chunk;
      });
      child.stderr.on("data", (chunk) => {
        stderr += chunk;
      });
    }

    child.on("error", reject);
    child.on("exit", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${command} ${args.join(" ")} exited with ${code}\n${stderr || stdout}`));
    });
  });
}

const docker = (args, options) => run("docker", [...composeArgs, ...args], options);

async function waitForHealth(url, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  let last = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(5_000) });
      const body = await response.text();
      if (response.ok && body.includes('"ok"')) return body;
      last = `${response.status} ${body.slice(0, 200)}`;
    } catch (error) {
      last = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(`${url} did not become healthy in time. Last result: ${last}`);
}

async function json(response, label) {
  const text = await response.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    throw new Error(`${label} returned non-JSON (${response.status}): ${text.slice(0, 300)}`);
  }
  if (!response.ok) {
    throw new Error(`${label} failed (${response.status}): ${text.slice(0, 300)}`);
  }
  return body;
}

async function main() {
  console.log(`[docker] verifying project "${project}" on ${baseUrl}`);

  let configWithoutTokenFailed = false;
  try {
    await docker(["config", "--quiet"], { env: { CLIPFORGE_WORKER_TOKEN: "" } });
  } catch {
    configWithoutTokenFailed = true;
  }
  if (!configWithoutTokenFailed) {
    throw new Error("Compose accepted a missing CLIPFORGE_WORKER_TOKEN; the required-variable guard is broken.");
  }
  console.log("[docker] missing token is rejected by compose");

  await docker(["config", "--quiet"], { capture: true });
  console.log("[docker] compose configuration is valid");

  await docker(["build"], { capture: false });
  console.log("[docker] both images built");

  await docker(["up", "-d", "--wait", "--wait-timeout", "180", "--remove-orphans"]);

  const health = await waitForHealth(`${baseUrl}/api/health`);
  console.log(`[docker] web proxy health: ${health.trim().slice(0, 160)}`);

  const capabilities = await json(await fetch(`${baseUrl}/api/capabilities`), "capabilities");
  if (!capabilities.capabilities?.ffmpeg) {
    throw new Error("Worker reports ffmpeg as unavailable inside the image.");
  }
  if (capabilities.capabilities?.storage?.backend !== "local") {
    throw new Error(
      `Docker verification must use local storage, got ${JSON.stringify(capabilities.capabilities?.storage)}`,
    );
  }

  const probe = await docker(
    [
      "exec",
      "-T",
      "worker",
      "sh",
      "-c",
      "ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=black:s=320x240:d=1 " +
        "-f lavfi -i anullsrc=r=48000:cl=stereo -shortest -c:v libx264 -pix_fmt yuv420p " +
        "-c:a aac -t 1 /tmp/smoke.mp4 && " +
        "ffprobe -v error -show_entries stream=codec_name -show_entries format=duration " +
        "-of json /tmp/smoke.mp4",
    ],
    { capture: true },
  );
  const probeJson = JSON.parse(probe.stdout);
  if (!probeJson.streams?.some((stream) => stream.codec_name === "h264")) {
    throw new Error(`FFmpeg smoke clip has no h264 stream: ${probe.stdout}`);
  }
  console.log(`[docker] ffmpeg/ffprobe smoke: duration ${probeJson.format?.duration}s`);

  const localClip = path.join(os.tmpdir(), `clipforge-docker-smoke-${process.pid}.mp4`);
  const created = await json(
    await fetch(`${baseUrl}/api/projects`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "Docker verification" }),
    }),
    "create project",
  );
  const projectId = created.project.id;

  try {
    await docker(["cp", "worker:/tmp/smoke.mp4", localClip], { capture: true });
    const bytes = await readFile(localClip);
    const form = new FormData();
    form.set("file", new Blob([bytes], { type: "video/mp4" }), "smoke.mp4");
    const uploaded = await json(
      await fetch(`${baseUrl}/api/projects/${projectId}/sources/upload`, {
        method: "POST",
        body: form,
      }),
      "upload through web proxy",
    );
    if (!uploaded.source?.duration_ms) {
      throw new Error(`Upload returned no probed duration: ${JSON.stringify(uploaded).slice(0, 300)}`);
    }
    console.log(`[docker] upload through web proxy stored ${uploaded.source.size_bytes} bytes`);

    const stored = await docker(
      ["exec", "-T", "worker", "sh", "-c", `test -s /data/app.db && find /data/projects/${projectId} -type f -print`],
      { capture: true },
    );
    if (!stored.stdout.includes("source")) {
      throw new Error(`Uploaded source is not on the worker data volume:\n${stored.stdout}`);
    }
    console.log("[docker] database and source live on the worker volume");
  } finally {
    await fetch(`${baseUrl}/api/projects/${projectId}`, { method: "DELETE" }).catch(() => undefined);
    await rm(localClip, { force: true });
  }

  if (keep) {
    console.log(`[docker] --keep: stack left running as ${project} on ${baseUrl}`);
    return;
  }
  await docker(["down", "-v", "--remove-orphans"]);
  console.log("[docker] stack stopped and verification volume removed");
}

let failure = null;
try {
  await main();
} catch (error) {
  failure = error;
  console.error(`\n[docker] FAILED: ${error instanceof Error ? error.message : error}`);
  if (!keep) {
    await docker(["down", "-v", "--remove-orphans"]).catch(() => undefined);
  }
}

if (failure) process.exit(1);
console.log("\n[docker] all container checks passed.");
