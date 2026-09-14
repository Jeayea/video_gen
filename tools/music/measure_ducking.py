#!/usr/bin/env python3
"""量「人声闪避」到底有没有生效：逐窗口比较不同混音的响度。

为什么需要这个工具：`sidechaincompress` 写进滤镜图不代表它真的压下去了
（`afir` 那种「配乐整条归零」的事故就是靠量出来的数字才发现的）。
这里用**纯标准库**读 WAV，自己找说话窗口，然后逐窗口比 RMS：

    python tools/music/measure_ducking.py \\
        --voice ab_music/audio/A_nobgm.wav \\
        --variant "无 BGM=A_nobgm.wav" \\
        --variant "有 BGM 固定音量=B_noduck.wav" \\
        --variant "有 BGM + 人声闪避=C_duck.wav" \\
        --reference "有 BGM 固定音量=B_noduck.wav" \\
        --report ab_music/ducking_report.json

两种口径：

- `--mode energy`（默认）：估音乐能量 = `混音² - 人声²`。适合「音乐本身就比较响」的对照，
  音乐被人声压了 15dB 以上时这个估计会失真（交叉项占主导，实测会把 9dB 读成 30dB）。
- `--mode residual`：**逐样本相减**（要求两版是同一渲染链、同一人声轨，只有效果器开关不同），
  `混音 - 人声` 就是音乐那一层，误差为零。想精确量闪避深度就用这个
  （`tools/music/ducking_harness.py` 会用真实滤镜链渲染出这样一对文件）。

判据（写在 README 里，避免事后编标准）：
- **说话窗口**：闪避版必须比固定音量版低 **≥4dB**（否则等于没压）
- **静音窗口**：两版之差必须 **<1.5dB**（否则是把整条音乐压小了，不是闪避）
"""

from __future__ import annotations

import argparse
import array
import json
import math
import wave
from dataclasses import dataclass
from pathlib import Path

#: 分析窗口（秒）
FRAME_SEC = 0.02
#: 说话判定：高于本片噪声底这么多 dB
SPEECH_ABOVE_FLOOR_DB = 6.0
#: 太短的说话/静音片段丢掉（避免嘴唇碰一下、换气被当成一段）
MIN_SPEECH_SEC = 0.30
MIN_SILENCE_SEC = 0.40
#: 窗口边缘各留这么多秒不算（淡入淡出、压缩器起停时间）
EDGE_GUARD_SEC = 0.15
#: 闪避判定门槛
DUCK_MIN_DB = 4.0
SILENCE_MAX_DIFF_DB = 1.5
#: 估计口径里超过这个压降就不可信（压得越深，交叉项占比越大）
IMPLAUSIBLE_DUCK_DB = 18.0
#: 「静音窗口」里只有够长的才算数：压缩器 release 期间音乐本来就还压着，
#: 短促的换气间隙被压住是**正常行为**，不是「整体压低」
LONG_SILENCE_SEC = 1.2


@dataclass
class Level:
    """一条音轨的整体响度。"""

    rms_db: float
    peak: float
    duration_sec: float


def read_wav(path: Path) -> tuple[array.array, int, int]:
    """读 16bit WAV，返回（交错样本, 采样率, 声道数）。"""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise SystemExit(f"只支持 16bit WAV：{path}（{width * 8}bit）")
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    return samples, rate, channels


def mono_frames(samples: array.array, channels: int, rate: int) -> list[float]:
    """按 `FRAME_SEC` 切窗，返回每个窗口的 RMS（0..1）。"""
    per_frame = max(1, int(rate * FRAME_SEC))
    step = per_frame * max(1, channels)
    out: list[float] = []
    for start in range(0, len(samples) - step + 1, step):
        total = 0.0
        for index in range(start, start + step, channels):
            value = samples[index] / 32768.0
            total += value * value
        out.append(math.sqrt(total / per_frame))
    return out


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * ratio)))
    return ordered[index]


def speech_windows(frames: list[float], *, rate: int) -> tuple[list[tuple[float, float]], float]:
    """从人声轨找出说话窗口（秒），同时返回噪声底（dBFS）。"""
    floor = max(1e-9, _percentile(frames, 0.05))
    threshold = floor * (10 ** (SPEECH_ABOVE_FLOOR_DB / 20.0))
    runs: list[list[int]] = []
    current: list[int] = []
    for index, value in enumerate(frames):
        if value >= threshold:
            current.append(index)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    windows: list[tuple[float, float]] = []
    for run in runs:
        start = run[0] * FRAME_SEC
        end = (run[-1] + 1) * FRAME_SEC
        if end - start >= MIN_SPEECH_SEC:
            windows.append((round(start, 3), round(end, 3)))
    return windows, round(20.0 * math.log10(floor), 2)


def invert_windows(
    windows: list[tuple[float, float]], duration: float, *, min_sec: float = MIN_SILENCE_SEC
) -> list[tuple[float, float]]:
    """说话窗口的补集 = 静音窗口。"""
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in windows:
        if start - cursor >= min_sec:
            gaps.append((round(cursor, 3), round(start, 3)))
        cursor = max(cursor, end)
    if duration - cursor >= min_sec:
        gaps.append((round(cursor, 3), round(duration, 3)))
    return gaps


def energy_in(samples: array.array, rate: int, channels: int, window: tuple[float, float],
              *, guard: float = EDGE_GUARD_SEC) -> float:
    """一段窗口内的均方值（0..1 的平方），窗口太短返回 0。"""
    start = int((window[0] + guard) * rate)
    end = int((window[1] - guard) * rate)
    if end <= start:
        return 0.0
    total = 0.0
    count = 0
    step = max(1, channels)
    for index in range(start * channels, min(end * channels, len(samples)), step):
        value = samples[index] / 32768.0
        total += value * value
        count += 1
    return total / count if count else 0.0


def _db(value: float) -> float:
    return round(10.0 * math.log10(value), 2) if value > 1e-12 else -120.0


def music_energy(mix: float, voice: float) -> float:
    """从「混音能量 - 人声能量」估出音乐自己的能量。

    为什么不能直接比混音响度：说话时人声本来就压着音乐（实测人声比音乐响 5~15dB），
    闪避 10dB 在总响度上只体现 0.5~1dB —— 那样得出的结论会是「没生效」。
    减去人声能量才是「音乐那一层被压了多少」。
    """
    return max(0.0, mix - voice)


def overall(path: Path) -> Level:
    samples, rate, channels = read_wav(path)
    total = 0.0
    peak = 0
    for index in range(0, len(samples), max(1, channels)):
        value = samples[index]
        magnitude = value if value >= 0 else -value
        if magnitude > peak:
            peak = magnitude
        normalised = value / 32768.0
        total += normalised * normalised
    count = max(1, len(samples) // max(1, channels))
    rms = math.sqrt(total / count)
    return Level(
        rms_db=round(20.0 * math.log10(rms), 2) if rms > 1e-9 else -120.0,
        peak=round(peak / 32768.0, 4),
        duration_sec=round(len(samples) / max(1, channels * rate), 3),
    )


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else -120.0


def subtract(samples: array.array, other: array.array) -> array.array:
    """逐样本相减（同一渲染链的两版混音 → 差的就是效果器那部分）。"""
    count = min(len(samples), len(other))
    out = array.array("h", bytes(count * 2))
    for index in range(count):
        value = samples[index] - other[index]
        out[index] = max(-32768, min(32767, value))
    return out


def measure(
    path: Path,
    speech: list[tuple[float, float]],
    gaps: list[tuple[float, float]],
    *,
    voice: tuple[array.array, int, int],
    residual: bool = False,
) -> dict:
    """量一条混音：整体响度 + 说话/静音窗口里的「人声」与「音乐」。

    `residual=True` 时先做逐样本相减，剩下的就是音乐那一层（精确口径）。
    """
    samples, rate, channels = read_wav(path)
    voice_samples, voice_rate, voice_channels = voice
    if residual:
        if (rate, channels) != (voice_rate, voice_channels):
            raise SystemExit("residual 模式要求两版采样率/声道一致")
        samples = subtract(samples, voice_samples)

    def windows(items: list[tuple[float, float]]) -> tuple[list[float], list[float], list[float]]:
        mix_values: list[float] = []
        voice_values: list[float] = []
        music_values: list[float] = []
        for window in items:
            level = energy_in(samples, rate, channels, window)
            human = energy_in(voice_samples, voice_rate, voice_channels, window)
            # residual 模式下 `samples` 已经是「音乐那一层」，不能再减一次人声
            music = level if residual else music_energy(level, human)
            mix_values.append(_db(level + (human if residual else 0.0)))
            voice_values.append(_db(human))
            music_values.append(_db(music))
        return mix_values, voice_values, music_values

    speech_mix, speech_voice, speech_music = windows(speech)
    # 只拿「够长的静音段」判「是不是整体压低」：短间隙里压缩器还没 release 完
    long_gaps = [window for window in gaps if window[1] - window[0] >= LONG_SILENCE_SEC] or gaps
    gap_mix, _gap_voice, gap_music = windows(long_gaps)
    return {
        "path": str(path),
        "overall": overall(path).__dict__,
        "speech_db": _mean(speech_mix),
        "speech_voice_db": _mean(speech_voice),
        "speech_music_db": _mean(speech_music),
        "silence_db": _mean(gap_mix),
        "silence_music_db": _mean(gap_music),
        "speech_windows": len(speech),
        "silence_windows": len(long_gaps),
        "silence_windows_all": len(gaps),
    }


def parse_variant(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise SystemExit(f"--variant 格式应为 「说明=文件」：{spec}")
    label, _, raw = spec.partition("=")
    path = Path(raw)
    if not path.is_file():
        raise SystemExit(f"找不到文件：{path}")
    return label, path


def main() -> int:
    parser = argparse.ArgumentParser(description="量人声闪避到底有没有生效")
    parser.add_argument("--voice", required=True, help="只有人声的混音（用来找说话窗口）")
    parser.add_argument("--variant", action="append", required=True, help="说明=文件（可多次）")
    parser.add_argument("--reference", default="", help="对照组说明=文件（默认第一个 variant）")
    parser.add_argument("--report", default="", help="把结果写成 JSON")
    parser.add_argument(
        "--mode",
        default="energy",
        choices=("energy", "residual"),
        help="energy = 混音²-人声²（默认）；residual = 逐样本相减（精确，需同一渲染链）",
    )
    args = parser.parse_args()

    voice_path = Path(args.voice)
    if not voice_path.is_file():
        raise SystemExit(f"找不到人声文件：{voice_path}")
    samples, rate, channels = read_wav(voice_path)
    frames = mono_frames(samples, channels, rate)
    speech, floor_db = speech_windows(frames, rate=rate)
    duration = len(samples) / max(1, channels * rate)
    gaps = invert_windows(speech, duration)
    if not speech or not gaps:
        raise SystemExit("找不到足够的说话/静音窗口，无法比较（人声轨太短或一直在说话）")

    variants = [parse_variant(spec) for spec in args.variant]
    reference_label = ""
    if args.reference:
        reference_label, reference_path = parse_variant(args.reference)
    else:
        reference_label, reference_path = variants[0]

    voice = (samples, rate, channels)
    residual = args.mode == "residual"
    results = {
        label: measure(path, speech, gaps, voice=voice, residual=residual)
        for label, path in variants
    }
    reference = results.get(reference_label) or measure(
        reference_path, speech, gaps, voice=voice, residual=residual
    )
    if reference_label not in results:
        results[reference_label] = reference

    print(
        f"人声轨：{voice_path.name}（{duration:.2f}s，噪声底 {floor_db} dBFS，"
        f"口径 {'逐样本相减（精确）' if residual else '能量相减（估计）'}）"
    )
    long_gaps = [window for window in gaps if window[1] - window[0] >= LONG_SILENCE_SEC]
    print(
        f"说话窗口 {len(speech)} 段（合计 {sum(e - s for s, e in speech):.1f}s）"
        f" / 静音窗口 {len(gaps)} 段（其中 ≥{LONG_SILENCE_SEC:g}s 的 {len(long_gaps)} 段参与判定）"
    )
    print()
    header = (
        f"{'版本':<24}{'整体':>8}{'说话人声':>10}{'说话音乐':>10}{'静音音乐':>10}"
        f"{'说话音乐-对照':>15}{'静音音乐-对照':>15}"
    )
    print(header)
    print("-" * len(header))
    deltas: dict[str, dict[str, float]] = {}
    for label, _ in variants:
        item = results[label]
        speech_delta = round(item["speech_music_db"] - reference["speech_music_db"], 2)
        silence_delta = round(item["silence_music_db"] - reference["silence_music_db"], 2)
        deltas[label] = {
            "speech_music_db": speech_delta,
            "silence_music_db": silence_delta,
            "speech_db": round(item["speech_db"] - reference["speech_db"], 2),
            "silence_db": round(item["silence_db"] - reference["silence_db"], 2),
        }
        print(
            f"{label:<24}{item['overall']['rms_db']:>8.2f}{item['speech_voice_db']:>10.2f}"
            f"{item['speech_music_db']:>10.2f}{item['silence_music_db']:>10.2f}"
            f"{speech_delta:>+15.2f}{silence_delta:>+15.2f}"
        )

    verdict = "无法判定"
    checked = [
        (label, delta)
        for label, delta in deltas.items()
        if label != reference_label and delta["speech_music_db"] <= -DUCK_MIN_DB
    ]
    if checked:
        good = [
            (label, delta)
            for label, delta in checked
            if abs(delta["silence_music_db"]) <= SILENCE_MAX_DIFF_DB
        ]
        if good:
            label, delta = good[0]
            verdict = (
                f"✅ 闪避生效：「{label}」说话时音乐比对照低 "
                f"{abs(delta['speech_music_db']):.1f}dB，不说话时只差 "
                f"{delta['silence_music_db']:+.1f}dB，人声不变"
            )
            if not residual and abs(delta["speech_music_db"]) > IMPLAUSIBLE_DUCK_DB:
                verdict += (
                    f"；⚠️ 但这是**估计口径**，超过 {IMPLAUSIBLE_DUCK_DB:g}dB 的压降不可信"
                    "（音乐被压深之后交叉项占主导，会把小压降放大）——"
                    "要精确值用 --mode residual 配 tools/music/ducking_harness.py"
                )
        else:
            label, delta = checked[0]
            verdict = (
                f"⚠️ 是整体压低了而不是闪避：「{label}」说话时音乐低 "
                f"{abs(delta['speech_music_db']):.1f}dB，但静音段也低了 "
                f"{abs(delta['silence_music_db']):.1f}dB"
            )
    print()
    print(verdict)

    if args.report:
        Path(args.report).write_text(
            json.dumps(
                {
                    "voice": str(voice_path),
                    "noise_floor_db": floor_db,
                    "speech_windows": speech,
                    "silence_windows": long_gaps,
                    "silence_windows_all": gaps,
                    "long_silence_sec": LONG_SILENCE_SEC,
                    "mode": args.mode,
                    "reference": reference_label,
                    "variants": results,
                    "deltas_vs_reference": deltas,
                    "criteria": {
                        "duck_min_db": DUCK_MIN_DB,
                        "silence_max_diff_db": SILENCE_MAX_DIFF_DB,
                    },
                    "verdict": verdict,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"报告：{args.report}")
    return 0 if verdict.startswith("✅") else 1


if __name__ == "__main__":
    raise SystemExit(main())
