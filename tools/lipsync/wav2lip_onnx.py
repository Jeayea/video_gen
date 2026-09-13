#!/usr/bin/env python3
"""Wav2Lip 逐帧口型后处理（ONNX 版，**不需要 torch**）。

为什么是 ONNX 而不是原版 Wav2Lip 仓库：
- 原版要 torch + librosa + scipy + opencv，装完 3GB 起，还得从 Google Drive 拖权重；
- 这里只要 onnxruntime + numpy + opencv（约 120MB），在 Apple Silicon 的 CPU 上
  5 秒 1080×1920 的片段大约 20~40 秒，够用。

这个脚本**只做画面**：`--audio` 只用来算 mel（口型的驱动信号），输出的 mp4 没有音轨。
音轨由调用方（`videogen.services.lipsync_service`）负责把原片段音轨接回去，
这样「环境音」链路不受影响，引擎也可以换成 LatentSync / MuseTalk 而不改上层。

用法：
    .venv-lipsync/bin/python tools/lipsync/wav2lip_onnx.py \\
        --video clip.mp4 --audio dub.wav --out synced.mp4 --report synced.json

与原版推理的三处有意差异（都是为了本项目「配音比画面短」的实际场景）：
1. **音频尾巴补静音**：原版在 mel 不够时重复最后一帧，配音只有 1.3 秒、镜头 5 秒时
   嘴会一直停在最后一个口型上；这里补零（归一化后正好是静音）→ 说完就闭嘴。
2. **只贴回下半张脸**：原版把整个 96×96 的人脸贴回原图（1080p 下眼睛会糊），
   这里只把嘴/下巴区域羽化贴回，眼睛保持原分辨率。
3. **人脸框做时序平滑 + 防跳变**：避免单帧检测抖动导致嘴部区域跳动。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

# 音频前端单独成模块（纯 numpy），这样它能脱离 cv2/onnxruntime 单独验证
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wav2lip_mel import (
    SAMPLE_RATE,
    mel_chunks_for_frames,
    mel_spectrogram,
)

IMG_SIZE = 96

DEFAULT_MODEL = "wav2lip_gan.onnx"
DEFAULT_DETECTOR = "face_detection_yunet_2023mar.onnx"


# ── ffmpeg I/O（流式，避免整段放进内存）────────────────────────


def ffmpeg_exe() -> str:
    return "ffmpeg"


def ffprobe_video(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    num, _, den = str(stream.get("r_frame_rate", "24/1")).partition("/")
    fps = float(num) / float(den or 1)
    return int(stream["width"]), int(stream["height"]), fps


def read_audio_16k(path: Path) -> np.ndarray:
    raw = subprocess.run(
        [
            ffmpeg_exe(), "-v", "error", "-i", str(path),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        ],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class FrameReader:
    """流式读原始帧（默认 bgr24，与 cv2 / Wav2Lip 的训练口径一致）。"""

    def __init__(self, path: Path, width: int, height: int, *, scale: float = 1.0):
        self.width = max(2, round(width * scale / 2) * 2)
        self.height = max(2, round(height * scale / 2) * 2)
        self.stride = self.width * self.height * 3
        vf = f"scale={self.width}:{self.height}:flags=area" if scale != 1.0 else "null"
        self.proc = subprocess.Popen(
            [
                ffmpeg_exe(), "-v", "error", "-i", str(path),
                "-vf", vf, "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=self.stride * 4,
        )
        #: 是否把整段读完了（提前 break 时 ffmpeg 会因管道断开报错，不算失败）
        self.exhausted = False

    def frames(self):
        assert self.proc.stdout is not None
        while True:
            buf = self.proc.stdout.read(self.stride)
            if not buf or len(buf) < self.stride:
                self.exhausted = True
                break
            # 从 bytes 建的 ndarray 是只读的，贴回时写不进去 —— 读出来就复制一份
            yield np.frombuffer(buf, dtype=np.uint8).reshape(
                self.height, self.width, 3
            ).copy()

    def close(self) -> None:
        if not self.exhausted and self.proc.poll() is None:
            self.proc.kill()  # 调用方提前停止 → 主动收掉，别等 ffmpeg 撞 Broken pipe
        if self.proc.stdout:
            self.proc.stdout.close()
        self.proc.wait()
        if self.exhausted and self.proc.returncode not in (0, None):
            stderr = (self.proc.stderr.read() if self.proc.stderr else b"").decode(
                "utf-8", "replace"
            )
            raise RuntimeError(f"ffmpeg 读帧失败：{stderr.strip()[-300:]}")


class FrameWriter:
    """流式写原始帧 → h264 mp4（无音轨）。"""

    def __init__(
        self, path: Path, width: int, height: int, fps: float, *, crf: int, preset: str
    ):
        self.proc = subprocess.Popen(
            [
                ffmpeg_exe(), "-y", "-v", "error",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", f"{fps:.6f}", "-i", "-",
                "-an", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
            ],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def write(self, frame: np.ndarray) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()
        if self.proc.returncode != 0:
            stderr = (self.proc.stderr.read() if self.proc.stderr else b"").decode(
                "utf-8", "replace"
            )
            raise RuntimeError(f"ffmpeg 写视频失败：{stderr.strip()[-300:]}")


# ── 人脸框 ───────────────────────────────────────────────────


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _nearest_to(previous: np.ndarray):
    """按「离上一帧的人脸中心多远」排序的 key 函数（显式绑定，避免闭包捕获循环变量）。"""
    center_x = (previous[0] + previous[2]) / 2
    center_y = (previous[1] + previous[3]) / 2

    def key(item):
        box = item[0]
        return float(
            np.hypot((box[0] + box[2]) / 2 - center_x, (box[1] + box[3]) / 2 - center_y)
        )

    return key


def detect_faces(
    video: Path, width: int, height: int, *, detector_path: Path, detect_width: int,
    score_threshold: float = 0.6,
) -> tuple[np.ndarray, int]:
    """逐帧检测人脸，返回 (boxes[N,4], 成功帧数)。检测不到的帧先留 0，稍后补齐。"""
    scale = min(1.0, detect_width / max(width, 1))
    reader = FrameReader(video, width, height, scale=scale)
    detector = cv2.FaceDetectorYN.create(
        str(detector_path), "", (reader.width, reader.height), score_threshold, 0.3, 50
    )
    detector.setInputSize((reader.width, reader.height))

    boxes: list[np.ndarray] = []
    found = 0
    previous: np.ndarray | None = None
    inverse = 1.0 / scale if scale else 1.0
    try:
        for frame in reader.frames():
            _, faces = detector.detect(frame)
            best: np.ndarray | None = None
            if faces is not None and len(faces):
                candidates = []
                for face in faces:
                    x, y, w, h = face[:4]
                    score = float(face[-1])
                    box = (x * inverse, y * inverse, (x + w) * inverse, (y + h) * inverse)
                    candidates.append((box, score, w * h))
                # 先按面积筛掉明显的小误检，再选「离上一帧最近」的，保证跟踪稳定
                max_area = max(c[2] for c in candidates)
                strong = [c for c in candidates if c[2] >= max_area * 0.4]
                if previous is not None:
                    strong.sort(key=_nearest_to(previous))
                else:
                    strong.sort(key=lambda item: item[2], reverse=True)
                best = np.array(strong[0][0], dtype=np.float64)
            boxes.append(best if best is not None else np.zeros(4, dtype=np.float64))
            if best is not None:
                found += 1
                previous = best
    finally:
        reader.close()
    return np.array(boxes, dtype=np.float64), found


def smooth_boxes(
    boxes: np.ndarray, *, window: int = 5, max_jump: float = 0.45
) -> np.ndarray:
    """补齐漏检 + 滑动平均 + 抑制单帧跳变。"""
    n = len(boxes)
    if n == 0:
        return boxes
    sizes = np.array([max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1]) for b in boxes])
    positive = sizes[sizes > 0]
    typical = float(np.median(positive)) if len(positive) else 0.0

    # 1) 漏检用前一个框填；一个都没有就用中位数框
    filled = boxes.copy()
    last = None
    for i in range(n):
        if sizes[i] > 0:
            last = filled[i].copy()
        elif last is not None:
            filled[i] = last
    last = None
    for i in range(n - 1, -1, -1):
        if sizes[i] > 0 or (filled[i] != 0).any():
            if sizes[i] > 0:
                last = filled[i].copy()
            elif last is not None:
                filled[i] = last
    valid = [i for i in range(n) if sizes[i] > 0]
    if not valid:
        return boxes
    median_box = np.median(filled[valid], axis=0)
    for i in range(n):
        if not (filled[i] != 0).any():
            filled[i] = median_box

    # 2) 单帧跳变（面积/位置突然大幅变化）拉回上一个框
    for i in range(1, n):
        area = max(0.0, filled[i][2] - filled[i][0]) * max(0.0, filled[i][3] - filled[i][1])
        if typical > 0 and (area < typical * 0.35 or area > typical * 2.6):
            filled[i] = filled[i - 1]

    # 3) 滑动平均（原版 T=5 的做法：向前取窗）
    if window and window > 1:
        out = filled.copy()
        for i in range(n):
            end = min(n, i + window)
            out[i] = filled[i:end].mean(axis=0)
        filled = out
    return filled


def crop_box(
    box: np.ndarray, width: int, height: int, *, pad_x: float, pad_top: float,
    pad_bottom: float, square: bool, mode: str = "face", mouth_scale: float = 0.5,
    mouth_center: float = 0.72,
) -> tuple[int, int, int, int]:
    """人脸框 → 裁剪框。

    `mode="face"`：整张脸 + 比例扩展（Wav2Lip 原版口径，安全但嘴部只占 96×96 的一小块）
    `mode="mouth"`：以嘴为中心取一个正方形小框 —— 模型只有 96×96 的输出分辨率，
    **框越小，嘴在 96×96 里占的像素越多，出来越清楚**（这是本项目 1440 宽特写镜头的关键）。
    """
    x1, y1, x2, y2 = box
    if mode == "mouth":
        face_w = max(1.0, x2 - x1)
        face_h = max(1.0, y2 - y1)
        cx = (x1 + x2) / 2
        cy = y1 + face_h * mouth_center
        half = max(face_w, face_h) * mouth_scale / 2
        left = round(max(0, min(width - 2, cx - half)))
        top = round(max(0, min(height - 2, cy - half)))
        right = round(max(left + 2, min(width, cx + half)))
        bottom = round(max(top + 2, min(height, cy + half)))
        return left, top, right, bottom
    w = max(1.0, x2 - x1)
    h = max(1.0, y2 - y1)
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    half_w = w / 2 + pad_x * w
    half_h = h / 2 + (pad_bottom - pad_top) * h / 2
    cy += (pad_bottom - pad_top) * h / 2
    if square:
        half = max(half_w, half_h)
        half_w = half_h = half
    left = round(max(0, min(width - 2, cx - half_w)))
    top = round(max(0, min(height - 2, cy - half_h)))
    right = round(max(left + 2, min(width, cx + half_w)))
    bottom = round(max(top + 2, min(height, cy + half_h)))
    return left, top, right, bottom


def feather_mask(
    height: int, width: int, *, top_fraction: float, feather: float
) -> np.ndarray:
    """羽化遮罩：只替换「下半张脸」，并且**四边都渐变成 0**。

    四边（尤其是底边）必须渐出：硬边会在脖子上留一条明显的横线 —— 这是第一版
    实测出来的问题（见 `tools/lipsync/README.md` 的对比图说明）。
    """
    mask = np.zeros((height, width), dtype=np.float32)
    start = round(height * (1.0 - top_fraction))
    ramp = max(1, round(feather * min(height, width)))
    ys = np.arange(height, dtype=np.float32)
    vertical = np.minimum(
        np.clip((ys - start) / ramp, 0.0, 1.0),
        np.clip((height - 1 - ys) / ramp, 0.0, 1.0),
    )
    xs = np.arange(width, dtype=np.float32)
    horizontal = np.minimum(
        np.clip(xs / ramp, 0.0, 1.0),
        np.clip((width - 1 - xs) / ramp, 0.0, 1.0),
    )
    mask = np.minimum(vertical[:, None], horizontal[None, :])
    return mask


def center_mask(height: int, width: int, *, size: float, feather: float) -> np.ndarray:
    """画面中央的羽化方框：这是**一定**会贴回的区域（嘴唇所在），保证口型一定跟着配音走。"""
    ys = np.arange(height, dtype=np.float32)[:, None]
    xs = np.arange(width, dtype=np.float32)[None, :]
    cy, cx = (height - 1) / 2, (width - 1) / 2
    half_h = max(1.0, height * size / 2)
    half_w = max(1.0, width * size / 2)
    distance = np.maximum(np.abs(ys - cy) / half_h, np.abs(xs - cx) / half_w)
    ramp = max(1e-6, feather)
    return np.clip((1.0 - distance) / ramp, 0.0, 1.0)


def adaptive_alpha(
    generated: np.ndarray, original: np.ndarray, prior: np.ndarray, *, blur: float = 0.02
) -> np.ndarray:
    """「只贴模型真正改过的地方」：用生成图与原图的差做出遮罩，再和先验（下半张脸）求交。

    为什么需要：Wav2Lip 的 96×96 输出放大到 1440 宽的镜头等于 15 倍上采样，整块贴回去
    会把下巴/脸颊变成蜡像。但**真正需要改的只有嘴唇**，周围皮肤本来就不该动 ——
    按差值做遮罩，皮肤保持原分辨率，只有嘴部参与替换。
    """
    diff = np.abs(generated - original).max(axis=2)
    sigma = max(1.0, blur * min(diff.shape))
    diff = cv2.GaussianBlur(diff, (0, 0), sigma)
    scale = float(np.percentile(diff, 99.0))
    if scale <= 1e-3:
        return np.zeros_like(prior)
    normalized = diff / scale
    return np.clip((normalized - 0.12) / 0.5, 0.0, 1.0) * prior


def _restore_detail(original: np.ndarray, generated: np.ndarray, gain: float) -> np.ndarray:
    """把原图的高频细节叠回生成结果上。

    Wav2Lip 只输出 96×96 的人脸，放大回 1440 宽等于 15 倍上采样 —— 皮肤会变成
    「蜡像」。原图的高频（毛孔、唇纹、发丝）是免费的真实细节，叠回去立刻不蜡。
    gain 不能给太大：原图的嘴唇边缘会跟着叠上来，1.0 会出现「双层嘴唇」。
    """
    if gain <= 0:
        return generated
    low = cv2.GaussianBlur(original, (0, 0), 1.2)
    high = original.astype(np.float32) - low.astype(np.float32)
    return generated + gain * high


# ── 主流程 ───────────────────────────────────────────────────


def run(args) -> dict:
    started = time.time()
    video = Path(args.video)
    audio = Path(args.audio)
    out = Path(args.out)
    report_path = Path(args.report) if args.report else None

    if not video.is_file():
        raise SystemExit(f"找不到视频：{video}")
    if not audio.is_file():
        raise SystemExit(f"找不到音频：{audio}")
    if not Path(args.detector).is_file():
        raise SystemExit(f"找不到人脸检测模型：{args.detector}")
    if not Path(args.model).is_file():
        raise SystemExit(f"找不到 Wav2Lip 模型：{args.model}")

    width, height, fps = ffprobe_video(video)
    boxes, found = detect_faces(
        video, width, height, detector_path=Path(args.detector), detect_width=args.detect_width
    )
    total = len(boxes)
    if args.limit and total > args.limit:
        boxes = boxes[: args.limit]
        total = args.limit
    boxes = smooth_boxes(boxes, window=args.smooth)

    crops = [
        crop_box(
            boxes[i], width, height,
            pad_x=args.pad_x, pad_top=args.pad_top, pad_bottom=args.pad_bottom,
            square=not args.no_square, mode=args.crop_mode,
            mouth_scale=args.mouth_scale, mouth_center=args.mouth_center,
        )
        for i in range(total)
    ]

    mel = mel_spectrogram(read_audio_16k(audio))
    mel_batch = mel_chunks_for_frames(mel, total, fps)

    session = ort.InferenceSession(str(args.model), providers=["CPUExecutionProvider"])
    input_names = [item.name for item in session.get_inputs()]
    masks = {
        crop: feather_mask(
            crop[3] - crop[1], crop[2] - crop[0],
            top_fraction=args.paste_top, feather=args.feather,
        )
        for crop in set(crops)
    }

    if args.dump_crops and args.dump_dir:
        dump_dir = Path(args.dump_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)
        reader = FrameReader(video, width, height)
        try:
            step = max(1, total // max(1, args.dump_crops))
            for index, frame in enumerate(reader.frames()):
                if index >= total:
                    break
                if index % step:
                    continue
                left, top, right, bottom = crops[index]
                cv2.imwrite(str(dump_dir / f"crop_{index:04d}.jpg"), frame[top:bottom, left:right])
        finally:
            reader.close()

    reader = FrameReader(video, width, height)
    writer = FrameWriter(out, width, height, fps, crf=args.crf, preset=args.preset)
    processed = 0
    change_acc = 0.0
    change_count = 0
    batch_size = max(1, args.batch)
    patch_top = args.paste_top
    try:
        pending_frames: list[np.ndarray] = []
        pending_index: list[int] = []

        def flush() -> None:
            nonlocal processed, change_acc, change_count
            if not pending_frames:
                return
            vid = np.zeros((len(pending_frames), 6, IMG_SIZE, IMG_SIZE), dtype=np.float32)
            for slot, index in enumerate(pending_index):
                left, top, right, bottom = crops[index]
                crop = cv2.resize(
                    pending_frames[slot][top:bottom, left:right], (IMG_SIZE, IMG_SIZE)
                )
                tensor = crop.astype(np.float32).transpose(2, 0, 1) / 255.0
                # **必须是 6 通道「遮罩图 + 原图」**，第一路把**下半张脸**（嘴）置 0 ——
                # 对应原版 inference.py 的 `img_masked[:, img_size//2:] = 0`
                # （那一行作用在 HWC 的第 1 轴 = 高，即行 48:96）。
                # 实测这一条错了会**静默失效**：两路都给原图时，模型直接从输入里
                # 「抄」嘴，输出与输入的最大差异只有 0.011，口型完全没生效；
                # 遮罩对了以后同一位置的 mel 灵敏度是 0.514，且上半张脸原样保留。
                masked = tensor.copy()
                masked[:, IMG_SIZE // 2 :, :] = 0.0
                vid[slot, :3] = masked
                vid[slot, 3:] = tensor
            feeds = {
                input_names[0]: mel_batch[pending_index],
                input_names[1]: vid,
            }
            generated = session.run(None, feeds)[0]
            for slot, index in enumerate(pending_index):
                left, top, right, bottom = crops[index]
                # 只放大「下半张脸」：上半张脸保持原分辨率（1080p 下眼睛不糊）。
                # 裁剪框里的行号和人脸张量（96×96）里的行号必须分别算，别混用。
                crop_height = bottom - top
                cut = round(crop_height * (1.0 - patch_top))
                patch_cut = round(IMG_SIZE * (1.0 - patch_top))
                region_height = crop_height - cut
                patch = generated[slot].transpose(1, 2, 0)
                if region_height <= 0 or IMG_SIZE - patch_cut <= 0:
                    writer.write(pending_frames[slot])
                    processed += 1
                    continue
                patch = np.clip(patch * 255.0, 0, 255).astype(np.uint8)
                base = cv2.resize(
                    patch[patch_cut:],
                    (right - left, region_height),
                    interpolation=cv2.INTER_LANCZOS4,
                ).astype(np.float32)
                target = pending_frames[slot][top + cut : bottom, left:right]
                prior = masks[crops[index]][cut:]
                # 自检用的真实改动量：只在先验区域里量，避免被边缘羽化稀释
                inner = prior > 0.5
                if inner.any():
                    delta = np.abs(base - target.astype(np.float32))[inner].mean() / 255.0
                    change_acc += float(delta)
                    change_count += 1
                if args.mask_mode == "lips":
                    # 嘴唇核心区一定替换（否则轻声/闭口音的细微动作会被差值门槛吃掉，
                    # 实测会出现「配音已经开始了但嘴前 0.4 秒没动」），
                    # 核心区之外只替换模型真正改过的地方（皮肤保持原分辨率，不发蜡）
                    core = center_mask(
                        region_height, right - left, size=args.core, feather=0.3
                    )
                    alpha = np.maximum(
                        core, adaptive_alpha(base, target.astype(np.float32), prior)
                    )
                else:
                    alpha = prior
                resized = _restore_detail(target, base, args.detail_gain)
                if args.sharpen > 0:
                    blur = cv2.GaussianBlur(resized, (0, 0), 1.0)
                    resized = resized + args.sharpen * (resized - blur)
                blended = (
                    resized * alpha[..., None]
                    + target.astype(np.float32) * (1.0 - alpha[..., None])
                )
                pending_frames[slot][top + cut : bottom, left:right] = np.clip(
                    blended, 0, 255
                ).astype(np.uint8)
                writer.write(pending_frames[slot])
                processed += 1
            pending_frames.clear()
            pending_index.clear()

        for index, frame in enumerate(reader.frames()):
            if index >= total:
                break
            pending_frames.append(frame)
            pending_index.append(index)
            if len(pending_frames) >= batch_size:
                flush()
        flush()
    finally:
        reader.close()
        writer.close()

    elapsed = time.time() - started
    report = {
        "engine": "wav2lip-onnx",
        "model": Path(args.model).name,
        "detector": Path(args.detector).name,
        "video": video.name,
        "audio": audio.name,
        "width": width,
        "height": height,
        "fps": round(fps, 4),
        "frames": processed,
        "faces_detected": found,
        "detect_ratio": round(found / total, 4) if total else 0.0,
        "audio_sec": round(len(read_audio_16k(audio)) / SAMPLE_RATE, 3),
        "mel_frames": int(mel.shape[1]),
        "elapsed_sec": round(elapsed, 2),
        "mean_patch_change": round(change_acc / change_count, 5) if change_count else 0.0,
        "crop_mode": args.crop_mode,
        "mask_mode": args.mask_mode,
        "core": args.core,
        "mouth_scale": args.mouth_scale,
        "paste_top": args.paste_top,
        "detail_gain": args.detail_gain,
        "sharpen": args.sharpen,
        "batch": batch_size,
        "crf": args.crf,
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    if report["mean_patch_change"] < 0.01:
        # 这一条是踩过坑加上的：mel 没喂对时模型会「抄」输入，口型静默失效
        print(
            "警告：模型输出与输入几乎相同（mean_patch_change="
            f"{report['mean_patch_change']:.4f}），口型可能没有生效",
            file=sys.stderr,
        )
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False))
    return report


def build_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    assets = here.parent.parent / "assets" / "lipsync"
    parser = argparse.ArgumentParser(description="Wav2Lip ONNX 逐帧口型")
    parser.add_argument("--video", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default=str(assets / DEFAULT_MODEL))
    parser.add_argument("--detector", default=str(assets / DEFAULT_DETECTOR))
    parser.add_argument("--report", default="")
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--crf", type=int, default=17)
    parser.add_argument("--preset", default="veryfast")
    parser.add_argument("--detect-width", type=int, default=480)
    parser.add_argument("--smooth", type=int, default=5)
    parser.add_argument("--pad-x", type=float, default=0.12)
    parser.add_argument("--pad-top", type=float, default=0.15)
    parser.add_argument("--pad-bottom", type=float, default=0.25)
    parser.add_argument("--no-square", action="store_true", help="裁剪框不扩成正方形")
    parser.add_argument(
        "--crop-mode", choices=("face", "mouth"), default="mouth",
        help="face = 整张脸（配 --paste-top 0.55）；"
        "mouth = 以嘴为中心的小方框（配 --paste-top 1.0）",
    )
    parser.add_argument("--mouth-scale", type=float, default=0.5)
    parser.add_argument("--mouth-center", type=float, default=0.72)
    parser.add_argument(
        "--paste-top", type=float, default=1.0,
        help="贴回裁剪框的下部比例（face 裁剪用 0.55 让眼睛保持原分辨率；"
        "mouth 裁剪必须用 1.0，否则会切掉上唇）",
    )
    parser.add_argument("--feather", type=float, default=0.12)
    parser.add_argument(
        "--detail-gain", type=float, default=0.35,
        help="把原图高频细节叠回嘴部的强度（0 = 纯 Wav2Lip 输出，会偏蜡）",
    )
    parser.add_argument("--sharpen", type=float, default=0.25)
    parser.add_argument(
        "--core", type=float, default=0.62,
        help="嘴唇核心区大小（占裁剪框的比例）：这一块一定贴回，保证口型跟着配音",
    )
    parser.add_argument(
        "--mask-mode", choices=("lips", "rect"), default="lips",
        help="lips = 只替换模型真正改过的嘴唇（皮肤保持原分辨率）；rect = 整块贴回",
    )
    parser.add_argument("--dump-crops", type=int, default=0)
    parser.add_argument("--dump-dir", default="")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 帧（冒烟测试用）")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except RuntimeError as exc:
        print(f"口型处理失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
