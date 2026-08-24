#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="${INDEXTTS_SOURCE_DIR:-/data1/ximizhou/indextts}"
PID_FILE="$ROOT/run/official-webui.pid"
if [[ ! -f "$PID_FILE" ]]; then echo "official WebUI not running"; exit 0; fi
PID="$(cat "$PID_FILE")"
if ! [[ "$PID" =~ ^[0-9]+$ ]]; then echo "invalid PID file: $PID_FILE" >&2; exit 1; fi
if kill -0 "$PID" 2>/dev/null; then
  PROCESS_CWD="$(readlink -f "/proc/$PID/cwd" 2>/dev/null || true)"
  PROCESS_CMD="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
  if [[ "$PROCESS_CWD" != "$UPSTREAM_DIR" || "$PROCESS_CMD" != *"$UPSTREAM_DIR/webui.py"* ]]; then
    echo "refusing to stop PID $PID: it is not the official IndexTTS WebUI" >&2
    exit 1
  fi
  kill "$PID"
  for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  if kill -0 "$PID" 2>/dev/null; then echo "PID $PID did not stop within 60 seconds; PID file retained" >&2; exit 1; fi
fi
rm -f -- "$PID_FILE"
echo "stopped official WebUI $PID"
