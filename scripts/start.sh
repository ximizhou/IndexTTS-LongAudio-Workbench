#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PY="${INDEXTTS_PYTHON:-/data1/ximizhou/envs/conda/indextts/bin/python}"
MODEL_DIR="${INDEXTTS_MODEL_DIR:-/data1/ximizhou/indextts/checkpoints}"
UPSTREAM_DIR="${INDEXTTS_SOURCE_DIR:-/data1/ximizhou/indextts}"
FFMPEG="${INDEXTTS_WORKBENCH_FFMPEG:-/data1/ximizhou/envs/conda/indextts/bin/ffmpeg}"
PORT="${PORT:-8082}"
MIN_FREE_MIB="${MIN_FREE_MIB:-8192}"
PREFERRED_GPU_ID="${PREFERRED_GPU_ID:-3}"

if [[ ! -x "$ENV_PY" ]]; then echo "missing runtime: $ENV_PY" >&2; exit 1; fi
if [[ ! -d "$MODEL_DIR" || ! -f "$MODEL_DIR/config.yaml" ]]; then echo "missing IndexTTS model: $MODEL_DIR" >&2; exit 1; fi
if [[ ! -d "$UPSTREAM_DIR/indextts" ]]; then echo "missing IndexTTS source: $UPSTREAM_DIR" >&2; exit 1; fi
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then echo "invalid PORT: $PORT" >&2; exit 1; fi
if ! [[ "$MIN_FREE_MIB" =~ ^[0-9]+$ ]]; then echo "invalid MIN_FREE_MIB: $MIN_FREE_MIB" >&2; exit 1; fi
if ! [[ "$PREFERRED_GPU_ID" =~ ^[0-9]+$ ]]; then echo "invalid PREFERRED_GPU_ID: $PREFERRED_GPU_ID" >&2; exit 1; fi

mkdir -p "$ROOT/logs" "$ROOT/tasks" "$ROOT/voices" "$ROOT/run"
PID_FILE="$ROOT/run/server.pid"
OFFICIAL_PID_FILE="$ROOT/run/official-webui.pid"
if [[ -f "$OFFICIAL_PID_FILE" ]]; then
  OFFICIAL_PID="$(cat "$OFFICIAL_PID_FILE" 2>/dev/null || true)"
  if [[ "$OFFICIAL_PID" =~ ^[0-9]+$ ]] && kill -0 "$OFFICIAL_PID" 2>/dev/null; then
    echo "official IndexTTS WebUI is running (PID $OFFICIAL_PID); stop it before starting the workbench" >&2
    exit 1
  fi
  rm -f -- "$OFFICIAL_PID_FILE"
fi
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "already running: $(cat "$PID_FILE")"
  exit 0
fi
if ss -ltnH 2>/dev/null | grep -Eq ":${PORT}[[:space:]]"; then
  echo "port $PORT is already listening" >&2
  exit 1
fi

if ! GPU_ID="$(
  nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits |
    "$ENV_PY" "$ROOT/scripts/select_gpu.py" \
      --min-free-mib "$MIN_FREE_MIB" --preferred-gpu-id "$PREFERRED_GPU_ID"
)"; then
  GPU_ID=""
fi
if [[ -z "$GPU_ID" ]]; then echo "no GPU has at least ${MIN_FREE_MIB} MiB free" >&2; exit 1; fi

if [[ "$GPU_ID" == "$PREFERRED_GPU_ID" ]]; then
  echo "selected preferred GPU $GPU_ID"
else
  echo "preferred GPU $PREFERRED_GPU_ID unavailable; selected fallback GPU $GPU_ID"
fi
cd "$ROOT"
nohup env CUDA_VISIBLE_DEVICES="$GPU_ID" \
  INDEXTTS_MODEL_DIR="$MODEL_DIR" INDEXTTS_REFERENCE_DIR="${INDEXTTS_REFERENCE_DIR:-$UPSTREAM_DIR/examples}" \
  INDEXTTS_WORKBENCH_FFMPEG="$FFMPEG" INDEXTTS_QWEN_EMO="${INDEXTTS_QWEN_EMO:-1}" \
  PYTHONPATH="$UPSTREAM_DIR:$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  "$ENV_PY" -m uvicorn app.web:app --host 127.0.0.1 --port "$PORT" \
  >> "$ROOT/logs/server.log" 2>&1 < /dev/null &
echo $! > "$PID_FILE"
PID="$(cat "$PID_FILE")"
for _ in $(seq 1 90); do
  if ! kill -0 "$PID" 2>/dev/null; then echo "server exited; see $ROOT/logs/server.log" >&2; rm -f "$PID_FILE"; exit 1; fi
  if "$ENV_PY" - "$PORT" <<'PY'
import sys
import urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=1) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
  then echo "started pid $PID on 127.0.0.1:$PORT"; exit 0; fi
  sleep 1
done
echo "server did not become healthy within 90 seconds; see $ROOT/logs/server.log" >&2
kill "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
exit 1
