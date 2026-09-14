"""响度测量与归一（`utils/loudness.py`）的测试。

这一层存在的理由是实测出来的：同一部片子里不同音色的配音能差 7.9dB。
所以测试盯的是三件事 —— 量得准（EBU R128 解析）、算得对（有上限的定值增益）、
以及「念法里的音量意图」不会被归一吃掉。
"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path

import pytest

from videogen.utils import loudness
from videogen.utils.ffmpeg import ffmpeg_available

requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="需要 ffmpeg")


def write_tone(path: Path, *, seconds: float = 4.0, amplitude: float = 0.25) -> Path:
    """写一段稳态正弦（440Hz），响度可预期。"""
    rate = 44100
    frames = array.array("h", bytes(4 * int(seconds * rate)))
    for index in range(int(seconds * rate)):
        value = amplitude * math.sin(2 * math.pi * 440.0 * index / rate)
        lead = 0.05 * rate
        if index < lead or index > (seconds - 0.05) * rate:  # 两端淡入淡出，避免爆音
            value = 0.0
        frames[2 * index] = int(value * 32767)
        frames[2 * index + 1] = int(value * 32767)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames.tobytes())
    return path


def write_silence(path: Path, *, seconds: float = 2.0) -> Path:
    rate = 44100
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(4 * int(seconds * rate)))
    return path


# ── 念法音量换算 ───────────────────────────────────────


def test_volume_to_db_percent_is_amplitude_not_db():
    """edge-tts 的 `--volume=+5%` 是**振幅** +5%，换算成 dB 是 +0.42 而不是 +5。"""
    assert loudness.volume_to_db("+5%") == pytest.approx(0.42, abs=0.01)
    assert loudness.volume_to_db("-20%") == pytest.approx(-1.94, abs=0.01)
    assert loudness.volume_to_db("100%") == pytest.approx(6.02, abs=0.01)
    assert loudness.volume_to_db("-100%") == pytest.approx(-40.0, abs=0.5)


def test_volume_to_db_plain_and_bad_values():
    assert loudness.volume_to_db("-3dB") == -3.0
    assert loudness.volume_to_db("-3") == -3.0
    assert loudness.volume_to_db("") == 0.0
    assert loudness.volume_to_db("稍慢一点") == 0.0


# ── 增益计算 ───────────────────────────────────────────


def test_gain_for_hits_target_within_limit():
    assert loudness.gain_for(-18.0, target=-20.0, max_db=6.0) == -2.0
    assert loudness.gain_for(-24.0, target=-20.0, max_db=6.0) == 4.0


def test_gain_for_clamps_extremes():
    # 差 12dB 但只允许修 6dB：剩下的差异当作有意的表演
    assert loudness.gain_for(-32.0, target=-20.0, max_db=6.0) == 6.0
    assert loudness.gain_for(-8.0, target=-20.0, max_db=6.0) == -6.0


def test_gain_for_keeps_intent_and_handles_unknown():
    # 念法里的 +5%（+0.42dB）要在归一之后加回来
    assert loudness.gain_for(-22.0, target=-20.0, max_db=6.0, intent_db=0.42) == 2.42
    # 量不出来（静音/失败）时不修正，但仍保留意图
    assert loudness.gain_for(None, target=-20.0, max_db=6.0) == 0.0
    assert loudness.gain_for(None, target=-20.0, max_db=6.0, intent_db=-1.94) == -1.94


# ── ffmpeg 输出解析 ───────────────────────────────────


def test_parse_ebur128_summary():
    output = """
[Parsed_ebur128_0 @ 0x1] t: 3.9 M: -20.4 S: -21.0 I: -21.9 LUFS
[Parsed_ebur128_0 @ 0x1] Integrated loudness:
    I:         -21.9 LUFS
    Threshold: -32.0 LUFS

  Loudness range:
    LRA:         0.8 LU
    Threshold: -42.2 LUFS

  True peak:
    Peak:       -6.2 dBFS
"""
    result = loudness._parse(output)
    assert result.integrated == -21.9
    assert result.true_peak == -6.2
    assert result.lra == 0.8
    assert result.ok


def test_parse_treats_silence_as_unknown():
    output = """
  Integrated loudness:
    I:          -inf LUFS
  Loudness range:
    LRA:         -inf LU
  True peak:
    Peak:        -inf dBFS
"""
    result = loudness._parse(output)
    assert result.integrated is None
    assert not result.ok


def test_parse_of_garbage_is_empty():
    result = loudness._parse("没有任何可解析内容")
    assert result.integrated is None
    assert result.true_peak is None


# ── 真实测量 ───────────────────────────────────────────


@requires_ffmpeg
@pytest.mark.asyncio
async def test_measure_tone_and_silence(tmp_path):
    loud = write_tone(tmp_path / "loud.wav", amplitude=0.5)
    quiet = write_tone(tmp_path / "quiet.wav", amplitude=0.05)
    silent = write_silence(tmp_path / "silent.wav")

    loud_info = await loudness.measure(loud)
    quiet_info = await loudness.measure(quiet)
    silent_info = await loudness.measure(silent)

    assert loud_info.ok and quiet_info.ok
    # 振幅差 20dB，集成响度也应该差 ~20dB
    assert loud_info.integrated - quiet_info.integrated == pytest.approx(20.0, abs=1.5)
    assert loud_info.true_peak > quiet_info.true_peak
    # 整条静音：测不到（而不是返回一个假数字）
    assert silent_info.integrated is None
    assert not silent_info.ok


@requires_ffmpeg
@pytest.mark.asyncio
async def test_measure_missing_file_and_dir(tmp_path):
    missing = await loudness.measure(tmp_path / "nope.wav")
    assert missing.integrated is None
    assert missing.error
    batch = await loudness.measure_many([tmp_path / "nope.wav", tmp_path / "nope.wav"])
    assert len(batch) == 1  # 同一个路径只量一次


@requires_ffmpeg
@pytest.mark.asyncio
async def test_normalization_brings_levels_together(tmp_path):
    """把「实测 → 算增益」跑一遍：两个差 14dB 的文件应该被拉到同一个响度。"""
    loud = write_tone(tmp_path / "a.wav", amplitude=0.5)
    quiet = write_tone(tmp_path / "b.wav", amplitude=0.1)
    # 上限给足（这里就是要验证「能拉齐」；上限本身由 test_gain_for_clamps_extremes 覆盖）
    target, limit = -20.0, 20.0
    gains = []
    measured = []
    for path in (loud, quiet):
        info = await loudness.measure(path)
        measured.append(info.integrated)
        gains.append(loudness.gain_for(info.integrated, target=target, max_db=limit))
    fixed = [value + gain for value, gain in zip(measured, gains, strict=True)]
    assert max(fixed) - min(fixed) < 0.2
    assert all(abs(value - target) < 0.2 for value in fixed)
