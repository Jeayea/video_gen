#!/usr/bin/env bash
# 默认只监听本机，避免无鉴权接口暴露到局域网
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"
RELOAD="${RELOAD:-1}"

args=(--host "$HOST" --port "$PORT")
if [ "$RELOAD" != "0" ]; then
  args+=(--reload)
fi

echo "videogen → http://${HOST}:${PORT}"
exec .venv/bin/python -m uvicorn videogen.main:app "${args[@]}"
