"""口型对齐实测：**嘴什么时候开始动、什么时候停**，和配音比。

为什么不用「相关系数」：试过画面差异、亮度、暗部占比、边缘能量四种像素统计量，
拿**模型原生语音**（天生同步、可作为基准）去验，最好的也只有 r≈0.5 而且最佳
滞后 0.1~0.25 秒 —— 说明这些便宜的二维统计量量不准嘴的开合，拿它下结论是不
诚实的。实测记录见 `tools/lipsync/README.md`。

改用**时间点**口径，因为这个量既稳、又正好对应观众真正的感受：

- 「画面里嘴开始动」的时刻 vs「配音开始」的时刻 → 开口错位（秒）
- 「配音结束之后嘴还在动多久」→ 尾部错位（秒）—— 这正是逐帧口型要解决的问题：
  原片里模型自己还在说话、配音早就说完了，嘴却一直在动

判据沿用 `audio_align.py` 的「相对峰值」阈值（绝对阈值会被片头底噪骗）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from videogen.utils import ffmpeg as ffmpeg_utils
from videogen.utils.audio_align import speech_span

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ROI",
    "ROI_SIZE",
    "ActivitySpan",
    "LipSyncCompare",
    "activity_span",
    "lip_sync_compare",
    "mouth_activity",
    "rewrite_activity",
    "roi_frames",
]

#: 嘴部 ROI（画面比例 x0,y0,x1,y1）：竖屏特写的默认位置，可用配置覆盖
DEFAULT_ROI = (0.36, 0.47, 0.60, 0.62)
#: ROI 采样尺寸（宽,高）：够看出嘴的开合，又便宜
ROI_SIZE = (64, 48)
#: 活动阈值（相对该曲线自身的峰值）
ONSET_RATIO = 0.20
#: 比这更短的「活动」不单独算
MIN_ACTIVE_SEC = 0.12
#: 「明显改善」的最小余量（秒）
_MARGIN_SEC = 0.10


@dataclass(slots=True)
class ActivitySpan:
    """一段「画面在动」的区间。"""

    start: float
    end: float
    peak_time: float

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def summary(self) -> dict[str, float]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "peak_time": round(self.peak_time, 3),
            "duration": self.duration,
        }


@dataclass(slots=True)
class LipSyncCompare:
    """逐帧口型的「前 / 后」对照（全部是秒；量不出来就是 None）。"""

    #: 配音本身的说话区间
    dub: ActivitySpan | None = None
    #: 口型处理前，画面里嘴动的时间区间
    before: ActivitySpan | None = None
    #: 口型处理后，画面里嘴动的时间区间
    after: ActivitySpan | None = None
    #: 用「相对原片段的改写量」量的活动区间（需要原片段）
    rewritten: ActivitySpan | None = None
    roi: tuple[float, float, float, float] = DEFAULT_ROI

    def _gap(self, span: ActivitySpan | None) -> float | None:
        if span is None or self.dub is None:
            return None
        return round(abs(span.start - self.dub.start), 3)

    def _tail(self, span: ActivitySpan | None) -> float | None:
        if span is None or self.dub is None:
            return None
        return round(max(0.0, span.end - self.dub.end), 3)

    @property
    def before_onset_gap(self) -> float | None:
        return self._gap(self.before)

    @property
    def after_onset_gap(self) -> float | None:
        return self._gap(self.after)

    @property
    def before_tail(self) -> float | None:
        """配音结束后，嘴还在动多久。"""
        return self._tail(self.before)

    @property
    def after_tail(self) -> float | None:
        return self._tail(self.after)

    @property
    def improved(self) -> bool:
        """开口错位或尾部错位有实质下降就算改善。"""
        for before, after in (
            (self.before_onset_gap, self.after_onset_gap),
            (self.before_tail, self.after_tail),
        ):
            if before is not None and after is not None and after < before - _MARGIN_SEC:
                return True
        return False

    def summary(self) -> dict[str, object]:
        return {
            "dub": self.dub.summary() if self.dub else None,
            "before": self.before.summary() if self.before else None,
            "after": self.after.summary() if self.after else None,
            "rewritten": self.rewritten.summary() if self.rewritten else None,
            "before_onset_gap": self.before_onset_gap,
            "after_onset_gap": self.after_onset_gap,
            "before_tail": self.before_tail,
            "after_tail": self.after_tail,
            "improved": self.improved,
            "roi": list(self.roi),
        }

    def text(self) -> str:
        if self.dub is None:
            return "口型实测：配音没有说话区间，量不出来"
        parts = []
        if self.before is not None:
            parts.append(
                f"对齐前画面动 {self.before.start:.2f}–{self.before.end:.2f}s"
                f"（开口错位 {self.before_onset_gap:.2f}s、尾部多动 {self.before_tail:.2f}s）"
            )
        if self.after is not None:
            parts.append(
                f"对齐后画面动 {self.after.start:.2f}–{self.after.end:.2f}s"
                f"（开口错位 {self.after_onset_gap:.2f}s、尾部多动 {self.after_tail:.2f}s）"
            )
        head = f"配音 {self.dub.start:.2f}–{self.dub.end:.2f}s"
        return "；".join([head, *parts])

    def warning(self) -> str:
        """需要提醒用户的一句话（没问题时返回空串）。"""
        if self.after is None or self.dub is None:
            return ""
        if not self.after_onset_gap or self.after_onset_gap <= 0.25:
            return ""
        return (
            f"逐帧口型后画面仍在配音开始后 {self.after_onset_gap:.2f}s 才动，"
            "建议检查配音是否比画面短很多 / 是否开了口型对齐"
        )


def activity_span(
    series: list[float],
    *,
    fps: float,
    ratio: float = ONSET_RATIO,
    min_sec: float = MIN_ACTIVE_SEC,
) -> ActivitySpan | None:
    """从一条活动曲线里找出「开始动 / 停止动」。

    阈值取相对峰值：绝对阈值会被片头底噪骗（`audio_align.py` 踩过的坑）。
    """
    if fps <= 0 or len(series) < 2:
        return None
    peak = max(series)
    if peak <= 0:
        return None
    threshold = peak * ratio
    min_windows = max(1, round(min_sec * fps))
    active = [index for index, value in enumerate(series) if value >= threshold]
    if not active:
        return None
    # 「开始动」= 第一次连续 min_windows 个窗口都超阈值（避免单帧噪声）；
    #   「停止动」= 最后一次超阈值 —— 中间的自然停顿**不能**截断区间，
    #   否则「配音早就说完了、嘴还在动」这件事会被前一个停顿提前截掉，量不出来。
    start = active[0]
    for index in active:
        if index + min_windows - 1 <= len(series) - 1 and all(
            series[index + offset] >= threshold for offset in range(min_windows)
        ):
            start = index
            break
    end = active[-1]
    peak_index = max(range(len(series)), key=lambda i: series[i])
    return ActivitySpan(
        start=round(start / fps, 3),
        end=round((end + 1) / fps, 3),
        peak_time=round((peak_index + 1) / fps, 3),
    )


async def roi_frames(
    video_path: Path,
    *,
    roi: tuple[float, float, float, float] = DEFAULT_ROI,
    fps: float = 24.0,
    timeout_sec: float = 60.0,
) -> list[bytes]:
    """把 ROI 抽成逐帧灰度小图（64×48），失败返回空列表。"""
    if not video_path.is_file() or not ffmpeg_utils.ffmpeg_available():
        return []
    width, height = ROI_SIZE
    x0, y0, x1, y1 = roi
    crop = (
        f"crop=iw*{x1 - x0:.4f}:ih*{y1 - y0:.4f}:iw*{x0:.4f}:ih*{y0:.4f},"
        f"scale={width}:{height}:flags=area,format=gray"
    )
    try:
        raw = await ffmpeg_utils.run_ffmpeg_bytes(
            [
                "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                "-vf", f"fps={fps:g},{crop}", "-an", "-f", "rawvideo", "-",
            ],
            timeout_sec=timeout_sec,
        )
    except ffmpeg_utils.FFmpegError as exc:
        logger.debug("读画面 ROI 失败 %s: %s", video_path, exc)
        return []
    stride = width * height
    return [raw[i : i + stride] for i in range(0, len(raw) - stride + 1, stride)]


def _mean_abs_diff(first: bytes, second: bytes, stride: int, *, floor: int = 0) -> float:
    total = 0
    for a, b in zip(first, second, strict=True):
        delta = abs(a - b) - floor
        if delta > 0:
            total += delta
    return total / (stride * 255.0)


async def mouth_activity(
    video_path: Path,
    *,
    roi: tuple[float, float, float, float] = DEFAULT_ROI,
    fps: float = 24.0,
    timeout_sec: float = 60.0,
) -> list[float]:
    """ROI 内相邻帧的平均变化（0..1），长度 = 帧数 - 1。"""
    frames = await roi_frames(video_path, roi=roi, fps=fps, timeout_sec=timeout_sec)
    if len(frames) < 2:
        return []
    stride = ROI_SIZE[0] * ROI_SIZE[1]
    return [
        _mean_abs_diff(frames[index], frames[index + 1], stride)
        for index in range(len(frames) - 1)
    ]


async def rewrite_activity(
    video_path: Path,
    reference_path: Path,
    *,
    roi: tuple[float, float, float, float] = DEFAULT_ROI,
    fps: float = 24.0,
    floor: int = 12,
) -> list[float]:
    """逐帧量「口型被改写了多少」= 相对原片段 ROI 的平均差异。

    先减掉 `floor`（12/255）再平均：重新编码产生的噪声通常低于这个量级，
    而真正的嘴部改写是 30~200/255，减掉门槛以后量到的才是真改写。
    """
    edited = await roi_frames(video_path, roi=roi, fps=fps)
    original = await roi_frames(reference_path, roi=roi, fps=fps)
    size = min(len(edited), len(original))
    if size < 4:
        return []
    stride = ROI_SIZE[0] * ROI_SIZE[1]
    return [
        _mean_abs_diff(edited[index], original[index], stride, floor=floor)
        for index in range(size)
    ]


async def lip_sync_compare(
    original_path: Path,
    synced_path: Path,
    dubbed_audio: Path | None,
    *,
    roi: tuple[float, float, float, float] = DEFAULT_ROI,
    fps: float = 24.0,
) -> LipSyncCompare:
    """量「逐帧口型前 / 后」的开口与收尾是否对上配音。"""
    report = LipSyncCompare(roi=roi)
    if dubbed_audio is not None and Path(dubbed_audio).is_file():
        span = await speech_span(Path(dubbed_audio), Path(dubbed_audio).parent)
        if span is not None:
            onset, end = span
            report.dub = ActivitySpan(start=onset, end=end, peak_time=onset)
    report.before = activity_span(await mouth_activity(original_path, roi=roi, fps=fps), fps=fps)
    report.after = activity_span(await mouth_activity(synced_path, roi=roi, fps=fps), fps=fps)
    if Path(synced_path) != Path(original_path):
        rewritten = await rewrite_activity(synced_path, original_path, roi=roi, fps=fps)
        report.rewritten = activity_span(rewritten, fps=fps, ratio=0.25)
    return report
