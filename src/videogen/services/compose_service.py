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
from videogen.services import lipsync_service, subtitle_service
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
        chains.append(_resample("1:a", "voice", duration, delay_ms=voice_delay_ms))
        chains.append(_resample("0:a", "ambient", duration, gain_db=ambient_gain_db))
        chains.append(
            "[voice][ambient]amix=inputs=2:duration=longest:dropout_transition=0:"
            "normalize=0[a]"
        )
    elif voice:
        chains.append(_resample("1:a", "a", duration, delay_ms=voice_delay_ms))
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
) -> None:
    """把一个镜头渲染成「视频 + 该镜头配音」的标准片段。

    统一分辨率/帧率/采样率/声道，这样后续 concat 可以纯 stream copy。
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
    font: str = "",
    font_scale: float = 0.05,
    margin_scale: float = 0.04,
    resolution: tuple[int, int] = (1080, 1920),
    exe: str | None = None,
    bgm_loop: bool = True,
    duck: bool = False,
    duck_ratio: float = 6.0,
    duck_threshold_db: float = -30.0,
    duck_attack_ms: float = 25.0,
    duck_release_ms: float = 420.0,
) -> SubtitleMode:
    """最后一道工序：混 BGM（可带人声闪避）+ 挂字幕。返回真正生效的字幕模式。

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

    if burn:
        # 烧字幕必须重编码视频；顺带把 BGM 混进去，少走一遍
        args = ["-y", "-hide_banner", "-loglevel", "error", "-i", source.name]
        burn_filter = _burn_filter(
            subtitle_file.name,
            font=font,
            resolution=resolution,
            font_scale=font_scale,
            margin_scale=margin_scale,
        )
        if bgm_path:
            args += _bgm_input(bgm_path, loop=bgm_loop)
            args += [
                "-filter_complex",
                f"[0:v]{burn_filter}[v];{mix}",
                "-map",
                "[v]",
                "-map",
                "[a]",
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

    if bgm_path:
        args += [
            "-filter_complex",
            mix,
            "-map",
            "0:v",
            "-map",
            "[a]",
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



def subtitle_style(
    resolution: tuple[int, int], *, font_scale: float = 0.05, margin_scale: float = 0.04
) -> tuple[int, int]:
    """按输出分辨率算字幕字号与下边距（像素）。

    参照 min(宽, 高)：竖屏 1080x1920 与横屏 1920x1080 都能得到合适的字号 ——
    如果按视频高度算，竖屏字幕会大到溢出画面。
    """
    reference = max(240, min(resolution))
    font_px = round(reference * max(0.01, font_scale))
    margin_px = round(reference * max(0.0, margin_scale))
    return max(10, font_px), max(0, margin_px)


#: ffmpeg 把 SRT 转成 ASS 时用的默认参考分辨率（libavcodec 的 384x288）。
#: force_style 里的 FontSize/MarginV 都是**这个参考系**下的单位，
#: 直接写像素值会导致竖屏字幕被放大到溢出画面。
ASS_REFERENCE_HEIGHT = 288


def script_units(pixels: int, height: int) -> int:
    """把「想要的像素值」换算成 force_style 的单位。"""
    height = max(1, height)
    return max(1, round(pixels * ASS_REFERENCE_HEIGHT / height))


def _burn_filter(
    subtitle_name: str,
    *,
    font: str = "",
    resolution: tuple[int, int] = (1080, 1920),
    font_scale: float = 0.05,
    margin_scale: float = 0.04,
) -> str:
    font_px, margin_px = subtitle_style(
        resolution, font_scale=font_scale, margin_scale=margin_scale
    )
    font_units = script_units(font_px, resolution[1])
    margin_units = script_units(margin_px, resolution[1]) if margin_px else 0
    style = [
        f"FontSize={font_units}",
        "Outline=1",
        "Shadow=0",
        "Alignment=2",
        f"MarginV={margin_units}",
    ]
    if font:
        style.insert(0, f"FontName={font}")
    # cwd 就在产物目录，这里只写文件名，避开 subtitles 滤镜的路径转义地狱
    return f"subtitles={subtitle_name}:force_style='{','.join(style)}'"


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
    if audio_manifest is not None and not audio_manifest.is_usable:
        raise ComposeError(
            f"配音不完整（就绪 {len(audio_manifest.ready_clips)}/"
            f"{len(audio_manifest.clips)}，失败 {len(audio_manifest.failed_clips)}），"
            "请先补齐配音再合成"
        )
    if audio_manifest is None or not audio_manifest.clips:
        warnings.append("没有配音，成片为纯画面（无声）")
    if not storyboard.shots:
        raise ComposeError("分镜为空，无法合成")

    segments = subtitle_service.build_segments(
        storyboard, audio_manifest, speaker_names=subtitle_service.speaker_name_map(bible)
    )
    segments = [s for s in segments if _bind_video(s, video_manifest, project_dir, warnings)]
    if not segments:
        raise ComposeError("没有任何可用的视频片段")
    total_duration = round(sum(s.duration_sec for s in segments), 3)

    width, height = _resolution(project)
    fps = max(1, settings.video_fps)
    caps = await ffmpeg_capabilities()
    desired_mode = settings.subtitle_mode
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
    if settings.lipsync_enabled:
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

    mouth_delays = await _render_all(
        segments,
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

    segment_paths = [segments_dir / f"{s.shot_id}.mp4" for s in segments]
    _notify(on_progress, len(segments), len(segments) + 2, "拼接分段")

    base = output_dir / "final_base.mp4"
    concat_file = output_dir / "concat.txt"
    _concat_list(segments, segment_paths, concat_file)
    await _concat(concat_file, base, cwd=output_dir)

    # 无论什么模式都导出 .srt 侧挂文件：平台上传/二次编辑都用得上
    # 同时导出一份 .vtt：浏览器的 <track> 只认 VTT，认不出 SRT / mov_text
    subtitles_path: Path | None = None
    subtitles_vtt_path: Path | None = None
    srt = subtitle_service.build_srt(segments, audio_manifest)
    if srt.strip():
        subtitles_path = output_dir / "subtitles.srt"
        subtitles_path.write_text(srt, encoding="utf-8")
        subtitles_vtt_path = output_dir / "subtitles.vtt"
        subtitles_vtt_path.write_text(
            subtitle_service.build_vtt(segments, audio_manifest), encoding="utf-8"
        )
    else:
        subtitles_path = None
        warnings.append("没有台词内容，未生成字幕")

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
        mode=SubtitleMode(settings.subtitle_mode),
        caps=caps,
        exe=burn_exe,
        crf=settings.compose_crf,
        preset=settings.compose_preset,
        font=settings.compose_subtitle_font,
        font_scale=settings.compose_subtitle_font_scale,
        margin_scale=settings.compose_subtitle_margin_scale,
        resolution=(width, height),
        bgm_loop=bgm_loop,
        duck=duck,
        duck_ratio=settings.bgm_duck_ratio,
        duck_threshold_db=settings.bgm_duck_threshold_db,
        duck_attack_ms=settings.bgm_duck_attack_ms,
        duck_release_ms=settings.bgm_duck_release_ms,
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

    # 成片自检：音轨时长必须和视频基本相等。拼接丢掉音频空洞时（mp4 音轨表达不了
    # 空洞 → 后面所有声音整体提前）这个差值会等于空洞之和，一眼可见。
    sync = await check_av_sync(final)
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
        duration_sec=total_duration,
        subtitle_mode=SubtitleMode(settings.subtitle_mode),
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
        video_provider=video_manifest.provider,
        tts_provider=audio_manifest.provider if audio_manifest else "",
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
) -> dict[str, float]:
    synced = synced or {}
    semaphore = asyncio.Semaphore(concurrency)
    done = 0
    lock = asyncio.Lock()
    probe_cache: dict[str, bool] = {}
    delays: dict[str, float] = {}

    async def has_ambient(video_path: Path) -> bool:
        key = str(video_path)
        if key not in probe_cache:
            streams = await probe_streams(video_path)
            probe_cache[key] = bool(streams and "audio" in streams)
        return probe_cache[key]

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
            use_ambient = ambient and await has_ambient(video_path)
            # 口型对齐：画面里的人几秒开口，配音就延后几秒（只用本地计算，不重新烧视频）。
            # 逐帧口型已经让嘴跟着配音走了，再叠加延后会把嘴和声音错开。
            delay_ms = 0
            if (
                lipsynced is None
                and align_mouth
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
            # 环境音/口型对齐开关变了要重新渲染：编码到"输入"里参与新鲜度判断
            marker = segments_dir / (
                f".ambient-{'on' if use_ambient else 'off'}"
                f".mouth-{'on' if delay_ms > 0 else 'off'}"
                f".lipsync-{'on' if lipsynced is not None else 'off'}"
            )
            if reuse and marker.exists() and _segment_is_fresh(output, [video_path, audio_path]):
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
                    ambient_gain_db=ambient_gain_db,
                    voice_delay_ms=delay_ms,
                )
                marker.touch()
            async with lock:
                done += 1
                _notify(
                    on_progress,
                    done,
                    len(segments) + 2,
                    f"分段渲染 {done}/{len(segments)}",
                )

    await asyncio.gather(*(run_one(segment) for segment in segments))
    return delays


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
