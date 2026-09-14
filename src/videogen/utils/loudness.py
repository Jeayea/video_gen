"""响度测量与归一（EBU R128，靠 ffmpeg 的 `ebur128` 滤镜，C 速度）。

为什么需要它：**逐条配音是独立生成的**，音色不同、每句念法不同，所以响度天然不一致。
真实项目实测（15 条云配音，`openai/gpt-audio-mini`）：

```
nova    n=3  均值=-18.30 LUFS      ash     n=3  均值=-24.77 LUFS
coral   n=4  均值=-19.45 LUFS      shimmer n=4  均值=-21.73 LUFS
onyx    n=1  均值=-22.80 LUFS
全片极差 7.90 dB（-17.8 ~ -25.7 LUFS），标准差 2.38 dB
```

也就是说：**同一个音色内部很稳（±0.5dB），换一个音色就换一个音量**（最多差 6.5dB）。
观众听到的就是「这句话突然变小了」。所以要在合成时按条把音量拉齐。

两条设计取舍：

- **测量用集成响度（Integrated LUFS）**，不用 `volumedetect` 的 mean_volume：
  后者把句首句尾的静音也算进平均，一条停顿多的台词会被判成「小声」。
  gated 的集成响度才反映「说出来的那几句有多响」。
- **修正是「定值增益 + 上下限」**，不是 `loudnorm` 动态归一：一条 2 秒的台词跑动态
  loudnorm 会因为素材太短而上下乱飘。这里只量一个数、算一个增益，
  并且**限制在 ±N dB 以内**（默认 6dB）——超过这个范围的差异通常是有意的表演
  （耳语、嘶吼），不该被拉平。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from videogen.utils.ffmpeg import FFmpegError, ffmpeg_available, run_ffmpeg_capture

logger = logging.getLogger(__name__)

__all__ = ["Loudness", "gain_for", "measure", "measure_many", "volume_to_db"]

_INTEGRATED = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+|-?inf)\s*LUFS")
_TRUE_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+|-?inf)\s*dBFS")
_RANGE = re.compile(r"LRA:\s*(-?[\d.]+|-?inf)\s*LU")


@dataclass(slots=True)
class Loudness:
    """一条音频的响度实测。"""

    #: 集成响度（LUFS）；测不出来 / 整条静音时是 None
    integrated: float | None = None
    #: 真峰值（dBFS）
    true_peak: float | None = None
    #: 响度范围（LU）
    lra: float | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.integrated is not None

    def summary(self) -> dict:
        return {
            "lufs": self.integrated,
            "true_peak_db": self.true_peak,
            "lra": self.lra,
            "error": self.error,
        }


def _to_float(raw: str) -> float | None:
    text = (raw or "").strip().lower()
    if not text or "inf" in text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    # ebur128 对静音会给 -70/-120 这类「没有信号」的值
    return value if value > -70.0 else None


async def measure(path: Path, *, timeout_sec: float = 120.0) -> Loudness:
    """量一条音频的集成响度 / 真峰值 / LRA（调 ffmpeg 的 ebur128）。

    任何失败都返回带 `error` 的空结果 —— 响度只是「对齐音量」用的，
    量不出来就按「不修正」处理，绝不因此打断合成。
    """
    if not ffmpeg_available() or not Path(path).is_file():
        return Loudness(error="没有 ffmpeg 或文件不存在")
    try:
        output = await run_ffmpeg_capture(
            [
                "-hide_banner",
                "-nostats",
                "-i", str(path),
                "-af", "ebur128=peak=true",
                "-f", "null",
                "-",
            ],
            timeout_sec=timeout_sec,
        )
    except (FFmpegError, OSError) as exc:  # 测量失败不影响出片
        logger.debug("响度测量失败：%s（%s）", path, exc)
        return Loudness(error=str(exc)[:200])

    result = _parse(output)
    if result.integrated is None and not result.error:
        result.error = "测不到集成响度（整条静音或文件太短）"
    return result


def _parse(output: str) -> Loudness:
    """解析 ebur128 的汇总输出（单独拆出来是为了能用样本直接测）。"""
    integrated = _INTEGRATED.search(output)
    peak = _TRUE_PEAK.search(output)
    lra = _RANGE.search(output)
    return Loudness(
        integrated=_to_float(integrated.group(1)) if integrated else None,
        true_peak=_to_float(peak.group(1)) if peak else None,
        lra=_to_float(lra.group(1)) if lra else None,
    )


async def measure_many(paths: list[Path]) -> dict[str, Loudness]:
    """按路径批量测量（顺序执行：ebur128 本身很快，避免把 CPU 抢满）。"""
    result: dict[str, Loudness] = {}
    for path in paths:
        key = str(path)
        if key not in result:
            result[key] = await measure(path)
    return result


def gain_for(
    measured: float | None, *, target: float, max_db: float, intent_db: float = 0.0
) -> float:
    """算这条音频该补多少 dB。

    `intent_db` 是**念法里显式的音量意图**（例如 Edge 的 `volume=-20%`）：
    它已经烘进音频文件里了，而归一会把它抹掉，所以要在这里加回来 ——
    对齐的是「基准音量」，不是「表演」。
    """
    if measured is None:
        return round(max(-max_db, min(max_db, intent_db)), 2)
    correction = max(-max_db, min(max_db, target - measured))
    return round(correction + intent_db, 2)


_VOLUME = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(%|db)?\s*$", re.IGNORECASE)


def volume_to_db(value: str) -> float:
    """把念法里的音量（`+5%` / `-20%` / `-3dB` / `-3`）换算成 dB。

    百分比按 **振幅** 解释（edge-tts 的 `--volume=+5%` 就是振幅 +5%），
    所以是 `20*log10(1+p/100)`，不是把百分比当 dB 用。
    """
    match = _VOLUME.match(value or "")
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit == "%":
        amplitude = max(0.01, (100.0 + number) / 100.0)
        try:
            import math

            return round(20.0 * math.log10(amplitude), 2)
        except ValueError:  # pragma: no cover - max(0.01) 已经兜住了
            return 0.0
    return round(number, 2)
