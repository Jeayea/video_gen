from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class SubtitleMode(str, Enum):
    SOFT = "soft"  # mp4 内嵌 mov_text 字幕轨（不需要 libass）
    BURN = "burn"  # 烧进画面（需要 libass）
    NONE = "none"  # 只导出 .srt 侧挂文件


class ComposeSegment(BaseModel):
    """一个镜头在成片里的片段（视频 + 该镜头的配音）。"""

    shot_id: str
    scene_id: str = ""
    index: int = 0
    duration_sec: float = 0.0
    video_path: str = ""
    audio_path: str | None = None
    text: str = ""


class ComposeReport(BaseModel):
    """合成结果。路径都是相对项目目录的，方便整体搬移。"""

    output_path: str = ""
    subtitles_path: str | None = None
    #: 浏览器可直接播放的 WebVTT 侧挂字幕（UI 预览用；soft/burn 都导出）
    subtitles_vtt_path: str | None = None
    duration_sec: float = 0.0
    subtitle_mode: SubtitleMode = SubtitleMode.SOFT
    #: 实际生效的字幕模式（请求 burn 但 ffmpeg 没有 libass 时会降级）
    subtitle_mode_effective: SubtitleMode | None = None
    segments: list[ComposeSegment] = Field(default_factory=list)
    bgm_id: str = ""
    bgm_path: str = ""
    #: 环境音（视频模型自带音轨）的处理方式与实际生效值
    ambient_mode: str = "off"
    ambient_mode_effective: str = "off"
    ambient_gain_db: float = 0.0
    bgm_gain_db: float = 0.0
    video_provider: str = ""
    tts_provider: str = ""
    ffmpeg: str = ""
    #: 真正执行烧字幕的那个 ffmpeg（主 ffmpeg 无 libass 时是另一个候选）
    subtitle_ffmpeg: str = ""
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def summary(self) -> dict[str, object]:
        return {
            "segments": len(self.segments),
            "duration_sec": self.duration_sec,
            "subtitle_mode": self.subtitle_mode_effective or self.subtitle_mode,
            "subtitles": bool(self.subtitles_path),
            "subtitles_vtt": bool(self.subtitles_vtt_path),
            "bgm": self.bgm_id,
            "ambient": self.ambient_mode_effective,
            "warnings": len(self.warnings),
        }


__all__ = ["ComposeReport", "ComposeSegment", "SubtitleMode"]
