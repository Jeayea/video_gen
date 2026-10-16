#!/usr/bin/env bash
# 默认只监听本机，避免无鉴权接口暴露到局域网
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"
RELOAD="${RELOAD:-1}"

# 第一次运行：还没装就先装（建 venv、装锁定版本的依赖、生成 .env）
if [ ! -x .venv/bin/python ]; then
  ./install.sh
fi

args=(serve --host "$HOST" --port "$PORT")
if [ "$RELOAD" != "0" ]; then
  args+=(--reload)
fi

exec .venv/bin/python -m videogen.cli "${args[@]}"
