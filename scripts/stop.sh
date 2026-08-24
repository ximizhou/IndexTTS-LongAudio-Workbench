#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/server.pid"
OFFICIAL_PID_FILE="$ROOT/run/official-webui.pid"
if [[ -f "$OFFICIAL_PID_FILE" ]]; then
  OFFICIAL_PID="$(cat "$OFFICIAL_PID_FILE" 2>/dev/null || true)"
  if [[ "$OFFICIAL_PID" =~ ^[0-9]+$ ]] && kill -0 "$OFFICIAL_PID" 2>/dev/null; then
    echo "official IndexTTS WebUI is running (PID $OFFICIAL_PID); stop it before replacing services" >&2
    exit 1
  fi
fi
if [[ ! -f "$PID_FILE" ]]; then echo "not running"; exit 0; fi
PID="$(cat "$PID_FILE")"
if ! [[ "$PID" =~ ^[0-9]+$ ]]; then echo "invalid PID file: $PID_FILE" >&2; exit 1; fi
if kill -0 "$PID" 2>/dev/null; then
  PROCESS_CWD="$(readlink -f "/proc/$PID/cwd" 2>/dev/null || true)"
  PROCESS_CMD="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
  if [[ "$PROCESS_CWD" != "$ROOT" || "$PROCESS_CMD" != *"uvicorn app.web:app"* ]]; then
    echo "refusing to stop PID $PID: it is not the IndexTTS Workbench process in $ROOT" >&2
    exit 1
  fi
  kill "$PID"
  for _ in $(seq 1 30); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  if kill -0 "$PID" 2>/dev/null; then echo "PID $PID did not stop within 30 seconds; PID file retained" >&2; exit 1; fi
fi
rm -f "$PID_FILE"
echo "stopped $PID"
