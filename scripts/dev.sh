#!/usr/bin/env bash
# Start the local worker and the web app together; Ctrl+C stops both.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

node scripts/worker.mjs dev &
worker_pid=$!

cleanup() {
  if kill -0 "$worker_pid" 2>/dev/null; then
    kill "$worker_pid" 2>/dev/null || true
    wait "$worker_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

pnpm --filter @clipforge/web dev
