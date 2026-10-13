from __future__ import annotations

import logging
from dataclasses import dataclass

from videogen.models.audio import AudioKind, AudioManifest
from videogen.models.bible import StoryBible
from videogen.models.compose import ComposeSegment
from videogen.models.storyboard import Storyboard
from videogen.utils import language
from videogen.utils.text import clean_spoken_text

logger = logging.getLogger(__name__)

#: 字幕比音频多留一点时间，读起来才不赶
_TAIL_HOLD_SEC = 0.35
_MIN_CUE_SEC = 0.6


def build_segments(
    storyboard: Storyboard,
    audio: AudioManifest | None,
    *,
    speaker_names: dict[str, str] | None = None,
) -> list[ComposeSegment]:
    """按分镜顺序排片：每段时长 = 镜头时长（已按配音对齐过）。"""
    names = speaker_names or {}
    segments: list[ComposeSegment] = []
    for index, shot in enumerate(storyboard.shots):
        audio_clip = audio.for_shot(shot.id) if audio else None
        text = _subtitle_text(shot, audio_clip, names)
        segments.append(
            ComposeSegment(
                shot_id=shot.id,
                scene_id=shot.scene_id,
                index=index,
                duration_sec=float(max(1, shot.duration_sec)),
                audio_path=audio_clip.path if audio_clip else None,
                text=text,
                volume=(audio_clip.volume if audio_clip else "") or "",
                voice_offscreen=shot.voice_offscreen,
                voice_offset_sec=audio_clip.offset_sec if audio_clip else 0.0,
            )
        )
    return segments


def _subtitle_text(shot, audio_clip, names: dict[str, str]) -> str:
    if audio_clip is not None and clean_spoken_text(audio_clip.text):
        text = clean_spoken_text(audio_clip.text)
        if audio_clip.kind == AudioKind.DIALOGUE and audio_clip.speaker:
            name = audio_clip.speaker_name or names.get(audio_clip.speaker, "")
            if name:
                colon = "：" if language.spoken_language_name(text) != "English" else ": "
                return f"{name}{colon}{text}"
        return text
    return clean_spoken_text(shot.dialogue_text) or clean_spoken_text(shot.narration)


def speaker_name_map(bible: StoryBible | None) -> dict[str, str]:
    if not bible:
        return {}
    return {c.id: str(c.name or c.id) for c in bible.characters}


def build_srt(
    segments: list[ComposeSegment],
    audio: AudioManifest | None = None,
    *,
    speed: float = 1.0,
) -> str:
    """生成 SRT。时间轴按段落时长累加，和成片完全一致。

    `speed` 是成片的变速倍率：成片被整体加速后，字幕时间轴也必须一起缩短，
    否则侧挂字幕会比画面晚（`speed > 1` 时）。烧进画面的字幕**不能**传它 ——
    那趟烧字发生在变速之前，用的是原始时间轴。
    """
    cues = _cues(segments, audio, speed=speed)
    return "\n".join(f"{index}\n{start} --> {end}\n{text}\n" for index, start, end, text in cues)


def build_vtt(
    segments: list[ComposeSegment],
    audio: AudioManifest | None = None,
    *,
    speed: float = 1.0,
) -> str:
    """生成 WebVTT。

    和 SRT 的内容完全一样，只是时间戳用 `.` 分隔毫秒 ——
    浏览器的 `<track>` **只认 VTT**，认不出 SRT（也不认 mp4 里的 mov_text 软轨），
    所以 Web 预览必须有一份 VTT。
    """
    cues = _cues(segments, audio, speed=speed)
    body = "\n".join(
        f"{index}\n{start.replace(',', '.')} --> {end.replace(',', '.')}\n{text}\n"
        for index, start, end, text in cues
    )
    return f"WEBVTT\n\n{body}" if body else "WEBVTT\n"


def srt_to_vtt(srt: str) -> str:
    """把 SRT 文本转成 WebVTT（给「已经合成过、还没导出 vtt」的老项目用）。"""
    text = (srt or "").replace("\r\n", "\n").strip()
    if not text:
        return "WEBVTT\n"
    if text.startswith("WEBVTT"):
        return text if text.endswith("\n") else f"{text}\n"
    lines: list[str] = []
    for line in text.split("\n"):
        if "-->" in line:
            # 00:00:01,000 --> 00:00:04,000  →  点号毫秒
            head, _, tail = line.partition("-->")
            line = f"{head.strip().replace(',', '.')} --> {tail.strip().replace(',', '.')}"
        lines.append(line)
    return "WEBVTT\n\n" + "\n".join(lines) + "\n"


def _cues(
    segments: list[ComposeSegment], audio: AudioManifest | None, *, speed: float = 1.0
) -> list[tuple[int, str, str, str]]:
    """算出每条字幕的 (序号, 起, 止, 文本)，SRT / VTT 共用。"""
    return [
        (index, _stamp(start), _stamp(end), text)
        for index, (start, end, text) in enumerate(_cue_times(segments, audio, speed=speed), 1)
    ]


def _cue_times(
    segments: list[ComposeSegment], audio: AudioManifest | None, *, speed: float = 1.0
) -> list[tuple[float, float, str]]:
    """每条字幕的 (起, 止, 文本)，单位秒。SRT / VTT / ASS 共用同一条时间轴。

    `speed > 1`（成片加速）时所有时间戳按 `1/speed` 缩放，跟着画面一起变短。
    """
    scale = 1.0 / max(0.01, float(speed))
    cues: list[tuple[float, float, str]] = []
    cursor = 0.0
    for segment in segments:
        start = cursor
        end = cursor + segment.duration_sec * scale
        cursor = end
        # 原声引导的配音不从镜头开头说起：字幕跟着开口的那一秒出现
        start = min(end, start + max(0.0, segment.voice_offset_sec) * scale)

        text = segment.text.strip()
        if not text:
            continue

        # 字幕只覆盖「说话的那几秒」，不要糊满整个镜头
        speech = 0.0
        if audio is not None:
            clip = audio.for_shot(segment.shot_id)
            if clip is not None:
                speech = clip.duration_sec
        if speech > 0:
            end = min(end, start + max(_MIN_CUE_SEC, (speech + _TAIL_HOLD_SEC) * scale))

        cues.append((start, end, text))
    return cues


# ── 烧字幕的样式（ASS）────────────────────────────────────


@dataclass(frozen=True)
class SubtitleLook:
    """一套烧字幕样式。尺寸都是 min(宽, 高) 的比例，横竖屏通用。

    下边距不归样式管（见 `build_ass`）：竖屏要躲开平台底部的按钮和标题，和好不好看无关。

    颜色是 ASS 的 `&HAABBGGRR`（AA=00 不透明，FF 全透明）。
    """

    font: str
    bold: bool
    primary: str
    outline_colour: str
    back_colour: str
    #: 1 = 描边 + 阴影；3 = 半透明底条（outline 当底条内边距用）
    border_style: int
    font_scale: float
    outline: float
    shadow: float
    blur: float
    spacing: float


#: 实测对比图：projects/20261012-天工开物-杀青-现代版/ab_subtitles/sub_compare.png
#: （1440 宽的竖屏上调的值，按 1440 换算成比例）
SUBTITLE_LOOKS: dict[str, SubtitleLook] = {
    # 柔和：常规字重、略灰的白、半透明描边 + 模糊 + 软阴影（默认）
    "soft": SubtitleLook(
        font="Hiragino Sans GB",
        bold=False,
        primary="&H00EDEDED",
        outline_colour="&H60000000",
        back_colour="&H80000000",
        border_style=1,
        font_scale=60 / 1440,
        outline=2.5 / 1440,
        shadow=3 / 1440,
        blur=4 / 1440,
        spacing=4 / 1440,
    ),
    # 半透明底条：平台感，亮背景上最清楚
    "box": SubtitleLook(
        font="Hiragino Sans GB",
        bold=False,
        primary="&H00F5F5F5",
        outline_colour="&H8C000000",
        back_colour="&H8C000000",
        border_style=3,
        font_scale=58 / 1440,
        outline=14 / 1440,
        shadow=0.0,
        blur=0.0,
        spacing=3 / 1440,
    ),
    # 纪录片：宋体加粗、暖白、模糊 + 软阴影、字距拉开
    "doc": SubtitleLook(
        font="Songti SC",
        bold=True,
        primary="&H00E8F0F2",
        outline_colour="&H70000000",
        back_colour="&H90000000",
        border_style=1,
        font_scale=64 / 1440,
        outline=2 / 1440,
        shadow=2 / 1440,
        blur=5 / 1440,
        spacing=6 / 1440,
    ),
    # 老样式：纯白粗体 + 硬描边（v0.21 之前的观感）
    "classic": SubtitleLook(
        font="",
        bold=True,
        primary="&H00FFFFFF",
        outline_colour="&H00000000",
        back_colour="&H00000000",
        border_style=1,
        font_scale=0.05,
        outline=1.5 / 1440,
        shadow=0.0,
        blur=0.0,
        spacing=0.0,
    ),
}
DEFAULT_SUBTITLE_LOOK = "soft"
_PORTRAIT_MARGIN = 0.12
_LANDSCAPE_MARGIN = 0.07


def subtitle_look(name: str) -> tuple[str, SubtitleLook]:
    """按名字取样式；不认识的名字退回默认（配置笔误不该让合成失败）。"""
    key = (name or "").strip().lower()
    if key not in SUBTITLE_LOOKS:
        key = DEFAULT_SUBTITLE_LOOK
    return key, SUBTITLE_LOOKS[key]


def build_ass(
    segments: list[ComposeSegment],
    audio: AudioManifest | None = None,
    *,
    resolution: tuple[int, int],
    look: SubtitleLook,
    font: str = "",
    font_scale: float = 0.0,
    margin_scale: float = 0.0,
) -> str:
    """生成烧字幕用的 ASS。

    `PlayResX/Y` 直接等于成片分辨率，所以字号 / 边距 / 描边都是**真实像素**，
    不用再往 libass 默认的 384x288 参考系里换算。`font` / `font_scale` /
    `margin_scale` 给了（非空 / >0）就压过样式里的值。

    和 `build_srt` 一样**不接收 speed**：烧字发生在变速之前，用原始时间轴。
    """
    width, height = (max(1, int(v)) for v in resolution)
    reference = max(240, min(width, height))

    def px(ratio: float) -> float:
        return round(reference * max(0.0, ratio), 1)

    font_px = max(10, round(reference * (font_scale if font_scale > 0 else look.font_scale)))
    if margin_scale > 0:
        margin_px = round(reference * margin_scale)
    else:
        # 竖屏离底边 12%（躲开抖音 / 视频号底部的标题和按钮），横屏 7%
        margin_px = round(height * (_PORTRAIT_MARGIN if height > width else _LANDSCAPE_MARGIN))
    side_px = round(reference * 0.04)
    style = ",".join(
        str(v)
        for v in (
            "Default",
            font or look.font or "Arial",
            font_px,
            look.primary,
            look.primary,
            look.outline_colour,
            look.back_colour,
            -1 if look.bold else 0,
            0,
            0,
            0,
            100,
            100,
            px(look.spacing),
            0,
            look.border_style,
            px(look.outline),
            px(look.shadow),
            2,
            side_px,
            side_px,
            margin_px,
            1,
        )
    )
    prefix = f"{{\\blur{px(look.blur):g}}}" if look.blur > 0 else ""
    events = [
        f"Dialogue: 0,{_ass_stamp(start)},{_ass_stamp(end)},Default,,0,0,0,,"
        f"{prefix}{_ass_text(text)}"
        for start, end, text in _cue_times(segments, audio)
    ]
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {style}",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    return "\n".join(header + events) + "\n"


def _ass_stamp(seconds: float) -> str:
    total_cs = max(0, round(seconds * 100))
    hours, remainder = divmod(total_cs, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_text(text: str) -> str:
    """ASS 里 `{}` 是样式标签、`\\N` 是换行：台词里的花括号换成全角，换行转成 `\\N`。"""
    cleaned = text.replace("{", "｛").replace("}", "｝").replace("\\", "＼")
    return "\\N".join(line.strip() for line in cleaned.splitlines() if line.strip())


def _stamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


__all__ = [
    "DEFAULT_SUBTITLE_LOOK",
    "SUBTITLE_LOOKS",
    "SubtitleLook",
    "build_ass",
    "build_segments",
    "build_srt",
    "build_vtt",
    "speaker_name_map",
    "srt_to_vtt",
    "subtitle_look",
]
