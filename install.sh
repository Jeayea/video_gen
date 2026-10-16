#!/usr/bin/env bash
# 一键安装：找 Python 3.11+ → 建 .venv → 按锁定版本（带哈希）装依赖 → 生成 .env → 自检。
# 可以重复跑：已装的不会重装，已有的 .env 不会被覆盖。
#
# 用法：
#   ./install.sh          # 运行环境（含便携 ffmpeg）
#   ./install.sh --dev    # 再加上 pytest / ruff
#   PYTHON=python3.12 ./install.sh   # 指定解释器
set -euo pipefail
cd "$(dirname "$0")"

LOCK=requirements.lock
for arg in "$@"; do
  case "$arg" in
    --dev) LOCK=requirements-dev.lock ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

# ── 1. Python 3.11+ ──────────────────────────────────────────
find_python() {
  local cands=("${PYTHON:-}" python3.13 python3.12 python3.11 python3)
  for c in "${cands[@]}"; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

if [ ! -x .venv/bin/python ]; then
  if ! PY="$(find_python)"; then
    echo "需要 Python 3.11 或更新的版本。macOS：brew install python@3.12" >&2
    exit 1
  fi
  echo "== 用 $PY 创建 .venv"
  "$PY" -m venv .venv
fi

# ── 2. 依赖（锁定版本 + 哈希校验）───────────────────────────
echo "== 安装依赖（${LOCK}）"
.venv/bin/python -m pip install --quiet --disable-pip-version-check --require-hashes -r "$LOCK"
.venv/bin/python -m pip install --quiet --disable-pip-version-check --no-deps -e .

# ── 3. .env ──────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "== 已生成 .env（最小模板，完整配置见 .env.full.example）"
fi

# key 还空着、又是交互终端时，问一次（不回显，不进 shell 历史，也不出现在进程参数里）
if grep -qE '^OPENROUTER_API_KEY=[[:space:]]*$' .env && [ -t 0 ]; then
  printf '粘贴 OPENROUTER_API_KEY（https://openrouter.ai/keys，直接回车跳过）：'
  read -rs KEY
  echo
  if [ -n "$KEY" ]; then
    VIDEOGEN_NEW_KEY="$KEY" .venv/bin/python - <<'PY'
import os
from pathlib import Path

env = Path(".env")
key = os.environ["VIDEOGEN_NEW_KEY"].strip()
lines = env.read_text(encoding="utf-8").splitlines()
lines = [f"OPENROUTER_API_KEY={key}" if ln.startswith("OPENROUTER_API_KEY=") else ln for ln in lines]
env.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
    unset KEY
    echo "== key 已写入 .env"
  fi
fi

# ── 4. 自检（不联网、不花钱；没通过也不中断安装）──────────────
echo
.venv/bin/python -m videogen.cli doctor || true

echo
echo "装好了。启动：./run.sh   （或 .venv/bin/videogen serve）"
echo "然后打开 http://127.0.0.1:8001"
