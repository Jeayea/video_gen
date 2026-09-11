from __future__ import annotations

import asyncio
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
from videogen.services import subtitle_service
from videogen.utils.ffmpeg import (
    ffmpeg_capabilities,
    resolve_ffmpeg,
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


def _segment_filter(width: int, height: int, duration: float, fps: int, has_audio: bool) -> str:
    video = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps},format=yuv420p,"
        # 片段比镜头短就冻结最后一帧补齐（不重新烧视频额度）
        f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[v]"
    )
    if has_audio:
        audio = (
            f"[1:a]aresample=44100,aformat=channel_layouts=stereo,"
            f"apad,atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[a]"
        )
    else:
        audio = f"[1:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[a]"
    return f"{video};{audio}"


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
) -> None:
    """把一个镜头渲染成「视频 + 该镜头配音」的标准片段。

    统一分辨率/帧率/采样率/声道，这样后续 concat 可以纯 stream copy。
    """
    duration = max(0.2, float(segment.duration_sec))
    args = ["-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path)]
    has_audio = audio_path is not None and Path(audio_path).is_file()
    if has_audio:
        args += ["-i", str(audio_path)]
    else:
        # 没有配音也要有音轨，否则 concat 时各段流不一致
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    args += [
        "-filter_complex",
        _segment_filter(width, height, duration, fps, has_audio),
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
) -> SubtitleMode:
    """最后一道工序：混 BGM + 挂字幕。返回真正生效的字幕模式。"""
    effective = mode
    if mode == SubtitleMode.SOFT and "mov_text" not in caps:
        effective = SubtitleMode.NONE
    if mode == SubtitleMode.BURN and ("subtitles" not in caps or subtitle_file is None):
        effective = SubtitleMode.SOFT if "mov_text" in caps else SubtitleMode.NONE
    if effective == SubtitleMode.SOFT and "mov_text" not in caps:
        effective = SubtitleMode.NONE

    burn = effective == SubtitleMode.BURN and subtitle_file is not None
    soft = effective == SubtitleMode.SOFT and subtitle_file is not None

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
            args += ["-stream_loop", "-1", "-i", str(bgm_path)]
            args += [
                "-filter_complex",
                f"[0:v]{burn_filter}[v];{_bgm_filter(1, bgm_gain_db, fade_sec, duration)}",
                "-map",
                "[v]",
                "-map",
                "[bgm]",
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
        await run_ffmpeg(args, timeout_sec=BURN_TIMEOUT_SEC, cwd=cwd)
        return effective

    # 软字幕 / 无字幕：视频流可以 copy，只有需要混音时才对音频重编码
    # source 已经是拼接好的 mp4，直接当普通输入读。
    # 注意：所有 -i 必须写在任何输出选项（-map/-c...）之前，否则 ffmpeg 会把
    # -map 当成下一个输入文件的输入选项。
    args = ["-y", "-hide_banner", "-loglevel", "error", "-i", source.name]
    if bgm_path:
        args += ["-stream_loop", "-1", "-i", str(bgm_path)]
    if soft:
        args += ["-i", subtitle_file.name]

    if bgm_path:
        args += [
            "-filter_complex",
            f"{_bgm_filter(1, bgm_gain_db, fade_sec, duration)};"
            f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
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


def _subtitle_input_index(bgm_path: Path | None) -> int:
    return 2 if bgm_path else 1


def _bgm_filter(index: int, gain_db: float, fade_sec: float, duration: float) -> str:
    fade = max(0.0, min(fade_sec, duration / 3 if duration else fade_sec))
    fade_out_start = max(0.0, duration - fade)
    return (
        f"[{index}:a]volume={gain_db:.1f}dB,"
        f"afade=t=in:st=0:d={fade:.2f},"
        f"afade=t=out:st={fade_out_start:.2f}:d={fade:.2f}[bgm]"
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
    if settings.subtitle_mode == SubtitleMode.BURN and "subtitles" not in caps:
        warnings.append("当前 ffmpeg 没有 libass，无法烧字幕，已降级为内嵌软字幕")
    if settings.subtitle_mode != SubtitleMode.NONE and "mov_text" not in caps:
        warnings.append("当前 ffmpeg 不支持 mov_text，字幕只导出 .srt 侧挂文件")

    segments_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _notify(on_progress, 0, len(segments) + 2, "开始渲染分段")

    await _render_all(
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
    )

    segment_paths = [segments_dir / f"{s.shot_id}.mp4" for s in segments]
    _notify(on_progress, len(segments), len(segments) + 2, "拼接分段")

    base = output_dir / "final_base.mp4"
    concat_file = output_dir / "concat.txt"
    _concat_list(segments, segment_paths, concat_file)
    await _concat(concat_file, base, cwd=output_dir)

    # 无论什么模式都导出 .srt 侧挂文件：平台上传/二次编辑都用得上
    subtitles_path: Path | None = None
    srt = subtitle_service.build_srt(segments, audio_manifest)
    if srt.strip():
        subtitles_path = output_dir / "subtitles.srt"
        subtitles_path.write_text(srt, encoding="utf-8")
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

    final = output_dir / "final.mp4"
    _notify(on_progress, len(segments) + 1, len(segments) + 2, "混音 / 挂字幕")
    effective_mode = await _finish(
        base,
        final,
        cwd=output_dir,
        duration=total_duration,
        bgm_path=bgm_path,
        bgm_gain_db=bgm.gain_db if bgm and bgm.gain_db is not None else settings.bgm_gain_db,
        fade_sec=settings.bgm_fade_sec,
        subtitle_file=subtitles_path,
        mode=SubtitleMode(settings.subtitle_mode),
        caps=caps,
        crf=settings.compose_crf,
        preset=settings.compose_preset,
        font=settings.compose_subtitle_font,
        font_scale=settings.compose_subtitle_font_scale,
        margin_scale=settings.compose_subtitle_margin_scale,
        resolution=(width, height),
    )

    if not final.exists() or final.stat().st_size == 0:
        raise ComposeError("ffmpeg 未产出有效成片文件")

    _cleanup(base, concat_file, segments_dir if not settings.compose_keep_segments else None)

    report = ComposeReport(
        output_path=_relative(final, project_dir),
        subtitles_path=_relative(subtitles_path, project_dir) if subtitles_path else None,
        duration_sec=total_duration,
        subtitle_mode=SubtitleMode(settings.subtitle_mode),
        subtitle_mode_effective=effective_mode,
        segments=segments,
        bgm_id=bgm.id if bgm else "",
        bgm_path=str(bgm.path) if bgm else "",
        bgm_gain_db=bgm.gain_db if bgm and bgm.gain_db is not None else settings.bgm_gain_db,
        video_provider=video_manifest.provider,
        tts_provider=audio_manifest.provider if audio_manifest else "",
        ffmpeg=resolve_ffmpeg() or "",
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
) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    done = 0
    lock = asyncio.Lock()

    async def run_one(segment: ComposeSegment) -> None:
        nonlocal done
        async with semaphore:
            output = segments_dir / f"{segment.shot_id}.mp4"
            video_path = project_dir / segment.video_path
            audio_path = project_dir / segment.audio_path if segment.audio_path else None
            if reuse and _segment_is_fresh(output, [video_path, audio_path]):
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
                )
            async with lock:
                done += 1
                _notify(
                    on_progress,
                    done,
                    len(segments) + 2,
                    f"分段渲染 {done}/{len(segments)}",
                )

    await asyncio.gather(*(run_one(segment) for segment in segments))


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
