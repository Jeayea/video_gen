#!/usr/bin/env python3
"""把几个「同一个镜头、不同做法」的版本拼成一条**音画同步**的对比片。

修复的事故（2026-09-12 实测）：上一版对比片用「每个部件音视频时长不等 →
`-f concat -c copy`」拼接，mp4 的音轨表达不了空洞，后面所有片段的音频整体提前，
累计漂移 **7.154 秒**（视频 41.833s / 音频 34.679s），用户看到的就是
「语音和视频是分开的」。这里的规矩只有一条：

    **每个部件的音轨时长必须严格等于它的画面时长，再拼接。**

做法：给每个部件算一个精确时长（说明卡 = 旁白 + 0.6s 静音；片段 = 画面时长），
画面用 `-t` 截准、音频用 `apad=whole_dur=…,atrim=duration=…` 补/截到同一长度，
最后用 **concat 滤镜**（不是 demuxer）拼接，并用 `videogen.utils.av_sync` 复验。

用法（在主环境跑，脚本内部会调独立环境的口型引擎）：
    .venv/bin/python tools/lipsync/build_compare.py --out DIR \\
        --narration-voice zh-CN-YunxiNeural \\
        --part "方案一：现状|A.mp4|dub.wav" \\
        --part "方案四：逐帧口型|B.mp4|dub.wav|lipsync"
其中 `--part` 的格式是 `说明文字|画面文件|配音文件[|lipsync]`，
配音文件写成 `native` 表示用片段自带音轨。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from videogen.utils.av_sync import check_av_sync, format_sync_warning  # noqa: E402
from videogen.utils.ffmpeg import probe_duration, probe_streams, run_ffmpeg  # noqa: E402

#: 说明卡：黑底 + 旁白，尾巴留白
CARD_TAIL_SEC = 0.6
#: 统一规格（竖屏 1080×1920 / 24fps / 44.1kHz 立体声）
WIDTH, HEIGHT, FPS, RATE = 1080, 1920, 24, 44100
AUDIO_FMT = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
VIDEO_FMT = (
    f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS},"
    "format=yuv420p"
)
ENCODE = ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]


async def _duration(path: Path) -> float:
    value = await probe_duration(path)
    if not value:
        raise SystemExit(f"读不到时长：{path}")
    return value


async def _has_audio(path: Path) -> bool:
    streams = await probe_streams(path)
    return bool(streams and "audio" in streams)


async def make_card(text: str, *, voice: str, out: Path, work: Path) -> Path:
    """黑底说明卡：时长 = 旁白 + 尾巴，音画严格等长。"""
    from videogen.providers.tts.base import TTSRequest
    from videogen.providers.tts.edge import EdgeTTSProvider

    speech = work / f"card_{out.stem}.mp3"
    if not speech.is_file():
        provider = EdgeTTSProvider()
        await provider.synthesize(
            TTSRequest(
                clip_id=out.stem, text=text, voice=voice, rate="+10%", output_path=speech
            )
        )
    total = await _duration(speech) + CARD_TAIL_SEC
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=0x101010:s={WIDTH}x{HEIGHT}:r={FPS}",
            "-i", str(speech),
            "-filter_complex",
            f"[0:v]trim=duration={total:.3f},setpts=PTS-STARTPTS[v];"
            f"[1:a]{AUDIO_FMT},apad,atrim=duration={total:.3f},"
            "asetpts=PTS-STARTPTS[a]",
            "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
            *ENCODE, "-c:a", "aac", "-b:a", "128k", str(out),
        ],
        timeout_sec=300,
    )
    return out


async def make_segment(
    source: Path,
    audio: Path | None,
    *,
    out: Path,
    lipsync_python: Path | None,
    lipsync_args: str = "",
) -> Path:
    """一个方案片段：画面 + 配音，两者严格等长（配音短就补静音，长就截断）。"""
    total = await _duration(source)
    if lipsync_python is not None and audio is not None:
        # 用真正的逐帧口型引擎重画嘴部（引擎只输出画面，音轨在下面统一加）
        command = [
            str(lipsync_python),
            str(Path(__file__).resolve().parent / "wav2lip_onnx.py"),
            "--video", str(source),
            "--audio", str(audio),
            "--out", str(out.with_name(f"{out.stem}.mouth.mp4")),
            "--report", str(out.with_name(f"{out.stem}.mouth.json")),
            *shlex.split(lipsync_args),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SystemExit(f"逐帧口型失败：{result.stderr.strip()[-300:]}")
        source = out.with_name(f"{out.stem}.mouth.mp4")

    if audio is None:
        await run_ffmpeg(
            [
                "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={RATE}",
                "-filter_complex",
                f"[0:v]{VIDEO_FMT},trim=duration={total:.3f},setpts=PTS-STARTPTS[v];"
                f"[1:a]{AUDIO_FMT},atrim=duration={total:.3f},asetpts=PTS-STARTPTS[a]",
                "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
                *ENCODE, "-c:a", "aac", "-b:a", "128k", str(out),
            ],
            timeout_sec=600,
        )
        return out

    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-i", str(audio),
            "-filter_complex",
            f"[0:v]{VIDEO_FMT},trim=duration={total:.3f},setpts=PTS-STARTPTS[v];"
            f"[1:a]{AUDIO_FMT},apad,atrim=duration={total:.3f},asetpts=PTS-STARTPTS[a]",
            "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
            *ENCODE, "-c:a", "aac", "-b:a", "128k", str(out),
        ],
        timeout_sec=600,
    )
    return out


async def build(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "parts"
    work.mkdir(exist_ok=True)
    lipsync_python = Path(args.lipsync_python) if args.lipsync else None
    if lipsync_python is not None and not lipsync_python.is_file():
        raise SystemExit(f"找不到口型引擎解释器：{lipsync_python}")

    parts: list[Path] = []
    for index, spec in enumerate(args.part, start=1):
        fields = spec.split("|")
        if len(fields) < 2:
            raise SystemExit(f"--part 格式不对：{spec}")
        label, video = fields[0], Path(fields[1])
        audio_field = fields[2] if len(fields) > 2 else ""
        use_lipsync = len(fields) > 3 and fields[3].strip() == "lipsync"
        if not video.is_file():
            raise SystemExit(f"找不到画面文件：{video}")

        card = await make_card(
            label, voice=args.narration_voice, out=work / f"card_{index}.mp4", work=work
        )
        parts.append(card)

        if audio_field in ("", "none"):
            audio: Path | None = None
        elif audio_field == "native":
            audio = video if await _has_audio(video) else None
        else:
            audio = Path(audio_field)
            if not audio.is_file():
                raise SystemExit(f"找不到配音文件：{audio}")
        segment = await make_segment(
            video,
            audio,
            out=work / f"seg_{index}.mp4",
            lipsync_python=lipsync_python if use_lipsync else None,
            lipsync_args=args.lipsync_args,
        )
        parts.append(segment)
        print(f"[{index}] {label[:28]}… → {segment.name}")

    final = out_dir / args.output_name
    inputs: list[str] = []
    for path in parts:
        inputs += ["-i", str(path)]
    chain = "".join(f"[{i}:v][{i}:a]" for i in range(len(parts)))
    chain += f"concat=n={len(parts)}:v=1:a=1[v][a]"
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            *inputs,
            "-filter_complex", chain,
            "-map", "[v]", "-map", "[a]",
            *ENCODE, "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(final),
        ],
        timeout_sec=900,
    )

    report = await check_av_sync(final)
    note = format_sync_warning(report, "对比片")
    print()
    print(f"产出：{final}")
    print(
        f"音画：视频 {report.video_sec:.3f}s / 音频 {report.audio_sec:.3f}s"
        f"（差 {report.drift_sec:+.3f}s）"
    )
    if note:
        print(f"❌ {note}")
        return 1
    print("✅ 音画同步检查通过")
    if args.report:
        Path(args.report).write_text(
            json.dumps(
                {
                    "output": str(final),
                    "video_sec": report.video_sec,
                    "audio_sec": report.audio_sec,
                    "drift_sec": report.drift_sec,
                    "parts": [str(path) for path in parts],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="拼一条音画同步的方案对比片")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--output-name", default="lipsync_compare.mp4")
    parser.add_argument(
        "--part",
        action="append",
        required=True,
        help="说明文字|画面文件|配音文件[|lipsync]（配音写 native = 用片段自带音轨）",
    )
    parser.add_argument("--narration-voice", default="zh-CN-YunxiNeural")
    parser.add_argument(
        "--lipsync", action="store_true", help="允许对带 lipsync 标记的部件做逐帧口型"
    )
    parser.add_argument("--lipsync-python", default=str(ROOT / ".venv-lipsync" / "bin" / "python"))
    parser.add_argument("--lipsync-args", default="", help="透传给口型引擎的额外参数")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    return asyncio.run(build(args))


if __name__ == "__main__":
    raise SystemExit(main())
