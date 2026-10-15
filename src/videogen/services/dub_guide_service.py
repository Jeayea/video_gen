"""原声引导配音：让视频模型先念一遍，再用角色自己的音色照着念。

为什么：TTS 单独念台词，节奏和情绪都是它自己猜的，听起来单调；而且和画面里那张嘴
（模型按自己的原声在动）对不上。视频模型开着原生音频时（`VIDEO_GENERATE_AUDIO=true`，
台词进 prompt），它已经替我们「演」过一遍了 —— 停顿、重音、语速都和画面一致，
只是音色不受控制（每个镜头一个嗓子）。

    视频原声 ──找出这句话的起止──> audio/guide/<镜头>.wav（参考录音）
                                         │
    台词 + 角色音色 + 念法 + 参考录音 ──> 云端 TTS 照着念（音色是角色自己的）
                                         │
    时长微调到与原声一致（atempo 变速不变调，有上限）──> 放回原声开口的那一秒

画面里的嘴是按原声动的，配音落在同一段时间、同样的长度上，所以不需要再做逐帧口型。
Edge 等不收参考录音的 TTS 也能用：只是没有「照着念」，位置和时长对齐照做。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from videogen.models.storyboard import Storyboard
from videogen.models.video import ClipStatus, VideoManifest
from videogen.utils.audio_align import speech_span
from videogen.utils.ffmpeg import (
    FFmpegError,
    atempo_chain,
    probe_duration,
    probe_streams,
    run_ffmpeg,
)
from videogen.utils.text import clean_spoken_text

logger = logging.getLogger(__name__)

__all__ = [
    "GUIDE_DIR",
    "GUIDE_INSTRUCTION",
    "Guide",
    "atempo_chain",
    "fit_to_span",
    "prepare_guides",
]

GUIDE_DIR = "guide"
#: 参考录音的采样率：24k 单声道（与云端 TTS 的输出一致，体积小）
_GUIDE_RATE = 24000
#: 原声说话区间短于这个就当没探测到（多半是一声咳嗽 / 环境音）
_MIN_SPAN_SEC = 0.4
#: 配音与原声时长差在这个比例以内就不变速（变速多少都会有一点音质损失）
_FIT_TOLERANCE = 0.04
#: 裁掉配音前后的静音时，说话段外侧各留一点余量（别把字头字尾的气声切掉）
_TRIM_PAD_SEC = 0.04

#: 有原声参考时**只发这一句念法**，不再发「看画面写的念法」。
#: 实测（2026-10-10，《孔乙己》3 句，同一份原声）：念法里写着「语速慢」「先慢后快」时，
#: 模型在念法和参考录音之间打架 —— 念成原声的 1.3~2.7 倍长，还有两次自己加了内容
#: （被时长守卫拦下）；只给这一句时落在原声的 0.91~1.18 倍（gpt-audio）。
#: 情绪本来就在原声里，用不着再用文字描述一遍
GUIDE_INSTRUCTION = "照参考录音的情绪、语气、节奏和停顿来念，语速和参考录音一样"


@dataclass(frozen=True)
class Guide:
    shot_id: str
    #: 切出来的原声片段（参考录音）
    path: Path
    #: 原声里这句话在镜头中的起止（秒，相对镜头开头）
    start_sec: float
    end_sec: float

    @property
    def span_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


async def prepare_guides(
    storyboard: Storyboard,
    video: VideoManifest | None,
    project_dir: Path,
    out_dir: Path,
) -> dict[str, Guide]:
    """给每个「有台词、片段带音轨」的镜头切出原声片段；探测不到的镜头直接跳过。"""
    if video is None:
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    guides: dict[str, Guide] = {}
    for shot in storyboard.shots:
        if not clean_spoken_text(shot.dialogue_text):
            continue
        clip = video.get(shot.id)
        if clip is None or clip.status != ClipStatus.READY or not clip.path:
            continue
        source = Path(project_dir) / clip.path
        streams = await probe_streams(source)
        if not streams or "audio" not in streams:
            continue
        span = await speech_span(source, out_dir)
        if span is None or span[1] - span[0] < _MIN_SPAN_SEC:
            logger.info("镜头 %s 的原声里找不到说话段，按普通配音处理", shot.id)
            continue
        start, end = span
        target = out_dir / f"{shot.id}.wav"
        if not await _cut(source, target, start, end):
            continue
        guides[shot.id] = Guide(shot_id=shot.id, path=target, start_sec=start, end_sec=end)
    return guides


async def _cut(source: Path, target: Path, start: float, end: float) -> bool:
    try:
        await run_ffmpeg(
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                # 输出端 seek：输入端 seek 在某些 mp4 上会落到错误的窗口（见 AGENTS.md 响度一节）
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{max(0.05, end - start):.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(_GUIDE_RATE),
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
            timeout_sec=120,
        )
    except FFmpegError:
        logger.warning("切原声片段失败：%s", source.name, exc_info=True)
        return False
    return target.is_file() and target.stat().st_size > 0


async def fit_to_span(
    audio: Path, target_sec: float, *, max_ratio: float = 1.25
) -> tuple[Path, float, float]:
    """裁掉前后静音、再变速（不变调）到接近 `target_sec`，返回 (文件, 时长, 实际倍率)。

    倍率夹在 `[1/max_ratio, max_ratio]`：差太多时只拉到上限，宁可略长略短，
    也不把一句话压成花栗鼠或拖成慢动作。
    """
    duration = await probe_duration(audio) or 0.0
    if duration <= 0 or target_sec <= 0:
        return audio, duration, 1.0
    # 先裁掉前后静音再比长短：云端 TTS 的文件尾巴上常有 0.3~0.6 秒静音，
    # 原声那边量的是「说话段」，两边口径必须一致（不然静音也会被一起变速塞进去）
    span = await speech_span(audio, audio.parent)
    start, end = 0.0, duration
    if span is not None:
        start = max(0.0, span[0] - _TRIM_PAD_SEC)
        end = min(duration, span[1] + _TRIM_PAD_SEC)
    speech = max(0.05, end - start)
    factor = speech / target_sec
    trimmed = start > 0.01 or end < duration - 0.01
    if abs(factor - 1.0) <= _FIT_TOLERANCE:
        factor = 1.0
        if not trimmed:
            return audio, duration, 1.0
    limit = max(1.0, max_ratio)
    factor = min(limit, max(1.0 / limit, factor))
    filters = f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"
    if factor != 1.0:
        filters += f",{atempo_chain(factor)}"
    output = audio.with_name(f"{audio.stem}.fit.wav")
    try:
        await run_ffmpeg(
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(audio),
                "-filter:a",
                filters,
                "-c:a",
                "pcm_s16le",
                str(output),
            ],
            timeout_sec=120,
        )
    except FFmpegError:
        logger.warning("配音变速失败，保留原时长：%s", audio.name, exc_info=True)
        return audio, duration, 1.0
    fitted = await probe_duration(output) or 0.0
    if fitted <= 0:
        return audio, duration, 1.0
    return output, fitted, factor
