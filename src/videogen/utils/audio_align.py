"""把配音对齐到「画面里那张嘴」的起点。

背景（真实测量，2026-09-11，同一个镜头/同一句话）：

| 方案 | 模型原生说话区间 | 我们的配音区间 | 结果 |
|---|---|---|---|
| prompt 无台词 | 2.76–3.98s（说的是别的话） | 0.02–1.34s | 声音先到、嘴 2.7s 后才动，且是两句话 |
| 台词进 prompt + 配 TTS | 1.22–4.60s | 0.02–1.34s | 话对上了，但开头仍错位 ≈1.2 秒 |
| 台词进 prompt + 原生语音 | 0.98–3.72s | 同一轨 | 口型天然完美，但音色不受我们控制 |

所以第三条路是：**台词进 prompt（让模型说同一句话）+ 把配音延后到模型开口的时刻**。
代价只有本地计算，音色仍然是我们选的。

这里的判据是「相对自身峰值的能量阈值」而不是 ffmpeg `silencedetect` 的绝对 dB：
真实样本里片头有底噪/呼吸（-45dB 左右），绝对阈值会把「还没说话」判成「已经在说」。
"""

from __future__ import annotations

import logging
import math
import struct
import wave
from pathlib import Path

from videogen.utils.ffmpeg import FFmpegError, run_ffmpeg

logger = logging.getLogger(__name__)

__all__ = ["mouth_align_delay", "speech_span"]

#: 分析用的采样率（16k 单声道对「什么时候开口」足够了，还便宜）
ANALYZE_RATE = 16000
#: 包络窗口（秒）
WINDOW_SEC = 0.02
#: 判定「在说话」的能量阈值 = 峰值 × 该比例
DEFAULT_THRESHOLD_RATIO = 0.15
#: 结尾之后允许的尾巴（避免把呼吸声算进说话区间）
_TAIL_SLACK_SEC = 0.05


async def _to_wav(source: Path, target: Path) -> bool:
    try:
        await run_ffmpeg(
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(ANALYZE_RATE),
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
            timeout_sec=120,
        )
    except FFmpegError:
        logger.debug("抽音轨失败，跳过口型对齐：%s", source, exc_info=True)
        return False
    return target.is_file() and target.stat().st_size > 0


def _envelope(samples: tuple[float, ...], rate: int) -> list[float]:
    step = max(1, int(rate * WINDOW_SEC))
    out: list[float] = []
    for start in range(0, len(samples), step):
        chunk = samples[start : start + step]
        if not chunk:
            break
        out.append(math.sqrt(sum(value * value for value in chunk) / len(chunk)))
    return out


async def speech_span(
    source: Path,
    work_dir: Path,
    *,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
) -> tuple[float, float] | None:
    """返回 (开始说话, 说完) 的秒数；探测不了返回 None。

    `work_dir` 用来落中间 wav（调用方负责清理）。
    """
    if not Path(source).is_file():
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    wav_path = work_dir / f".onset-{abs(hash(str(source)))}.wav"
    if not await _to_wav(Path(source), wav_path):
        return None
    try:
        with wave.open(str(wav_path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (OSError, wave.Error):  # pragma: no cover - 坏文件
        return None
    finally:
        wav_path.unlink(missing_ok=True)

    count = len(frames) // 2
    if count <= 0:
        return None
    samples = tuple(value / 32768.0 for value in struct.unpack(f"<{count}h", frames[: count * 2]))
    env = _envelope(samples, rate)
    if not env:
        return None
    peak = max(env)
    if peak <= 1e-6:
        return None
    threshold = peak * max(0.01, threshold_ratio)
    onset = next((index for index, value in enumerate(env) if value > threshold), None)
    if onset is None:
        return None
    tail = next(
        (len(env) - 1 - index for index, value in enumerate(reversed(env)) if value > threshold),
        onset,
    )
    return (round(onset * WINDOW_SEC, 3), round(tail * WINDOW_SEC + _TAIL_SLACK_SEC, 3))


async def mouth_align_delay(
    video_path: Path,
    work_dir: Path,
    *,
    voice_sec: float,
    segment_sec: float,
    max_delay_sec: float = 1.5,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
) -> float:
    """算出「配音该延后多少秒才和嘴对上」。

    规则：延后量 = 画面里人声的起点，且必须满足
    `起点 + 配音时长 <= 片段时长`（否则尾音会被切掉，那就宁可不对齐）。
    """
    span = await speech_span(video_path, work_dir, threshold_ratio=threshold_ratio)
    if span is None:
        return 0.0
    onset = span[0]
    if onset <= 0.05:
        return 0.0
    allowed = segment_sec - max(0.0, voice_sec) - 0.05
    if allowed <= 0:
        return 0.0
    return round(min(onset, max_delay_sec, allowed), 3)
