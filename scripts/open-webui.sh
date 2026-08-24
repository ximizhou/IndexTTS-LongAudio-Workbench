#!/usr/bin/env bash
set -euo pipefail

ROOT="/data1/ximizhou/indextts-workbench"
URL="http://127.0.0.1:8082"

if ! "$ROOT/scripts/start.sh"; then
  command -v notify-send >/dev/null && notify-send "IndexTTS" "启动失败，请查看 $ROOT/logs/server.log"
  exit 1
fi

nohup xdg-open "$URL" >/dev/null 2>&1 &
