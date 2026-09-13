"""画面运动实测的测试。

分两层：
- 纯函数（`curve_from_frames` / `activity_span` 一类）用合成帧，不需要 ffmpeg；
- 真片段（`analyze_clip` / `mouth_activity`）用 ffmpeg 生成几秒测试画面，
  因为这一层的价值就在于「真的能从画面里量出运动」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from videogen.utils import motion
from videogen.utils.ffmpeg import ffmpeg_available, run_ffmpeg

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="环境里没有 ffmpeg，跳过真实画面用例"
)

SIZE = 8
BLACK = bytes([0] * (SIZE * SIZE))
WHITE = bytes([255] * (SIZE * SIZE))


def _frames(*blocks: tuple[bytes, int]) -> list[bytes]:
    out: list[bytes] = []
    for payload, count in blocks:
        out.extend([payload] * count)
    return out


# ── 纯函数 ────────────────────────────────────────────


def test_static_clip_is_dead():
    curve = motion.curve_from_frames(_frames((BLACK, 10)), fps=6.0, size=SIZE)
    assert curve.dead
    assert curve.bursts() == []
    assert "几乎静止" in curve.describe()


def test_burst_is_located_in_time():
    # 第 0.5 秒那一下整帧变化 → 应被报成一次「剧烈动作」
    curve = motion.curve_from_frames(
        _frames((BLACK, 3), (WHITE, 1), (WHITE, 6)), fps=6.0, size=SIZE
    )
    assert not curve.dead
    # energy[i] 覆盖 [i/fps, (i+1)/fps)：第 3 帧出现变化 → 窗口 0.33~0.50s，
    # peak_time 记的是「变化落地」的时刻（0.50s）
    assert curve.peak_time() == pytest.approx(0.5, abs=0.02)
    bursts = curve.bursts()
    assert len(bursts) == 1
    assert bursts[0].start == pytest.approx(0.33, abs=0.02)
    assert "剧烈" in bursts[0].level


def test_burst_ratio_uses_baseline_not_zero():
    """有基线时按「平时的多少倍」算，而不是相对峰值。"""
    low = bytes([40] * (SIZE * SIZE))
    high = bytes([46] * (SIZE * SIZE))
    louder = bytes([90] * (SIZE * SIZE))
    # 交替两个相近的值：每一帧都在轻微变化 → 有非零基线
    alternating = [low if index % 2 else high for index in range(6)]
    curve = motion.curve_from_frames(
        [*alternating, louder, *alternating], fps=6.0, size=SIZE
    )
    bursts = curve.bursts()
    assert bursts and bursts[0].relative
    assert bursts[0].ratio > 1.6


def test_region_restricts_measurement():
    """只有右上角在变：整幅会看到运动，左下角那一块看不到。"""
    frames = [bytes(SIZE * SIZE) for _ in range(4)]
    changed = bytearray(SIZE * SIZE)
    for row in range(0, 4):
        for col in range(4, 8):
            changed[row * SIZE + col] = 200
    frames.append(bytes(changed))
    frames.extend([bytes(changed)] * 3)
    overall = motion.curve_from_frames(frames, fps=6.0, size=SIZE)
    corner = motion.curve_from_frames(
        frames, fps=6.0, size=SIZE, region=(0.0, 0.5, 0.5, 1.0)
    )
    assert overall.peak > 0
    assert corner.peak == 0


def test_too_few_frames_is_unknown():
    curve = motion.curve_from_frames([BLACK], fps=6.0, size=SIZE)
    assert curve.energy == []
    assert "未知" in motion.MotionAnalysis(
        fps=6.0, size=SIZE, frames=1, overall=curve, subject=curve
    ).describe()


def test_advice_mentions_quiet_opening_and_peak():
    curve = motion.curve_from_frames(
        _frames((BLACK, 6), (WHITE, 2), (WHITE, 6)), fps=6.0, size=SIZE
    )
    analysis = motion.MotionAnalysis(
        fps=6.0, size=SIZE, frames=14, overall=curve, subject=curve
    )
    text = analysis.describe()
    assert "念法建议" in text
    assert "开头是静的" in text


def test_summary_roundtrip_keeps_curve(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    curve = motion.curve_from_frames(
        _frames((BLACK, 3), (WHITE, 1), (WHITE, 4)), fps=6.0, size=SIZE
    )
    analysis = motion.MotionAnalysis(
        fps=6.0, size=SIZE, frames=8, overall=curve, subject=curve
    )
    cache = tmp_path / "clip.motion.json"
    motion.save_motion(cache, clip, analysis)
    loaded = motion.load_cached_motion(cache, clip)
    assert loaded is not None
    assert loaded.overall.energy == curve.energy
    assert loaded.describe() == analysis.describe()


def test_cache_invalidates_when_clip_changes(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    curve = motion.curve_from_frames(_frames((BLACK, 4), (WHITE, 4)), fps=6.0, size=SIZE)
    analysis = motion.MotionAnalysis(
        fps=6.0, size=SIZE, frames=8, overall=curve, subject=curve
    )
    cache = tmp_path / "clip.motion.json"
    motion.save_motion(cache, clip, analysis)
    clip.write_bytes(b"fake-but-longer")
    assert motion.load_cached_motion(cache, clip) is None


# ── 真片段（需要 ffmpeg）──────────────────────────────


async def _make_clip(path: Path, source: str, *, seconds: float = 1.0) -> None:
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", source,
            "-t", f"{seconds:.2f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        timeout_sec=60,
    )


@requires_ffmpeg
async def test_analyze_clip_sees_motion(tmp_path: Path):
    clip = tmp_path / "moving.mp4"
    await _make_clip(clip, "testsrc2=size=160x160:rate=12", seconds=1.5)
    analysis = await motion.analyze_clip(clip, fps=6.0, size=32)
    assert analysis is not None
    assert analysis.frames >= 4
    assert not analysis.overall.dead
    assert "画面运动" in analysis.describe()


@requires_ffmpeg
async def test_analyze_clip_reports_static_as_static(tmp_path: Path):
    clip = tmp_path / "still.mp4"
    await _make_clip(clip, "color=c=gray:size=160x160:rate=12", seconds=1.0)
    analysis = await motion.analyze_clip(clip, fps=6.0, size=32)
    assert analysis is not None
    assert analysis.overall.dead


async def test_missing_clip_returns_none(tmp_path: Path):
    assert await motion.analyze_clip(tmp_path / "nope.mp4") is None


def test_no_event_loop_required_for_pure_parts():
    """纯函数不需要事件循环（保证 CLI / 同步代码里也能用）。"""
    curve = asyncio.run(_curve_async())
    assert curve.energy


async def _curve_async() -> motion.MotionCurve:
    return motion.curve_from_frames(_frames((BLACK, 2), (WHITE, 2)), fps=6.0, size=SIZE)
