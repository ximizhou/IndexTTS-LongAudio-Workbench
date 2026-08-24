#!/usr/bin/env bash
set -euo pipefail

ROOT="/data1/ximizhou/indextts-workbench"
URL="http://127.0.0.1:${OFFICIAL_PORT:-7860}"
if ! "$ROOT/scripts/start-official-webui.sh"; then
  command -v notify-send >/dev/null && notify-send "IndexTTS 官方 WebUI" "另一套界面可能正在运行；先停止工作台或查看 $ROOT/logs/official-webui.log"
  exit 1
fi
nohup xdg-open "$URL" >/dev/null 2>&1 &
