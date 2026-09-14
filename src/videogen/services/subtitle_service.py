from __future__ import annotations

import logging

from videogen.models.audio import AudioKind, AudioManifest
from videogen.models.bible import StoryBible
from videogen.models.compose import ComposeSegment
from videogen.models.storyboard import Storyboard
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
            )
        )
    return segments


def _subtitle_text(shot, audio_clip, names: dict[str, str]) -> str:
    if audio_clip is not None and clean_spoken_text(audio_clip.text):
        text = clean_spoken_text(audio_clip.text)
        if audio_clip.kind == AudioKind.DIALOGUE and audio_clip.speaker:
            name = audio_clip.speaker_name or names.get(audio_clip.speaker, "")
            if name:
                return f"{name}：{text}"
        return text
    return clean_spoken_text(shot.dialogue_text) or clean_spoken_text(shot.narration)


def speaker_name_map(bible: StoryBible | None) -> dict[str, str]:
    if not bible:
        return {}
    return {c.id: str(c.name or c.id) for c in bible.characters}


def build_srt(
    segments: list[ComposeSegment],
    audio: AudioManifest | None = None,
) -> str:
    """生成 SRT。时间轴按段落时长累加，和成片完全一致。"""
    cues = _cues(segments, audio)
    return "\n".join(f"{index}\n{start} --> {end}\n{text}\n" for index, start, end, text in cues)


def build_vtt(
    segments: list[ComposeSegment],
    audio: AudioManifest | None = None,
) -> str:
    """生成 WebVTT。

    和 SRT 的内容完全一样，只是时间戳用 `.` 分隔毫秒 ——
    浏览器的 `<track>` **只认 VTT**，认不出 SRT（也不认 mp4 里的 mov_text 软轨），
    所以 Web 预览必须有一份 VTT。
    """
    cues = _cues(segments, audio)
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
    segments: list[ComposeSegment], audio: AudioManifest | None
) -> list[tuple[int, str, str, str]]:
    """算出每条字幕的 (序号, 起, 止, 文本)，SRT / VTT 共用。"""
    cues: list[tuple[int, str, str, str]] = []
    cursor = 0.0
    for segment in segments:
        start = cursor
        end = cursor + segment.duration_sec
        cursor = end

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
            end = min(end, start + max(_MIN_CUE_SEC, speech + _TAIL_HOLD_SEC))

        cues.append((len(cues) + 1, _stamp(start), _stamp(end), text))
    return cues


def _stamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


__all__ = ["build_segments", "build_srt", "build_vtt", "speaker_name_map", "srt_to_vtt"]
