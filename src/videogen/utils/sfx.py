"""黑场音效：本机合成（纯标准库，$0，不联网）。

只做剧情里最常用、又没法靠配音念出来的几种电话声音（`SFX_NAMES`）：

- `phone_ring`：手机来电铃声（两音交替的电子铃，响 1 秒停 0.6 秒，循环）
- `phone_vibrate`：手机震动（低频嗡嗡，0.45 秒一下，循环）
- `call_connect`：接通的轻响（一次性）
- `busy_tone`：忙音（中国大陆制式：450 Hz，响 0.35 秒停 0.35 秒，循环）
- `hang_up`：挂断的轻响（一次性）

排法：按列表顺序排进这一镜。一次性的占自己的长度，循环的平分剩下的时间。
例：`["phone_ring", "call_connect"]` = 铃声一直响，最后一下接通。
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

from videogen.models.transition import SFX_NAMES

SAMPLE_RATE = 44100
#: 一次性音效的长度（秒）；其余是循环类
ONE_SHOT_SEC = {"call_connect": 0.18, "hang_up": 0.22}


def fixed_seconds(names: list[str]) -> float:
    """一次性音效加起来要多久（循环类不算：它们铺满剩下的时间）。"""
    return sum(ONE_SHOT_SEC.get(n, 0.0) for n in names)


def has_loop(names: list[str]) -> bool:
    return any(n in SFX_NAMES and n not in ONE_SHOT_SEC for n in names)


def render_track(names: list[str], seconds: float, path: Path, *, seed: int = 7) -> Path:
    """把音效按顺序排成一条 `seconds` 秒的立体声 WAV。没有可用音效时写一条静音。"""
    total = max(1, round(max(0.1, seconds) * SAMPLE_RATE))
    names = [n for n in names if n in SFX_NAMES]
    fixed = sum(round(ONE_SHOT_SEC.get(n, 0.0) * SAMPLE_RATE) for n in names)
    loops = [n for n in names if n not in ONE_SHOT_SEC]
    loop_len = (total - fixed) // len(loops) if loops else 0
    rng = random.Random(seed)
    samples: list[float] = []
    for name in names:
        length = round(ONE_SHOT_SEC[name] * SAMPLE_RATE) if name in ONE_SHOT_SEC else loop_len
        samples.extend(_SYNTH[name](max(0, length), rng))
    samples = (samples + [0.0] * total)[:total]
    _write_stereo(path, samples)
    return path


# ── 合成 ──────────────────────────────────────────────


def _env(i: int, n: int, attack: float = 0.004, release: float = 0.01) -> float:
    """短促的起落包络，避免爆音。"""
    a, r = attack * SAMPLE_RATE, release * SAMPLE_RATE
    return min(1.0, i / a if a else 1.0, (n - i) / r if r else 1.0)


def _gated(length: int, on_sec: float, off_sec: float, tone) -> list[float]:
    period = round((on_sec + off_sec) * SAMPLE_RATE)
    on = round(on_sec * SAMPLE_RATE)
    out = []
    for i in range(length):
        k = i % period
        out.append(tone(i, k, on) * _env(k, on) if k < on else 0.0)
    return out


def _ring(length: int, rng: random.Random) -> list[float]:
    def tone(i: int, k: int, on: int) -> float:
        freq = 1046.5 if (k // round(0.05 * SAMPLE_RATE)) % 2 == 0 else 1318.5  # C6 / E6 交替
        t = i / SAMPLE_RATE
        return 0.2 * (math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t))

    return _gated(length, 1.0, 0.6, tone)


def _vibrate(length: int, rng: random.Random) -> list[float]:
    def tone(i: int, k: int, on: int) -> float:
        t = i / SAMPLE_RATE
        buzz = sum(math.sin(2 * math.pi * 170 * h * t) / h for h in (1, 2, 3))
        wobble = 0.85 + 0.15 * math.sin(2 * math.pi * 23 * t)
        return 0.22 * buzz * wobble + rng.uniform(-0.01, 0.01)

    return _gated(length, 0.45, 0.35, tone)


def _click(length: int, rng: random.Random, *, freq: float) -> list[float]:
    out = []
    for i in range(length):
        t = i / SAMPLE_RATE
        noise = rng.uniform(-1, 1) * math.exp(-t / 0.004) * 0.35
        blip = math.sin(2 * math.pi * freq * t) * math.exp(-t / 0.03) * 0.18
        out.append((noise + blip) * _env(i, length, attack=0.0005, release=0.02))
    return out


def _busy(length: int, rng: random.Random) -> list[float]:
    def tone(i: int, k: int, on: int) -> float:
        return 0.16 * math.sin(2 * math.pi * 450 * i / SAMPLE_RATE)

    return _gated(length, 0.35, 0.35, tone)


_SYNTH = {
    "phone_ring": _ring,
    "phone_vibrate": _vibrate,
    "call_connect": lambda n, rng: _click(n, rng, freq=900),
    "busy_tone": _busy,
    "hang_up": lambda n, rng: _click(n, rng, freq=600),
}


def _write_stereo(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for value in samples:
        v = int(max(-1.0, min(1.0, value)) * 32767)
        frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(bytes(frames))
