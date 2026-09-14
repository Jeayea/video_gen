#!/usr/bin/env python3
"""用**真实的混音滤镜链**离线复现一次闪避，产出可以被精确测量的两版混音。

为什么需要它：在成片里「说话段音乐被压了多少」很难量 ——
音乐比人声低 15dB 以上时，`混音² - 人声²` 这个估计会被交叉项吃掉
（实测同一个 9dB 的闪避会被读成 30dB）。而只要**同一渲染链、同一人声轨、
只有闪避开关不同**，逐样本相减剩下的就恰好是音乐那一层，误差为零。

所以这里直接调用应用里的 `compose_service.bgm_mix_filter()`（不是抄一份滤镜串），
用真实的人声轨与真实的配乐文件渲染出：

    mix_off.wav   音乐固定音量
    mix_on.wav    音乐 + 人声闪避（sidechaincompress）

然后用 `measure_ducking.py --mode residual` 量。

用法：
    .venv/bin/python tools/music/ducking_harness.py \\
        --voice projects/<id>/ab_music/audio/A_nobgm.wav \\
        --music projects/<id>/output/music/bgm.wav \\
        --out projects/<id>/ab_music/harness --duration 67.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from videogen.config import get_settings  # noqa: E402
from videogen.services.compose_service import bgm_mix_filter  # noqa: E402
from videogen.utils.ffmpeg import run_ffmpeg  # noqa: E402


async def render(
    voice: Path, music: Path, out: Path, *, duck: bool, duration: float, settings
) -> str:
    chain = bgm_mix_filter(
        gain_db=settings.bgm_gain_db,
        fade_sec=settings.bgm_fade_sec,
        duration=duration,
        duck=duck,
        duck_ratio=settings.bgm_duck_ratio,
        duck_threshold_db=settings.bgm_duck_threshold_db,
        duck_attack_ms=settings.bgm_duck_attack_ms,
        duck_release_ms=settings.bgm_duck_release_ms,
    )
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(voice),
            "-i", str(music),
            "-filter_complex", chain,
            "-map", "[a]",
            "-c:a", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            "-t", f"{duration:.3f}",
            str(out),
        ],
        timeout_sec=300,
    )
    return chain


async def main() -> int:
    parser = argparse.ArgumentParser(description="用真实滤镜链复现闪避，产出可精确对比的两版混音")
    parser.add_argument("--voice", required=True, help="人声轨 wav（例如无 BGM 的成片音轨）")
    parser.add_argument("--music", required=True, help="配乐 wav")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--duration", type=float, required=True, help="成片时长（秒）")
    args = parser.parse_args()

    voice, music = Path(args.voice), Path(args.music)
    for path in (voice, music):
        if not path.is_file():
            raise SystemExit(f"找不到文件：{path}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    off, on = out_dir / "mix_off.wav", out_dir / "mix_on.wav"
    chain_off = await render(
        voice, music, off, duck=False, duration=args.duration, settings=settings
    )
    chain_on = await render(
        voice, music, on, duck=True, duration=args.duration, settings=settings
    )

    print(f"人声：{voice}")
    print(f"配乐：{music}（音量 {settings.bgm_gain_db}dB）")
    print(f"固定音量：{off}")
    print(f"人声闪避：{on}")
    print()
    print("固定音量滤镜链：")
    print(f"  {chain_off}")
    print("闪避滤镜链：")
    print(f"  {chain_on}")
    print()
    print("下一步：")
    print(
        f"  .venv/bin/python tools/music/measure_ducking.py --mode residual \\\n"
        f"      --voice {voice} \\\n"
        f"      --variant \"固定音量={off}\" --variant \"人声闪避={on}\" \\\n"
        f"      --reference \"固定音量={off}\""
    )

    (out_dir / "harness.json").write_text(
        json.dumps(
            {
                "voice": str(voice),
                "music": str(music),
                "duration_sec": args.duration,
                "bgm": {
                    key: value
                    for key, value in settings.model_dump().items()
                    if key.startswith("bgm_")
                },
                "filter_off": chain_off,
                "filter_on": chain_on,
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
