"""音画时长自检的测试。

这一条自检的由来是一次真实事故：`ab_lipsync/lipsync_compare.mp4` 用
「部件音视频时长不等 + `-f concat -c copy`」拼出来，mp4 音轨表达不了空洞，
后面所有片段的音频整体提前，累计 7.154 秒（视频 41.833s / 音频 34.679s）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videogen.utils.av_sync import SyncReport, check_av_sync, format_sync_warning
from videogen.utils.ffmpeg import ffmpeg_available, run_ffmpeg

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="环境里没有 ffmpeg，跳过真实媒体用例"
)


def test_report_drift_and_ok():
    report = SyncReport(video_sec=41.833, audio_sec=34.679)
    assert report.drift_sec == pytest.approx(-7.154, abs=1e-3)
    assert not report.ok
    assert "音频比视频短" in format_sync_warning(report)


def test_report_ok_within_tolerance():
    report = SyncReport(video_sec=67.0, audio_sec=66.96)
    assert report.ok
    assert format_sync_warning(report) == ""


def test_report_unknown_is_not_ok():
    report = SyncReport(unknown=True)
    assert not report.ok
    assert "ffprobe" in format_sync_warning(report)


def test_report_audio_longer_than_video():
    report = SyncReport(video_sec=10.0, audio_sec=12.5)
    assert not report.ok
    assert "音频比视频长" in format_sync_warning(report)


def test_report_without_audio_is_quiet():
    report = SyncReport(video_sec=10.0, audio_sec=None)
    assert not report.ok
    assert format_sync_warning(report) == ""


@requires_ffmpeg
async def test_check_av_sync_on_real_files(tmp_path: Path):
    good = tmp_path / "good.mp4"
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:size=128x128:rate=12",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(good),
        ],
        timeout_sec=60,
    )
    report = await check_av_sync(good)
    assert not report.unknown
    assert report.video_sec and report.audio_sec
    assert report.ok, report.drift_sec

    # 音频只有视频的一半（等价于「拼接丢了音频空洞」）
    bad = tmp_path / "bad.mp4"
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:size=128x128:rate=12",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-map", "0:v", "-map", "1:a",
            # 故意不加 -shortest：视频 2s、音频 1s，正是「音频被截短/提前」的形态
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(bad),
        ],
        timeout_sec=60,
    )
    report = await check_av_sync(bad)
    assert not report.ok
    assert "音频比视频短" in format_sync_warning(report)


async def test_missing_file_is_unknown(tmp_path: Path):
    report = await check_av_sync(tmp_path / "nope.mp4")
    assert report.unknown
