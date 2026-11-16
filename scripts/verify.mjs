#!/usr/bin/env node
/**
 * Verification gate. Runs every check that can be automated locally, in the order
 * that fails fastest, and prints a summary. Exits non-zero if any step fails.
 *
 *   node scripts/verify.mjs            typecheck, lint, unit tests, worker tests
 *   node scripts/verify.mjs --build    also run the production Next.js build
 *   node scripts/verify.mjs --docker   also build images and smoke-test Compose
 *   node scripts/verify.mjs --slow     include worker tests marked slow
 */

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const isWindows = process.platform === "win32";
const pnpm = isWindows ? "pnpm.cmd" : "pnpm";

const flags = new Set(process.argv.slice(2));
const includeBuild = flags.has("--build");
const includeDocker = flags.has("--docker");
const includeSlow = flags.has("--slow");

/** @type {{name: string, command: string, args: string[], env?: Record<string,string>}[]} */
const steps = [
  {
    name: "TypeScript",
    command: pnpm,
    args: ["--filter", "@clipforge/web", "typecheck"],
  },
  {
    name: "Web lint",
    command: pnpm,
    args: ["--filter", "@clipforge/web", "lint"],
  },
  {
    name: "Worker lint",
    command: "node",
    args: ["scripts/worker.mjs", "lint"],
  },
  {
    name: "Shared tests",
    command: pnpm,
    args: ["--filter", "@clipforge/shared", "test"],
  },
  {
    name: "Web tests",
    command: pnpm,
    args: ["--filter", "@clipforge/web", "test"],
  },
  {
    name: includeSlow ? "Worker tests (all)" : "Worker tests (fast)",
    command: "node",
    args: ["scripts/worker.mjs", "test", "--", ...(includeSlow ? [] : ["-m", "not slow"])],
  },
];

if (includeBuild) {
  steps.push({
    name: "Production build",
    command: pnpm,
    args: ["--filter", "@clipforge/web", "build"],
  });
}

if (includeDocker) {
  steps.push({
    name: "Docker verification",
    command: "node",
    args: ["scripts/docker-verify.mjs"],
  });
}

const results = [];

function runStep(step) {
  const started = Date.now();
  console.log(`\n=== ${step.name} ===`);
  return new Promise((resolve) => {
    const child = spawn(step.command, step.args, {
      cwd: repoRoot,
      stdio: "inherit",
      env: { ...process.env, ...step.env },
      // pnpm is a .cmd shim on Windows and needs a shell. Node is a real
      // executable; running it through the shell would split arguments that
      // contain spaces, such as pytest's `-m "not slow"`.
      shell: isWindows && step.command === pnpm,
    });
    child.on("error", (error) => {
      results.push({ name: step.name, ok: false, ms: Date.now() - started, detail: error.message });
      resolve();
    });
    child.on("exit", (code) => {
      results.push({ name: step.name, ok: code === 0, ms: Date.now() - started });
      resolve();
    });
  });
}

for (const step of steps) {
  await runStep(step);
}

console.log("\n=== Summary ===");
for (const result of results) {
  const status = result.ok ? "PASS" : "FAIL";
  console.log(`${status}  ${result.name.padEnd(26)} ${(result.ms / 1000).toFixed(1)}s${result.detail ? `  ${result.detail}` : ""}`);
}

const failed = results.filter((result) => !result.ok);
if (failed.length > 0) {
  console.error(`\n${failed.length} step(s) failed.`);
  process.exit(1);
}
console.log("\nAll checks passed.");
