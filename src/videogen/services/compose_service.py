from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from videogen.config import Settings, get_settings
from videogen.models.audio import AudioManifest
from videogen.models.compose import ComposeReport, ComposeSegment, SubtitleMode
from videogen.models.project import Project
from videogen.models.storyboard import Storyboard
from videogen.models.video import VideoManifest
from videogen.services import lipsync_service, subtitle_service, video_service
from videogen.utils import loudness as loudness_utils
from videogen.utils.audio_align import mouth_align_delay
from videogen.utils.av_sync import check_av_sync, format_sync_warning
from videogen.utils.ffmpeg import (
    ffmpeg_capabilities,
    probe_duration,
    probe_streams,
    resolve_ffmpeg,
    resolve_ffmpeg_for,
    run_ffmpeg,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None] | None

SEGMENT_TIMEOUT_SEC = 300.0
CONCAT_TIMEOUT_SEC = 300.0
FINISH_TIMEOUT_SEC = 600.0
BURN_TIMEOUT_SEC = 600.0


class ComposeError(RuntimeError):
    """合成失败（前置产物不齐、ffmpeg 报错等，消息已脱敏）。"""


def _resolution(project: Project) -> tuple[int, int]:
    """成片分辨率：取目标分辨率，并保证是偶数（H.264 要求）。"""
    width, height = project.output_resolution
    return width - width % 2, height - height % 2


def _resample(
    label_in: str,
    label_out: str,
    duration: float,
    *,
    gain_db: float = 0.0,
    delay_ms: int = 0,
) -> str:
    """统一采样格式，并按需**延后人声**（口型对齐用）。"""
    gain = f"volume={gain_db:.1f}dB," if gain_db else ""
    delay = f"adelay={delay_ms}|{delay_ms}," if delay_ms > 0 else ""
    return (
        f"[{label_in}]aresample=44100,aformat=channel_layouts=stereo,{gain}{delay}"
        f"apad,atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[{label_out}]"
    )


def _segment_filter(
    width: int,
    height: int,
    duration: float,
    fps: int,
    *,
    voice: bool,
    ambient: bool,
    ambient_gain_db: float = -20.0,
    voice_delay_ms: int = 0,
    voice_gain_db: float = 0.0,
) -> str:
    """拼一条分段滤镜：视频统一规格，音频把「配音」和「模型自带环境音」混起来。

    三种组合：
    - 只有配音：`[1:a] -> [a]`
    - 只有环境音（这个镜头没有台词）：`[0:a]` 压低后直接当音轨
    - 两者都有：环境音压到 `ambient_gain_db`，再和人声 `amix`（`normalize=0`
      保证人声音量不被拉低）
    """
    video = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps},format=yuv420p,"
        # 片段比镜头短就冻结最后一帧补齐（不重新烧视频额度）
        f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[v]"
    )
    chains = [video]
    if voice and ambient:
        chains.append(
            _resample(
                "1:a", "voice", duration, gain_db=voice_gain_db, delay_ms=voice_delay_ms
            )
        )
        chains.append(_resample("0:a", "ambient", duration, gain_db=ambient_gain_db))
        chains.append(
            "[voice][ambient]amix=inputs=2:duration=longest:dropout_transition=0:"
            "normalize=0[a]"
        )
    elif voice:
        chains.append(
            _resample("1:a", "a", duration, gain_db=voice_gain_db, delay_ms=voice_delay_ms)
        )
    elif ambient:
        chains.append(_resample("0:a", "a", duration, gain_db=ambient_gain_db))
    else:
        chains.append(f"[1:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[a]")
    return ";".join(chains)


async def render_segment(
    segment: ComposeSegment,
    *,
    video_path: Path,
    audio_path: Path | None,
    output: Path,
    width: int,
    height: int,
    fps: int,
    crf: int,
    preset: str,
    ambient: bool = False,
    ambient_gain_db: float = -20.0,
    voice_delay_ms: int = 0,
    voice_gain_db: float = 0.0,
) -> None:
    """把一个镜头渲染成「视频 + 该镜头配音」的标准片段。

    统一分辨率/帧率/采样率/声道，这样后续 concat 可以纯 stream copy。
    `voice_gain_db` 是这条配音的响度修正（逐条对齐，见 utils/loudness.py）。
    """
    duration = max(0.2, float(segment.duration_sec))
    args = ["-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path)]
    voice = audio_path is not None and Path(audio_path).is_file()
    if voice:
        args += ["-i", str(audio_path)]
    elif not ambient:
        # 既没配音也不用环境音时，也要有音轨，否则 concat 时各段流不一致
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    args += [
        "-filter_complex",
        _segment_filter(
            width,
            height,
            duration,
            fps,
            voice=voice,
            ambient=ambient,
            ambient_gain_db=ambient_gain_db,
            voice_delay_ms=voice_delay_ms,
            voice_gain_db=voice_gain_db,
        ),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-t",
        f"{duration:.3f}",
        "-movflags",
        "+faststart",
        str(output),
    ]
    await run_ffmpeg(args, timeout_sec=SEGMENT_TIMEOUT_SEC)


def _concat_list(segments: list[ComposeSegment], paths: list[Path], target: Path) -> None:
    lines = []
    for path in paths:
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _concat(concat_file: Path, output: Path, *, cwd: Path) -> None:
    await run_ffmpeg(
        [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file.name,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output.name,
        ],
        timeout_sec=CONCAT_TIMEOUT_SEC,
        cwd=cwd,
    )


async def _finish(
    source: Path,
    output: Path,
    *,
    cwd: Path,
    duration: float,
    bgm_path: Path | None,
    bgm_gain_db: float,
    fade_sec: float,
    subtitle_file: Path | None,
    mode: SubtitleMode,
    caps: set[str],
    crf: int,
    preset: str,
    burn_file: Path | None = None,
    exe: str | None = None,
    bgm_loop: bool = True,
    duck: bool = False,
    duck_ratio: float = 6.0,
    duck_threshold_db: float = -30.0,
    duck_attack_ms: float = 25.0,
    duck_release_ms: float = 420.0,
    loudness_normalize: bool = False,
    target_lufs: float = -16.0,
    true_peak_db: float = -1.5,
) -> SubtitleMode:
    """最后一道工序：混 BGM（可带人声闪避）+ 成片响度归一 + 挂字幕。

    响度归一放在**最后**：它调的是整条混音的增益，各层之间的比例不变
    （逐条配音的音量对齐在前面分段渲染时就做完了）。

    `burn_file` 是烧字幕用的 ASS（带样式）；没给时退回直接烧 `subtitle_file`（libass 默认样式）。

    `exe` 只用于**烧字幕**那一趟：主 ffmpeg 没有 libass 时，调用方会传一个
    带 libass 的候选（`resolve_ffmpeg_for({"subtitles"})`）；软字幕/无字幕
    仍然走主 ffmpeg。
    """
    effective = mode
    if mode == SubtitleMode.SOFT and "mov_text" not in caps:
        effective = SubtitleMode.NONE
    if mode == SubtitleMode.BURN and ("subtitles" not in caps or subtitle_file is None):
        effective = SubtitleMode.SOFT if "mov_text" in caps else SubtitleMode.NONE
    if effective == SubtitleMode.SOFT and "mov_text" not in caps:
        effective = SubtitleMode.NONE

    burn = effective == SubtitleMode.BURN and subtitle_file is not None
    soft = effective == SubtitleMode.SOFT and subtitle_file is not None
    mix = (
        bgm_mix_filter(
            bgm_label="1:a",
            voice_label="0:a",
            gain_db=bgm_gain_db,
            fade_sec=fade_sec,
            duration=duration,
            duck=duck,
            duck_ratio=duck_ratio,
            duck_threshold_db=duck_threshold_db,
            duck_attack_ms=duck_attack_ms,
            duck_release_ms=duck_release_ms,
        )
        if bgm_path
        else ""
    )
    # 成片响度归一：把「这条片子整体多响」也定死，交付到平台不会时大时小
    normalize = (
        _final_loudness_filter(target_lufs, true_peak_db) if loudness_normalize else ""
    )
    audio_filter = ""
    if bgm_path and normalize:
        audio_filter = f"{mix};[a]{normalize}[aout]"
    elif bgm_path:
        audio_filter = mix
    elif normalize:
        audio_filter = f"[0:a]{normalize}[aout]"
    audio_label = "[aout]" if normalize else "[a]"

    if burn:
        # 烧字幕必须重编码视频；顺带把 BGM 混进去，少走一遍
        args = ["-y", "-hide_banner", "-loglevel", "error", "-i", source.name]
        burn_filter = _burn_filter((burn_file or subtitle_file).name)
        if audio_filter:
            args += _bgm_input(bgm_path, loop=bgm_loop) if bgm_path else []
            args += [
                "-filter_complex",
                f"[0:v]{burn_filter}[v];{audio_filter}",
                "-map",
                "[v]",
                "-map",
                audio_label,
            ]
        else:
            args += ["-vf", burn_filter, "-map", "0:v", "-map", "0:a?"]
        args += [
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
            output.name,
        ]
        await run_ffmpeg(args, timeout_sec=BURN_TIMEOUT_SEC, cwd=cwd, exe=exe)
        return effective

    # 软字幕 / 无字幕：视频流可以 copy，只有需要混音时才对音频重编码
    # source 已经是拼接好的 mp4，直接当普通输入读。
    # 注意：所有 -i 必须写在任何输出选项（-map/-c...）之前，否则 ffmpeg 会把
    # -map 当成下一个输入文件的输入选项。
    args = ["-y", "-hide_banner", "-loglevel", "error", "-i", source.name]
    if bgm_path:
        args += _bgm_input(bgm_path, loop=bgm_loop)
    if soft:
        args += ["-i", subtitle_file.name]

    if audio_filter:
        args += [
            "-filter_complex",
            audio_filter,
            "-map",
            "0:v",
            "-map",
            audio_label,
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
        ]
    else:
        args += ["-map", "0:v", "-map", "0:a", "-c", "copy"]

    if soft:
        args += [
            "-map",
            str(_subtitle_input_index(bgm_path)),
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=zho",
        ]

    args += ["-t", f"{duration:.3f}", "-movflags", "+faststart", output.name]
    await run_ffmpeg(args, timeout_sec=FINISH_TIMEOUT_SEC, cwd=cwd)
    return effective


#: atempo 每一级的合法区间。老版 ffmpeg 只认 0.5~2.0，所以任意倍率都拆成多级串联
_ATEMPO_MIN = 0.5
_ATEMPO_MAX = 2.0


def atempo_chain(speed: float) -> str:
    """把任意倍速拆成若干级合法 `atempo`（每级都落在 0.5~2.0）。

    直接写 `atempo=4` 在老版 ffmpeg 上会报错，而 `atempo=2,atempo=2` 到处都能跑。
    """
    value = float(speed)
    if value <= 0:
        raise ComposeError(f"变速倍率必须大于 0（收到 {speed!r}）")
    factors: list[float] = []
    remaining = value
    while remaining > _ATEMPO_MAX:
        factors.append(_ATEMPO_MAX)
        remaining /= _ATEMPO_MAX
    while remaining < _ATEMPO_MIN:
        factors.append(_ATEMPO_MIN)
        remaining /= _ATEMPO_MIN
    factors.append(remaining)
    return ",".join(f"atempo={factor:g}" for factor in factors)


def speed_filter(speed: float) -> str:
    """整片变速的滤镜图。

    - 视频：`setpts=PTS/speed` —— 只改时间戳，不动帧内容
    - 音频：`atempo` —— **变速不变调**，用改采样率那套会把配音变成花栗鼠
    """
    return f"[0:v]setpts=PTS/{float(speed):g}[v];[0:a]{atempo_chain(speed)}[a]"


async def apply_speed(
    source: Path,
    output: Path,
    *,
    speed: float,
    duration: float,
    fps: int,
    crf: int,
    preset: str,
    cwd: Path,
    exe: str | None = None,
) -> None:
    """把成片整体变速（`speed == 1` 时调用方不该调它）。

    配音、口型、BGM、**烧进画面的字幕**都已经在 `source` 里了，一起被缩放，
    所以变速之后它们之间仍然同步 —— 这也是为什么变速要放在最后一道工序。

    **必须显式给 `-r`**：`setpts` 只改时间戳、不改帧数，不指定输出帧率时 ffmpeg
    会按目标帧率复制/丢帧回到 CFR，把加速效果吃掉或产生重影。

    **必须给 `-t duration`**：`atempo` 的输出并不精确等于 `源时长 / speed`，
    AAC 编码还会再补一段 padding（67s 成片 2 倍速实测：视频 33.583s / 音频 33.753s，
    差 0.17s —— 刚好越过 0.20s 的音画同步阈值被报出来）。掐到精确时长后两边都是
    33.500s，漂移归零。
    """
    args = [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source.name,
        "-filter_complex",
        speed_filter(speed),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-r",
        str(max(1, int(fps))),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-t",
        f"{max(0.01, float(duration)):.3f}",
        "-movflags",
        "+faststart",
        output.name,
    ]
    await run_ffmpeg(args, timeout_sec=BURN_TIMEOUT_SEC, cwd=cwd, exe=exe)


def _final_loudness_filter(target_lufs: float, true_peak_db: float) -> str:
    """成片整体响度归一：loudnorm 到目标 LUFS + 真峰值限幅。

    单遍 `loudnorm` 是**动态**的（慢速 AGC），对 60 秒以上的成片是合适的；
    LRA 给 11 是为了别把有意的强弱起伏压成一条直线。
    后面再跟一个 `alimiter` 兜底：loudnorm 单遍的真峰值控制只是近似。
    """
    limit = min(1.0, max(0.1, 10 ** (true_peak_db / 20.0)))
    return (
        f"loudnorm=I={target_lufs:g}:TP={true_peak_db:g}:LRA=11,"
        f"alimiter=limit={limit:.4f}:level=disabled"
    )


def _bgm_input(bgm_path: Path, *, loop: bool) -> list[str]:
    """BGM 的输入参数：曲库曲目循环铺满，生成的曲子本身就是整片长度（不循环）。"""
    return (["-stream_loop", "-1"] if loop else []) + ["-i", str(bgm_path)]


def _subtitle_input_index(bgm_path: Path | None) -> int:
    return 2 if bgm_path else 1


def bgm_mix_filter(
    *,
    bgm_label: str = "1:a",
    voice_label: str = "0:a",
    gain_db: float = -20.0,
    fade_sec: float = 1.5,
    duration: float = 0.0,
    duck: bool = False,
    duck_ratio: float = 6.0,
    duck_threshold_db: float = -30.0,
    duck_attack_ms: float = 25.0,
    duck_release_ms: float = 420.0,
) -> str:
    """BGM 混音滤镜链（公开给测试与诊断用）。

    两种形态：
    - 不闪避：直接 `amix`（音乐固定音量压在人声下面）
    - 闪避：`asplit` 出人声当 sidechain，说话时把音乐压下去（`sidechaincompress`），
      说完 0.4 秒左右抬回来。比固定音量好得多 —— 固定音量要么平时太小、要么说话时糊。
    """
    fade = max(0.0, min(fade_sec, duration / 3 if duration else fade_sec))
    fade_out_start = max(0.0, duration - fade)
    chain = (
        f"[{bgm_label}]volume={gain_db:.1f}dB,"
        f"afade=t=in:st=0:d={fade:.2f},"
        f"afade=t=out:st={fade_out_start:.2f}:d={fade:.2f},"
        f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[music]"
    )
    if not duck:
        return (
            f"{chain};[{voice_label}][music]amix=inputs=2:duration=first:"
            "dropout_transition=0:normalize=0[a]"
        )
    # sidechaincompress 的 threshold 是线性幅度（0.00097563..1），这里从 dBFS 换算
    threshold = min(1.0, max(0.001, 10 ** (duck_threshold_db / 20.0)))
    return (
        f"{chain};[{voice_label}]asplit=2[voice][side];"
        f"[music][side]sidechaincompress=threshold={threshold:.5f}:"
        f"ratio={duck_ratio:g}:attack={duck_attack_ms:g}:release={duck_release_ms:g}:"
        "makeup=1[ducked];"
        "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )



def _burn_filter(subtitle_name: str) -> str:
    """烧字幕滤镜。样式全在 ASS 文件里（`subtitle_service.build_ass`），这里不再 force_style。"""
    # cwd 就在产物目录，这里只写文件名，避开 subtitles 滤镜的路径转义地狱
    return f"subtitles={subtitle_name}"


async def compose(
    project: Project,
    storyboard: Storyboard,
    video_manifest: VideoManifest | None,
    audio_manifest: AudioManifest | None,
    *,
    segments_dir: Path,
    output_dir: Path,
    project_dir: Path,
    bible=None,
    settings: Settings | None = None,
    on_progress: ProgressCallback = None,
    reuse_segments: bool = True,
) -> ComposeReport:
    """把「分镜 + 视频片段 + 配音」合成一条成片。

    前置校验故意严格：视频里有占位文件、配音缺失或失败，都直接拒绝合成 ——
    宁可报错也不要产出一条看起来成功、实际缺料的片子。
    """
    settings = settings or get_settings()
    warnings: list[str] = []

    if video_manifest is None or not video_manifest.clips:
        raise ComposeError("视频清单不存在，请先生成视频片段")
    if not video_manifest.is_usable:
        raise ComposeError(
            f"视频片段不完整（就绪 {len(video_manifest.ready_clips)}/"
            f"{len(video_manifest.clips)}，失败 {len(video_manifest.failed_clips)}，"
            f"占位 {len(video_manifest.placeholder_clips)}），请先补齐再合成"
        )
    # 原声模式：人声全用视频原片，不读配音清单（切换模式前留下的 TTS 条目也不用）
    dub_mode = video_service.dub_mode_for(project, settings)
    voice_audio = audio_manifest if dub_mode == "tts" else None
    if voice_audio is not None and not voice_audio.is_usable:
        raise ComposeError(
            f"配音不完整（就绪 {len(voice_audio.ready_clips)}/"
            f"{len(voice_audio.clips)}，失败 {len(voice_audio.failed_clips)}），"
            "请先补齐配音再合成"
        )
    if not storyboard.shots:
        raise ComposeError("分镜为空，无法合成")
    native = video_service.native_voice_shots(storyboard, video_manifest, dub_mode=dub_mode)
    if (voice_audio is None or not voice_audio.clips) and not native:
        warnings.append("没有配音，成片为纯画面（无声）")

    segments = subtitle_service.build_segments(
        storyboard, voice_audio, speaker_names=subtitle_service.speaker_name_map(bible)
    )
    segments = [s for s in segments if _bind_video(s, video_manifest, project_dir, warnings)]
    if settings.compose_follow_clip_length:
        await _follow_clip_lengths(segments, video_manifest, voice_audio, project_dir)
    for segment in segments:
        segment.native_voice = segment.shot_id in native and not segment.audio_path
    native_count = sum(1 for s in segments if s.native_voice)
    if dub_mode == "native":
        silent: list[str] = []
        for segment in segments:
            if not segment.native_voice:
                continue
            streams = await probe_streams(project_dir / segment.video_path)
            if streams is not None and "audio" not in streams:  # None = 探测不了，不乱报
                silent.append(segment.shot_id)
        if silent:
            warnings.append(
                f"原声模式：{len(silent)} 个镜头的视频没有音轨（{', '.join(silent[:5])}"
                f"{' …' if len(silent) > 5 else ''}），这些镜头是无声的 —— Seedance / Kling / Wan "
                "要用 VIDEO_GENERATE_AUDIO=true 生成，或改用 AUDIO_DUB_MODE=tts"
            )
    if native_count and dub_mode == "native":
        warnings.append(f"原声模式：{native_count} 个镜头的人声都用视频原声（不做 TTS 配音）")
    elif native_count:
        warnings.append(f"原声旁白：{native_count} 个镜头的旁白用视频原声（按原音量，不另配音）")
    if not segments:
        raise ComposeError("没有任何可用的视频片段")
    total_duration = round(sum(s.duration_sec for s in segments), 3)

    width, height = _resolution(project)
    fps = max(1, settings.video_fps)
    caps = await ffmpeg_capabilities()
    desired_mode = settings.subtitle_mode
    speed = float(settings.compose_speed or 1.0)
    speed_changing = abs(speed - 1.0) > 1e-9
    if speed_changing and desired_mode == SubtitleMode.SOFT:
        # 内嵌软字幕（mov_text）只有一条整体时间戳，**不参与变速缩放** ——
        # 与其交一条字幕整体错位的片子，不如改烧进画面（帧被缩放，字自然跟着走）
        warnings.append(
            f"成片 {speed:g} 倍速：内嵌软字幕无法随变速缩放，已自动改为烧字幕"
        )
        desired_mode = SubtitleMode.BURN
    burn_exe: str | None = None
    if desired_mode == SubtitleMode.BURN:
        # 主 ffmpeg 常常没有 libass（brew 的 9.x 就没有），别急着降级：
        # 先在候选里找一个带 subtitles 滤镜的（imageio-ffmpeg 便携版带 libass）
        candidate_exe, candidate_caps = await resolve_ffmpeg_for({"subtitles"})
        if "subtitles" in candidate_caps:
            burn_exe = candidate_exe
            caps = candidate_caps  # 用「真正要执行的那个 exe」的能力判降级
            if candidate_exe and candidate_exe != resolve_ffmpeg():
                warnings.append(f"主 ffmpeg 没有 libass，烧字幕改用：{candidate_exe}")
        else:
            warnings.append("当前 ffmpeg 没有 libass，无法烧字幕，已降级为内嵌软字幕")
    if desired_mode != SubtitleMode.NONE and "mov_text" not in caps:
        warnings.append("当前 ffmpeg 不支持 mov_text，字幕只导出 .srt 侧挂文件")

    ambient_mode = settings.ambient_mode
    ambient_effective = await _resolve_ambient_mode(
        segments, project_dir, ambient_mode, warnings
    )

    segments_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _notify(on_progress, 0, len(segments) + 2, "开始渲染分段")

    # 逐帧口型（可选）：先把画面里的嘴按配音重画一遍，再走常规分段渲染。
    # 做过口型的镜头**不再**做「把配音整体延后」——嘴已经跟着配音走了，再延后反而错开。
    synced: dict[str, Path] = {}
    lipsync_outcomes: list[lipsync_service.LipsyncOutcome] = []
    if settings.lipsync_enabled and dub_mode == "tts":
        synced, lipsync_outcomes = await _lipsync_all(
            segments,
            storyboard,
            output_dir / "lipsynced",
            project_dir,
            settings,
            on_progress=on_progress,
            reuse=reuse_segments,
        )
        ok_count = sum(1 for item in lipsync_outcomes if item.ok)
        if ok_count:
            warnings.append(
                f"逐帧口型：{ok_count}/{len(lipsync_outcomes)} 个镜头已按配音重画嘴部"
                f"（引擎 {settings.lipsync}，本地计算，不额外花钱）"
            )
        for item in lipsync_outcomes:
            if not item.ok and item.note:
                warnings.append(f"逐帧口型跳过 {item.shot_id}：{item.note}")

    # 整段直通：一段里全是原声、不需要逐镜处理声音 → 直接拿整段原片渲一次
    groups = (
        _passthrough_groups(segments, video_manifest, project_dir, synced)
        if settings.compose_segment_passthrough
        else {}
    )
    grouped = {s.shot_id for members in groups.values() for s in members}
    if groups:
        await _render_passthrough(
            groups,
            video_manifest,
            segments_dir,
            project_dir,
            width=width,
            height=height,
            fps=fps,
            crf=settings.compose_crf,
            preset=settings.compose_preset,
            reuse=reuse_segments,
        )
        warnings.append(
            f"整段直通：{len(groups)} 段（{len(grouped)} 个镜头）直接用视频模型原片，"
            "不切镜重编码"
        )

    voice_gains: dict[str, float] = {}
    mouth_delays, voice_gains = await _render_all(
        [s for s in segments if s.shot_id not in grouped],
        segments_dir,
        project_dir,
        width=width,
        height=height,
        fps=fps,
        crf=settings.compose_crf,
        preset=settings.compose_preset,
        concurrency=max(1, settings.compose_concurrency),
        reuse=reuse_segments,
        on_progress=on_progress,
        ambient=ambient_effective == "under",
        ambient_gain_db=settings.compose_ambient_gain_db,
        align_mouth=settings.compose_align_dub_to_mouth,
        mouth_align_max_sec=settings.compose_mouth_align_max_sec,
        synced=synced,
        normalize_voice=settings.audio_normalize,
        target_lufs=settings.audio_target,
        max_gain_db=settings.audio_max_gain_db,
    )
    if voice_gains:
        notes = sorted(voice_gains.values())
        warnings.append(
            f"逐条配音响度归一：{len(voice_gains)} 条，增益 {notes[0]:+.1f}~{notes[-1]:+.1f}dB"
            f"（目标 {settings.audio_target:g} LUFS）"
        )
    if lipsync_outcomes:
        _write_lipsync_report(output_dir, lipsync_outcomes)
    if mouth_delays:
        logger.info(
            "口型对齐：%d 个镜头的配音已延后到画面开口时刻（最大 %.2fs）",
            len(mouth_delays),
            max(mouth_delays.values()),
        )
        warnings.append(
            f"口型对齐：{len(mouth_delays)} 个镜头的配音延后了 "
            f"{min(mouth_delays.values()):.2f}~{max(mouth_delays.values()):.2f}s"
        )

    segment_paths: list[Path] = []
    for segment in segments:
        if segment.shot_id not in grouped:
            segment_paths.append(segments_dir / f"{segment.shot_id}.mp4")
            continue
        segment_id = video_manifest.get(segment.shot_id).segment_id
        if groups[segment_id][0] is segment:
            segment_paths.append(segments_dir / f"{segment_id}.mp4")
    _notify(on_progress, len(segments), len(segments) + 2, "拼接分段")

    base = output_dir / "final_base.mp4"
    concat_file = output_dir / "concat.txt"
    _concat_list(segments, segment_paths, concat_file)
    await _concat(concat_file, base, cwd=output_dir)

    # 无论什么模式都导出 .srt 侧挂文件：平台上传/二次编辑都用得上
    # 同时导出一份 .vtt：浏览器的 <track> 只认 VTT，认不出 SRT / mov_text
    subtitles_path: Path | None = None
    subtitles_vtt_path: Path | None = None
    srt = subtitle_service.build_srt(segments, voice_audio)
    if srt.strip():
        subtitles_path = output_dir / "subtitles.srt"
        subtitles_path.write_text(srt, encoding="utf-8")
        subtitles_vtt_path = output_dir / "subtitles.vtt"
        subtitles_vtt_path.write_text(
            subtitle_service.build_vtt(segments, voice_audio), encoding="utf-8"
        )
    else:
        subtitles_path = None
        warnings.append("没有台词内容，未生成字幕")

    # 烧字幕用 ASS：样式（字体 / 柔化 / 阴影 / 底条）写在文件里，像素单位 = 成片分辨率
    burn_path: Path | None = None
    look_name, look = subtitle_service.subtitle_look(settings.compose_subtitle_style)
    if subtitles_path is not None and desired_mode == SubtitleMode.BURN:
        burn_path = output_dir / "subtitles.ass"
        burn_path.write_text(
            subtitle_service.build_ass(
                segments,
                voice_audio,
                resolution=(width, height),
                look=look,
                font=settings.compose_subtitle_font,
                font_scale=settings.compose_subtitle_font_scale,
                margin_scale=settings.compose_subtitle_margin_scale,
            ),
            encoding="utf-8",
        )

    bgm = audio_manifest.bgm if audio_manifest else None
    bgm_path: Path | None = None
    if bgm and bgm.path:
        candidate = Path(bgm.path)
        if candidate.is_file():
            bgm_path = candidate
        else:
            warnings.append(f"BGM 文件不存在，已跳过配乐：{bgm.id}")

    # 人声闪避：说话时把音乐压下去。ffmpeg 没有 sidechaincompress 就退回固定音量
    duck = settings.bgm_duck if (bgm is None or bgm.duck is None) else bool(bgm.duck)
    if bgm_path and duck and "sidechaincompress" not in caps:
        warnings.append("当前 ffmpeg 没有 sidechaincompress，本次配乐不做人声闪避（固定音量）")
        duck = False
    bgm_fade = settings.bgm_fade_sec
    if bgm is not None and bgm.fade_sec is not None:
        bgm_fade = float(bgm.fade_sec)
    bgm_loop = True if bgm is None else bool(bgm.loop)

    final = output_dir / "final.mp4"
    _notify(on_progress, len(segments) + 1, len(segments) + 2, "混音 / 挂字幕")
    effective_mode = await _finish(
        base,
        final,
        cwd=output_dir,
        duration=total_duration,
        bgm_path=bgm_path,
        bgm_gain_db=bgm.gain_db if bgm and bgm.gain_db is not None else settings.bgm_gain_db,
        fade_sec=bgm_fade,
        subtitle_file=subtitles_path,
        # 变速会把 soft 升级成 burn：传「真正要用的模式」，不是配置里的原值
        mode=SubtitleMode(desired_mode),
        caps=caps,
        exe=burn_exe,
        crf=settings.compose_crf,
        preset=settings.compose_preset,
        burn_file=burn_path,
        bgm_loop=bgm_loop,
        duck=duck,
        duck_ratio=settings.bgm_duck_ratio,
        duck_threshold_db=settings.bgm_duck_threshold_db,
        duck_attack_ms=settings.bgm_duck_attack_ms,
        duck_release_ms=settings.bgm_duck_release_ms,
        loudness_normalize=settings.compose_loudness_normalize,
        target_lufs=settings.target_lufs,
        true_peak_db=settings.true_peak_db,
    )
    if bgm_path and bgm is not None:
        engine_label = {
            "generate": "本机现写",
            "cli": "外部音乐模型",
            "library": "本地曲库",
        }.get(bgm.engine, bgm.engine or "曲库")
        warnings.append(
            f"配乐（{engine_label}）：{bgm.title or bgm.id}；音量 "
            f"{bgm.gain_db if bgm.gain_db is not None else settings.bgm_gain_db:.1f}dB，"
            f"{'人声闪避开' if duck else '固定音量'}"
            f"{'，整片长度不循环' if not bgm_loop else ''}"
        )

    if not final.exists() or final.stat().st_size == 0:
        raise ComposeError("ffmpeg 未产出有效成片文件")

    # ── 整片变速（最后一道工序）─────────────────────────────
    # 放在响度归一与烧字幕**之后**：配音、逐帧口型、BGM、烧进画面的字幕都已在文件里，
    # 一起被缩放 → 它们之间的同步关系不变。换到前面任何一步都要各自补偿，必错。
    delivered_duration = total_duration
    if speed_changing:
        delivered_duration = round(total_duration / speed, 3)
        sped = output_dir / "final_speed.mp4"
        sped.unlink(missing_ok=True)
        await apply_speed(
            final,
            sped,
            speed=speed,
            duration=delivered_duration,
            fps=fps,
            crf=settings.compose_crf,
            preset=settings.compose_preset,
            cwd=output_dir,
            exe=burn_exe,
        )
        if not sped.exists() or sped.stat().st_size == 0:
            raise ComposeError(f"变速输出失败（{speed:g} 倍速）")
        sped.replace(final)  # 同一目录内替换，成片路径不变
        warnings.append(
            f"成片 {speed:g} 倍速：{total_duration:g}s → {delivered_duration:g}s"
            f"（每镜观感 {1 / speed:.2f} 倍时长；音频用 atempo，变速不变调）"
        )
        if effective_mode == SubtitleMode.SOFT:
            # 走到这里说明连烧字幕的 ffmpeg 都没找到：软轨已随变速被丢弃
            warnings.append("变速后内嵌软字幕轨已丢弃，请用侧挂 .srt/.vtt（时间轴已按倍速重算）")
            effective_mode = SubtitleMode.NONE
        # 侧挂字幕必须跟着变速重算：用户拿它上传平台或用 <track> 预览
        if subtitles_path is not None:
            subtitles_path.write_text(
                subtitle_service.build_srt(segments, voice_audio, speed=speed),
                encoding="utf-8",
            )
            if subtitles_vtt_path is not None:
                subtitles_vtt_path.write_text(
                    subtitle_service.build_vtt(segments, voice_audio, speed=speed),
                    encoding="utf-8",
                )

    # 成片自检：音轨时长必须和视频基本相等。拼接丢掉音频空洞时（mp4 音轨表达不了
    # 空洞 → 后面所有声音整体提前）这个差值会等于空洞之和，一眼可见。
    sync = await check_av_sync(final)
    # 顺带量一遍成片响度：响度归一如果没生效（或把片子压小了），这里必须看得见
    final_loudness = await loudness_utils.measure(final)
    if settings.compose_loudness_normalize and final_loudness.ok:
        delta = (final_loudness.integrated or 0.0) - settings.target_lufs
        if abs(delta) > 1.5:
            note = (
                f"成片响度 {final_loudness.integrated:.1f} LUFS 与目标 "
                f"{settings.target_lufs:g} LUFS 差 {delta:+.1f}dB"
                "（loudnorm 单遍在极短或动态很大的片子上会有偏差）"
            )
            logger.warning("%s", note)
            warnings.append(note)
    sync_note = format_sync_warning(sync, "成片")
    if sync_note:
        logger.warning("%s", sync_note)
        warnings.append(sync_note)

    _cleanup(base, concat_file, segments_dir if not settings.compose_keep_segments else None)

    report = ComposeReport(
        output_path=_relative(final, project_dir),
        subtitles_path=_relative(subtitles_path, project_dir) if subtitles_path else None,
        subtitles_vtt_path=(
            _relative(subtitles_vtt_path, project_dir) if subtitles_vtt_path else None
        ),
        duration_sec=delivered_duration,
        source_duration_sec=total_duration,
        speed=speed,
        subtitle_mode=SubtitleMode(settings.subtitle_mode),
        subtitle_style=look_name if effective_mode == SubtitleMode.BURN else "",
        subtitle_mode_effective=effective_mode,
        segments=segments,
        bgm_id=bgm.id if bgm else "",
        bgm_path=str(bgm.path) if bgm else "",
        bgm_gain_db=bgm.gain_db if bgm and bgm.gain_db is not None else settings.bgm_gain_db,
        bgm_engine=bgm.engine if bgm else "",
        bgm_plan_path=bgm.plan_path if bgm else "",
        bgm_duck=duck if bgm_path else False,
        ambient_mode=ambient_mode,
        ambient_mode_effective=ambient_effective,
        ambient_gain_db=settings.compose_ambient_gain_db,
        audio_normalize=settings.audio_normalize,
        audio_target_lufs=settings.audio_target,
        voice_gains=voice_gains,
        loudness_normalize=settings.compose_loudness_normalize,
        target_lufs=settings.target_lufs,
        final_lufs=final_loudness.integrated,
        final_true_peak_db=final_loudness.true_peak,
        video_provider=video_manifest.provider,
        tts_provider=voice_audio.provider if voice_audio else "",
        ffmpeg=resolve_ffmpeg() or "",
        subtitle_ffmpeg=burn_exe if effective_mode == SubtitleMode.BURN else "",
        warnings=warnings,
    )
    _notify(on_progress, len(segments) + 2, len(segments) + 2, "合成完成")
    logger.info(
        "合成完成 project=%s 时长=%.1fs 分段=%d 字幕=%s BGM=%s",
        project.id,
        total_duration,
        len(segments),
        effective_mode.value,
        bgm.id if bgm else "无",
    )
    return report


async def _resolve_ambient_mode(
    segments: list[ComposeSegment],
    project_dir: Path,
    requested: str,
    warnings: list[str],
) -> str:
    """确认环境音到底能不能用：需要 ffprobe 探到片段里真的有音轨。"""
    if requested != "under":
        return "off"

    with_audio = 0
    unknown = False
    for segment in segments:
        if not segment.video_path:
            continue
        streams = await probe_streams(project_dir / segment.video_path)
        if streams is None:
            unknown = True
            break
        if "audio" in streams:
            with_audio += 1

    if unknown:
        warnings.append(
            "没有可用的 ffprobe，无法确认片段是否自带音轨，本次环境音不混"
            "（装 ffmpeg 或设 FFPROBE_PATH 后可用）"
        )
        return "off"
    if not with_audio:
        warnings.append("所选片段都没有自带音轨，本次环境音不混")
        return "off"
    logger.info("环境音混音已开启：%d/%d 个片段自带音轨", with_audio, len(segments))
    return "under"


async def _render_all(
    segments: list[ComposeSegment],
    segments_dir: Path,
    project_dir: Path,
    *,
    width: int,
    height: int,
    fps: int,
    crf: int,
    preset: str,
    concurrency: int,
    reuse: bool,
    on_progress: ProgressCallback,
    ambient: bool = False,
    ambient_gain_db: float = -20.0,
    align_mouth: bool = False,
    mouth_align_max_sec: float = 1.5,
    synced: dict[str, Path] | None = None,
    normalize_voice: bool = False,
    target_lufs: float = -20.0,
    max_gain_db: float = 6.0,
) -> tuple[dict[str, float], dict[str, float]]:
    """渲染所有分段，返回（口型延后记录, 逐条配音的响度增益）。"""
    synced = synced or {}
    semaphore = asyncio.Semaphore(concurrency)
    done = 0
    lock = asyncio.Lock()
    probe_cache: dict[str, bool] = {}
    delays: dict[str, float] = {}
    gains: dict[str, float] = {}
    lufs_cache: dict[str, float | None] = {}

    async def has_ambient(video_path: Path) -> bool:
        key = str(video_path)
        if key not in probe_cache:
            streams = await probe_streams(video_path)
            probe_cache[key] = bool(streams and "audio" in streams)
        return probe_cache[key]

    async def voice_gain(segment: ComposeSegment) -> float:
        """这条配音该补多少 dB（逐条响度对齐 + 念法里的音量意图）。

        实测动机：同一部片子里不同音色的配音可以差 7.9dB，观众听感就是
        「这句突然变小了」。这里只量一个数、算一个**有上限的**定值增益 ——
        不用动态 loudnorm（2 秒的台词跑动态归一会上下来回飘）。
        """
        if not normalize_voice or not segment.audio_path:
            return 0.0
        key = segment.audio_path
        if key not in lufs_cache:
            info = await loudness_utils.measure(project_dir / key)
            lufs_cache[key] = info.integrated
            if info.error:
                logger.warning("配音响度测不出来（%s）：%s", key, info.error)
        gain = loudness_utils.gain_for(
            lufs_cache[key],
            target=target_lufs,
            max_db=max_gain_db,
            intent_db=loudness_utils.volume_to_db(segment.volume),
        )
        return gain

    async def run_one(segment: ComposeSegment) -> None:
        nonlocal done
        async with semaphore:
            output = segments_dir / f"{segment.shot_id}.mp4"
            video_path = project_dir / segment.video_path
            # 做过逐帧口型的镜头用重画后的画面（音轨已接回，环境音链路照旧）
            lipsynced = synced.get(segment.shot_id)
            if lipsynced is not None and lipsynced.is_file():
                video_path = lipsynced
            audio_path = project_dir / segment.audio_path if segment.audio_path else None
            gain_db = await voice_gain(segment)
            use_ambient = ambient and await has_ambient(video_path)
            # 原声里就是这一镜的旁白：原音量进成片（不是环境音那样压到 -20dB）
            native = segment.native_voice and await has_ambient(video_path)
            segment_ambient_db = ambient_gain_db
            if native:
                use_ambient, segment_ambient_db = True, 0.0
            # 口型对齐：画面里的人几秒开口，配音就延后几秒（只用本地计算，不重新烧视频）。
            # 逐帧口型已经让嘴跟着配音走了，再叠加延后会把嘴和声音错开。
            delay_ms = 0
            if lipsynced is None and segment.voice_offset_sec > 0 and audio_path is not None:
                # 原声引导算好的位置：直接用，不再做起点探测
                delay_ms = round(segment.voice_offset_sec * 1000)
                async with lock:
                    delays[segment.shot_id] = segment.voice_offset_sec
            elif (
                lipsynced is None
                and align_mouth
                and not segment.voice_offscreen
                and audio_path is not None
                and await has_ambient(video_path)
            ):
                voice_sec = await probe_duration(audio_path) or 0.0
                delay = await mouth_align_delay(
                    video_path,
                    segments_dir,
                    voice_sec=voice_sec,
                    segment_sec=segment.duration_sec,
                    max_delay_sec=mouth_align_max_sec,
                )
                if delay > 0:
                    delay_ms = round(delay * 1000)
                    async with lock:
                        delays[segment.shot_id] = delay
            # 渲染参数变了必须重渲（环境音/口型对齐/逐帧口型/响度修正都会改变音轨内容）。
            # 这里把参数**写进一个固定名字的标记文件**再比对内容，而不是塞进文件名：
            # 文件名方案会被上一轮的旧标记骗过 —— 旧标记还在、分段文件却已经被
            # 「另一套参数」重渲过（真实事故：关掉响度归一的对比跑复用到了归一后的分段）。
            marker = segments_dir / f"{segment.shot_id}.params"
            params = (
                f"ambient={'on' if use_ambient else 'off'}"
                f" mouth={delay_ms if delay_ms > 0 else 'off'}"
                f" lipsync={'on' if lipsynced is not None else 'off'}"
                f" gain={gain_db:+.1f}"
                f" native={'on' if native else 'off'}"
                f" dur={segment.duration_sec:.3f}"
            )
            if (
                reuse
                and _params_match(marker, params)
                and _segment_is_fresh(output, [video_path, audio_path])
            ):
                logger.debug("复用已渲染分段：%s", output.name)
            else:
                await render_segment(
                    segment,
                    video_path=video_path,
                    audio_path=audio_path,
                    output=output,
                    width=width,
                    height=height,
                    fps=fps,
                    crf=crf,
                    preset=preset,
                    ambient=use_ambient,
                    ambient_gain_db=segment_ambient_db,
                    voice_delay_ms=delay_ms,
                    voice_gain_db=gain_db,
                )
                marker.write_text(params, encoding="utf-8")
            async with lock:
                segment.gain_db = gain_db
                if gain_db:
                    gains[segment.shot_id] = gain_db
                done += 1
                _notify(
                    on_progress,
                    done,
                    len(segments) + 2,
                    f"分段渲染 {done}/{len(segments)}",
                )

    await asyncio.gather(*(run_one(segment) for segment in segments))
    return delays, gains


async def _lipsync_all(
    segments: list[ComposeSegment],
    storyboard: Storyboard,
    lipsync_dir: Path,
    project_dir: Path,
    settings: Settings,
    *,
    on_progress: ProgressCallback = None,
    reuse: bool = True,
) -> tuple[dict[str, Path], list[lipsync_service.LipsyncOutcome]]:
    """给需要口型的镜头逐个做逐帧口型，返回 shot_id -> 新画面路径 与逐镜结果。

    产物落在 `output/lipsynced/`（**不进** compose 的临时目录清理），这样重跑合成
    可以直接复用、也方便人工逐个核对「嘴是不是真的改了」。
    """
    lipsync_dir.mkdir(parents=True, exist_ok=True)
    shots = {shot.id: shot for shot in storyboard.shots}
    problem = lipsync_service.engine_problem(settings)
    if problem:
        logger.warning("逐帧口型不可用：%s", problem)
        return {}, [lipsync_service.LipsyncOutcome(shot_id="—", note=problem)]

    scope = settings.lipsync_scope_mode
    targets: list[ComposeSegment] = []
    for segment in segments:
        if not segment.audio_path:
            continue
        if not lipsync_service.shot_eligible(shots.get(segment.shot_id), scope=scope):
            continue
        if segment.voice_offset_sec > 0:
            # 原声引导：配音已经按画面里那张嘴的时间放好了（嘴本来就在念这句），不用重画
            continue
        if settings.lipsync_max_sec > 0 and segment.duration_sec > settings.lipsync_max_sec:
            continue
        targets.append(segment)
    if not targets:
        return {}, []

    synced: dict[str, Path] = {}
    outcomes: list[lipsync_service.LipsyncOutcome] = []
    for index, segment in enumerate(targets, start=1):
        _notify(
            on_progress, index - 1, len(segments) + 2, f"逐帧口型 {index}/{len(targets)}"
        )
        video_path = project_dir / segment.video_path
        audio_path = project_dir / segment.audio_path
        output = lipsync_dir / f"{segment.shot_id}.synced.mp4"
        if reuse and output.is_file() and output.stat().st_size > 0 and _is_newer(
            output, [video_path, audio_path]
        ):
            synced[segment.shot_id] = output
            outcomes.append(
                lipsync_service.LipsyncOutcome(
                    shot_id=segment.shot_id,
                    ok=True,
                    engine=settings.lipsync,
                    output=output,
                    note="复用已有的口型结果",
                )
            )
            continue
        outcome = await lipsync_service.sync_shot(
            video_path,
            audio_path,
            output,
            shot_id=segment.shot_id,
            settings=settings,
        )
        outcomes.append(outcome)
        if outcome.ok and outcome.output is not None:
            synced[segment.shot_id] = outcome.output
    return synced, outcomes


def _is_newer(output: Path, inputs: list[Path]) -> bool:
    """输出不比任何输入旧（含相等，避免同一秒内的产物被判成过期）。"""
    try:
        stamp = output.stat().st_mtime
    except OSError:
        return False
    for path in inputs:
        try:
            if path.is_file() and path.stat().st_mtime > stamp:
                return False
        except OSError:
            return False
    return True


def _write_lipsync_report(output_dir: Path, outcomes: list[lipsync_service.LipsyncOutcome]):
    """把逐帧口型的结果落盘（哪个镜头做了什么、改写区间、耗时）。"""
    payload = {
        "engine_ok": sum(1 for item in outcomes if item.ok),
        "engine_total": len(outcomes),
        "measure_note": (
            "dub_span = 配音本身的说话区间；rewrite_span = 相对原片段「嘴部被改写」的"
            "时段（只说明哪里真的改过，**不是**同步误差指标。便宜的像素统计量量不准"
            "嘴的开合，详见 tools/lipsync/README.md 的实测记录）"
        ),
        "shots": [item.summary() for item in outcomes],
    }
    try:
        (output_dir / "lipsync_report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        logger.debug("口型报告落盘失败", exc_info=True)


def _bind_video(
    segment: ComposeSegment,
    manifest: VideoManifest,
    project_dir: Path,
    warnings: list[str],
) -> bool:
    clip = manifest.get(segment.shot_id)
    if clip is None or not clip.path:
        warnings.append(f"镜头 {segment.shot_id} 没有视频片段，已跳过")
        return False
    if not (project_dir / clip.path).is_file():
        warnings.append(f"镜头 {segment.shot_id} 的视频文件丢失，已跳过")
        return False
    segment.video_path = clip.path
    return True


async def _follow_clip_lengths(
    segments: list[ComposeSegment],
    manifest: VideoManifest,
    audio: AudioManifest | None,
    project_dir: Path,
) -> None:
    """段落切出来的镜头：成片时长 = 片段实际长度（但不短于配音需要的长度）。

    单镜生成的片段不动 —— 那里分镜时长就是下单时长，多出来的是档位多买的尾巴。
    """
    stretched = {a.shot_id for a in audio.adjustments} if audio else set()
    for segment in segments:
        clip = manifest.get(segment.shot_id)
        if clip is None or not clip.segment_id:
            continue
        length = await probe_duration(project_dir / clip.path)
        if not length:
            continue
        need = 0.0
        voice = audio.for_shot(segment.shot_id) if audio and segment.audio_path else None
        if voice is not None and voice.duration_sec > 0:
            need = max(0.0, voice.offset_sec) + voice.duration_sec
        if segment.shot_id in stretched:
            # 已经按配音拉长过的镜头保留拉长后的时长（尾部冻结帧是有意的）
            need = max(need, segment.duration_sec)
        segment.duration_sec = round(max(length, need), 3)


def _passthrough_groups(
    segments: list[ComposeSegment],
    manifest: VideoManifest,
    project_dir: Path,
    synced: dict[str, Path],
) -> dict[str, list[ComposeSegment]]:
    """哪些段落可以整段直通：段内所有镜头都在、连续、都用原声、没有配音文件、没做逐帧口型。

    缺任何一条（某个镜头要配 TTS、被跳过、整段文件不在了）就按单镜渲染。
    """
    members: dict[str, list[ComposeSegment]] = {}
    for segment in segments:
        clip = manifest.get(segment.shot_id)
        if clip is not None and clip.segment_id and clip.segment_path:
            members.setdefault(clip.segment_id, []).append(segment)
    groups: dict[str, list[ComposeSegment]] = {}
    for segment_id, group in members.items():
        expected = [c for c in manifest.clips if c.segment_id == segment_id]
        indexes = [s.index for s in group]
        if (
            len(group) == len(expected)
            and indexes == list(range(indexes[0], indexes[0] + len(group)))
            and all(s.native_voice and not s.audio_path for s in group)
            and not any(s.shot_id in synced for s in group)
            and (project_dir / expected[0].segment_path).is_file()
            and manifest.get(group[0].shot_id).segment_offset_sec == 0
        ):
            groups[segment_id] = group
    return groups


async def _render_passthrough(
    groups: dict[str, list[ComposeSegment]],
    manifest: VideoManifest,
    segments_dir: Path,
    project_dir: Path,
    *,
    width: int,
    height: int,
    fps: int,
    crf: int,
    preset: str,
    reuse: bool,
) -> None:
    """整段原片渲一次：长度 = 段内镜头时长之和（字幕时间轴照旧按镜头累加，两边对得上）。"""
    for segment_id, group in groups.items():
        source = project_dir / manifest.get(group[0].shot_id).segment_path
        duration = round(sum(s.duration_sec for s in group), 3)
        streams = await probe_streams(source)
        has_audio = bool(streams and "audio" in streams)
        output = segments_dir / f"{segment_id}.mp4"
        marker = segments_dir / f"{segment_id}.params"
        params = f"passthrough audio={'on' if has_audio else 'off'} dur={duration:.3f}"
        if reuse and _params_match(marker, params) and _segment_is_fresh(output, [source]):
            continue
        whole = ComposeSegment(
            shot_id=segment_id, index=group[0].index, duration_sec=duration, native_voice=True
        )
        await render_segment(
            whole,
            video_path=source,
            audio_path=None,
            output=output,
            width=width,
            height=height,
            fps=fps,
            crf=crf,
            preset=preset,
            ambient=has_audio,
            ambient_gain_db=0.0,
        )
        marker.write_text(params, encoding="utf-8")


def _params_match(marker: Path, params: str) -> bool:
    """分段是不是用这套参数渲的（比对标记文件内容）。"""
    try:
        if not marker.is_file():
            return False
        return marker.read_text(encoding="utf-8").strip() == params.strip()
    except OSError:  # pragma: no cover - 读不到就当不匹配，重渲一次
        return False


def _segment_is_fresh(output: Path, inputs: list[Path | None]) -> bool:
    if not output.is_file() or output.stat().st_size == 0:
        return False
    try:
        stamp = output.stat().st_mtime
    except OSError:  # pragma: no cover
        return False
    for item in inputs:
        if item is None:
            continue
        try:
            if item.is_file() and item.stat().st_mtime > stamp:
                return False
        except OSError:  # pragma: no cover
            return False
    return True


def _cleanup(*paths: Path | None) -> None:
    for path in paths:
        if path is None:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            logger.debug("清理合成中间产物失败: %s", path)


def _relative(path: Path | None, project_dir: Path) -> str:
    if path is None:
        return ""
    try:
        return str(Path(path).resolve().relative_to(Path(project_dir).resolve()))
    except ValueError:
        return str(path)


def _notify(callback: ProgressCallback, done: int, total: int, message: str) -> None:
    if callback is None:
        return
    try:
        callback(done, total, message)
    except Exception:
        logger.exception("合成进度回调执行失败")


__all__ = ["ComposeError", "compose", "render_segment"]
