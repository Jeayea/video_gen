"""生成可直接用于演示的免版权 BGM（用 ffmpeg 合成，不含任何第三方素材）。

用法::

    python -m videogen.tools.demo_bgm               # 写到 ./assets/bgm
    python -m videogen.tools.demo_bgm --dir /tmp/bgm --duration 20 --force

生成的是简单的和弦垫音（正弦波叠加 + 颤音 + 低通），够用来验证
「按情绪选曲 → 混音 → 出片」这条链路，正式项目请换成真实音乐。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from videogen.models.audio import BgmTrack
from videogen.utils.ffmpeg import FFmpegError, ffmpeg_available, run_ffmpeg

logger = logging.getLogger(__name__)

#: id / 标题 / 情绪标签 / 和弦频率（Hz）/ 颤音频率
DEMO_TRACKS: tuple[dict, ...] = (
    {
        "id": "tense_01",
        "title": "暗涌",
        "moods": ["紧张", "悬疑", "压抑", "冲突"],
        "freqs": (110.0, 130.81, 164.81),
        "tremolo": 0.25,
        "gain_db": -22.0,
    },
    {
        "id": "warm_01",
        "title": "微光",
        "moods": ["温暖", "治愈", "希望", "轻松"],
        "freqs": (130.81, 164.81, 196.0),
        "tremolo": 0.12,
        "gain_db": -24.0,
    },
    {
        "id": "sad_01",
        "title": "别离",
        "moods": ["悲伤", "失落", "无奈", "离别"],
        "freqs": (98.0, 116.54, 146.83),
        "tremolo": 0.18,
        "gain_db": -23.0,
    },
    {
        "id": "uplift_01",
        "title": "翻盘",
        "moods": ["爽感", "热血", "逆袭", "紧张"],
        "freqs": (146.83, 185.0, 220.0),
        "tremolo": 0.3,
        "gain_db": -20.0,
    },
)


def _track_command(spec: dict, duration: float, output: Path) -> list[str]:
    args = ["-y", "-hide_banner", "-loglevel", "error"]
    for frequency in spec["freqs"]:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=44100:duration={duration}",
        ]
    count = len(spec["freqs"])
    fade_out = max(0.0, duration - 2.0)
    chain = (
        f"amix=inputs={count}:normalize=0,"
        f"tremolo=f={spec['tremolo']}:d=0.6,"
        "lowpass=f=1800,"
        "volume=0.22,"
        "afade=t=in:st=0:d=2,"
        f"afade=t=out:st={fade_out:.1f}:d=2"
    )
    args += [
        "-filter_complex",
        chain,
        "-c:a",
        "libmp3lame",
        "-b:a",
        "96k",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(output),
    ]
    return args


async def generate(
    directory: Path, *, duration: float = 45.0, force: bool = False
) -> list[BgmTrack]:
    """生成演示曲目 + `bgm.json` 元数据，返回曲目清单。"""
    if not ffmpeg_available():
        raise FFmpegError(
            "未找到 ffmpeg，无法生成演示 BGM（brew install ffmpeg 或 pip install imageio-ffmpeg）"
        )

    directory.mkdir(parents=True, exist_ok=True)
    tracks: list[BgmTrack] = []

    for spec in DEMO_TRACKS:
        output = directory / f"{spec['id']}.mp3"
        if output.exists() and not force:
            logger.info("已存在，跳过：%s（--force 可覆盖）", output.name)
        else:
            logger.info("生成演示 BGM：%s（%.0fs）", output.name, duration)
            await run_ffmpeg(_track_command(spec, duration, output), timeout_sec=duration * 6)
        tracks.append(
            BgmTrack(
                id=spec["id"],
                title=spec["title"],
                path=str(output),
                moods=list(spec["moods"]),
                duration_sec=duration,
                gain_db=spec["gain_db"],
                license="generated-by-ffmpeg (no third-party material)",
            )
        )

    metadata = {
        "note": "由 `python -m videogen.tools.demo_bgm` 生成的演示曲目，正弦波和弦，可自由替换。",
        "tracks": [
            {
                "id": track.id,
                "title": track.title,
                "file": Path(track.path).name,
                "moods": track.moods,
                "duration_sec": track.duration_sec,
                "gain_db": track.gain_db,
                "license": track.license,
            }
            for track in tracks
        ],
    }
    (directory / "bgm.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tracks


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="生成演示用免版权 BGM")
    parser.add_argument("--dir", default="assets/bgm", help="输出目录（默认 ./assets/bgm）")
    parser.add_argument("--duration", type=float, default=45.0, help="每首时长（秒）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    args = parser.parse_args(argv)

    try:
        tracks = asyncio.run(generate(Path(args.dir), duration=args.duration, force=args.force))
    except FFmpegError as exc:
        print(f"生成失败：{exc}")
        return 1

    print(f"已生成 {len(tracks)} 首演示 BGM 到 {Path(args.dir).resolve()}")
    for track in tracks:
        print(f"  - {track.id}（{track.title}）：{'、'.join(track.moods)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
