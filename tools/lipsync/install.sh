#!/usr/bin/env bash
# 装「逐帧口型」用的独立环境 + 模型权重。
#
# 为什么要独立环境：Wav2Lip 的 ONNX 版要 onnxruntime / numpy / opencv（约 120MB），
# 而主环境 videogen 只依赖 fastapi/pydantic/httpx/edge-tts。把 ML 依赖塞进主环境
# 会让「只跑剧本/配音」的人也背上几百 MB，所以分开装、按进程调用。
#
# 用法：
#   tools/lipsync/install.sh            # 装到 <repo>/.venv-lipsync
#   tools/lipsync/install.sh /tmp/xx    # 装到指定目录
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="${1:-$ROOT/.venv-lipsync}"
MODELS="$ROOT/assets/lipsync"
PYTHON="${PYTHON:-python3}"

echo "== 仓库根目录: $ROOT"
echo "== 环境目录  : $VENV"
echo "== 模型目录  : $MODELS"

if [ ! -d "$VENV" ]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
echo "== 安装 onnxruntime / numpy / opencv-python-headless"
"$VENV/bin/python" -m pip install --quiet onnxruntime numpy opencv-python-headless

mkdir -p "$MODELS"
fetch() {  # fetch <url> <target>
  if [ -s "$2" ]; then
    echo "== 已存在，跳过：$(basename "$2")"
    return 0
  fi
  echo "== 下载 $(basename "$2")"
  curl -fL --retry 3 --max-time 900 -o "$2.part" "$1"
  mv "$2.part" "$2"
}

# 人脸检测（YuNet，230KB，opencv_zoo 官方）
fetch "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
      "$MODELS/face_detection_yunet_2023mar.onnx"
# Wav2Lip 的 ONNX 权重（145MB；GAN 版嘴部更清晰，默认用它）
fetch "https://huggingface.co/bluefoxcreation/Wav2lip-Onnx/resolve/main/wav2lip_gan.onnx" \
      "$MODELS/wav2lip_gan.onnx"
# 非 GAN 版更平滑、更少伪影，想要稳一点可以换用（可选，不装也能跑）
# fetch "https://huggingface.co/bluefoxcreation/Wav2lip-Onnx/resolve/main/wav2lip.onnx" \
#       "$MODELS/wav2lip.onnx"

echo
echo "== 自检（跑一个 1 秒的空镜头）"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ffmpeg -v error -y -f lavfi -i "testsrc2=s=320x320:d=1:r=12" \
  -f lavfi -i "sine=frequency=440:duration=1" -shortest \
  -c:v libx264 -pix_fmt yuv420p -c:a aac "$TMP/in.mp4"
ffmpeg -v error -y -f lavfi -i "sine=frequency=440:duration=1" "$TMP/dub.wav"
"$VENV/bin/python" "$ROOT/tools/lipsync/wav2lip_onnx.py" \
  --video "$TMP/in.mp4" --audio "$TMP/dub.wav" --out "$TMP/out.mp4" \
  --model "$MODELS/wav2lip_gan.onnx" --detector "$MODELS/face_detection_yunet_2023mar.onnx"

echo
echo "装好了。在 .env 里打开："
echo "  LIPSYNC_ENGINE=wav2lip-onnx"
echo "  LIPSYNC_PYTHON=$VENV/bin/python"
