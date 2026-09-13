"""音画同步检查：抓「拼接把音频空洞吃掉」这类事故。

起因是一次真实事故：`projects/<id>/ab_lipsync/lipsync_compare.mp4` 用
「每个部件音视频时长不等 → `-f concat -c copy` 拼接」的做法拼出来，
mp4 的音轨表达不了「空洞」，后面所有片段的音频整体提前，
累计漂移 **7.154 秒**（视频 41.833s / 音频 34.679s）—— 用户看到的就是
「语音和视频是分开的」。

判据很朴素但足够灵敏：**成片的音轨时长必须和视频时长基本相等**。
只要拼接过程丢了空洞，这个差值就会等于所有空洞之和，一眼可见。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from videogen.utils.ffmpeg import probe_stream_durations

logger = logging.getLogger(__name__)

__all__ = ["SyncReport", "check_av_sync", "format_sync_warning"]

#: 允许的音视频时长差（秒）：一帧的零头 + 容器时间基误差，远小于一帧要求过严
DEFAULT_TOLERANCE_SEC = 0.20


@dataclass(slots=True)
class SyncReport:
    """一个媒体文件的音画时长对照。"""

    #: 视频流时长（秒），探测不到就是 None
    video_sec: float | None = None
    #: 音频流时长（秒），没有音轨或探测不到就是 None
    audio_sec: float | None = None
    #: 容差（秒）
    tolerance_sec: float = DEFAULT_TOLERANCE_SEC
    #: 测不出来（没有 ffprobe / 文件不存在）时为 True —— 这时 ok 为 False 但不算事故
    unknown: bool = False

    @property
    def drift_sec(self) -> float | None:
        """音频相对视频的漂移：正 = 音频更长，负 = **音频被截短/整体提前**。"""
        if self.video_sec is None or self.audio_sec is None:
            return None
        return round(self.audio_sec - self.video_sec, 4)

    @property
    def has_audio(self) -> bool:
        return self.audio_sec is not None

    @property
    def ok(self) -> bool:
        drift = self.drift_sec
        if self.unknown or drift is None:
            return False
        return abs(drift) <= self.tolerance_sec


async def check_av_sync(
    path: Path, *, tolerance_sec: float = DEFAULT_TOLERANCE_SEC
) -> SyncReport:
    """读一个媒体文件的音视频流时长并给出是否同步。

    测不出来（没有 ffprobe / 文件不存在）→ `unknown=True`，调用方不能当成通过。
    """
    durations = await probe_stream_durations(path)
    if durations is None:
        return SyncReport(tolerance_sec=tolerance_sec, unknown=True)
    return SyncReport(
        video_sec=durations.get("video"),
        audio_sec=durations.get("audio"),
        tolerance_sec=tolerance_sec,
        unknown=False,
    )


def format_sync_warning(report: SyncReport, label: str = "成片") -> str:
    """把检查结果写成一句给人看的警告（没问题时返回空串）。"""
    if report.unknown:
        return f"{label}音画同步：本机没有 ffprobe，跳过了音画时长检查"
    drift = report.drift_sec
    if drift is None:
        if not report.has_audio:
            return ""
        return f"{label}音画同步：读不到视频流时长，无法核对"
    if report.ok:
        return ""
    if drift < 0:
        return (
            f"{label}音画不同步：音频比视频短 {abs(drift):.2f}s"
            f"（视频 {report.video_sec:.2f}s / 音频 {report.audio_sec:.2f}s）——"
            "拼接时很可能丢掉了音频空洞，后面所有声音都会整体提前"
        )
    return (
        f"{label}音画不同步：音频比视频长 {drift:.2f}s"
        f"（视频 {report.video_sec:.2f}s / 音频 {report.audio_sec:.2f}s）——"
        "末尾可能被截断或混进了多余的静音"
    )
