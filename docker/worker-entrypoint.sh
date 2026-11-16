#!/bin/sh
# Claim a freshly mounted data volume, then run the service unprivileged.
set -eu

DATA_DIR="${CLIPFORGE_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  # Only the mount root needs fixing: the app creates its own subdirectories as
  # the unprivileged user afterwards. A recursive chown here would walk gigabytes
  # of media and models on every restart.
  chown clipforge:clipforge "$DATA_DIR" 2>/dev/null || true
  exec gosu clipforge "$@"
fi

exec "$@"
