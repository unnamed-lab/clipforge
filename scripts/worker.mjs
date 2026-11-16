#!/usr/bin/env node
/**
 * Cross-platform launcher for the Python worker.
 *
 *   node scripts/worker.mjs dev     start uvicorn with reload
 *   node scripts/worker.mjs test    run pytest (extra args after `--`)
 *   node scripts/worker.mjs python  print the interpreter that would be used
 *
 * The interpreter is resolved from CLIPFORGE_PYTHON, then the project venv,
 * then PATH. Nothing here talks to the network.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const workerDir = path.join(repoRoot, "apps", "worker");
const isWindows = process.platform === "win32";

const VENV_PYTHONS = isWindows
  ? [path.join(workerDir, ".venv", "Scripts", "python.exe")]
  : [path.join(workerDir, ".venv", "bin", "python3"), path.join(workerDir, ".venv", "bin", "python")];

function resolvePython() {
  const configured = process.env.CLIPFORGE_PYTHON?.trim();
  if (configured) return configured;
  for (const candidate of VENV_PYTHONS) {
    if (existsSync(candidate)) return candidate;
  }
  return isWindows ? "python" : "python3";
}

const python = resolvePython();
const [command = "dev", ...rest] = process.argv.slice(2);

function run(args, extraEnv = {}) {
  const child = spawn(python, args, {
    cwd: workerDir,
    stdio: "inherit",
    env: { ...process.env, PYTHONPATH: path.join(workerDir, "src"), ...extraEnv },
    windowsHide: false,
  });

  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => {
      if (!child.killed) child.kill(signal);
    });
  }

  child.on("error", (error) => {
    if (error.code === "ENOENT") {
      console.error(
        `\nCould not find a Python interpreter at "${python}".\n` +
          `Create the worker environment first:\n\n` +
          `  cd apps/worker\n  python -m venv .venv\n  .venv\\Scripts\\pip install -e ".[dev]"\n\n` +
          `Or set CLIPFORGE_PYTHON to an existing interpreter (3.11-3.13).\n`,
      );
    } else {
      console.error(`Failed to start the worker: ${error.message}`);
    }
    process.exit(1);
  });

  child.on("exit", (code) => process.exit(code ?? 0));
}

switch (command) {
  case "dev":
    console.log(`[worker] ${python} -m clipforge (reload)`);
    run(["-m", "clipforge"], { CLIPFORGE_RELOAD: "1" });
    break;
  case "start":
    console.log(`[worker] ${python} -m clipforge`);
    run(["-m", "clipforge"]);
    break;
  case "test": {
    const passthrough = rest[0] === "--" ? rest.slice(1) : rest;
    run(["-m", "pytest", ...passthrough]);
    break;
  }
  case "lint":
    console.log(`[worker] ${python} -m ruff check src tests`);
    run(["-m", "ruff", "check", "src", "tests"]);
    break;
  case "python":
    console.log(python);
    break;
  default:
    console.error(`Unknown command "${command}". Use: dev | start | test | lint | python`);
    process.exit(2);
}
