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
    #: 该镜在成片里的时长（**变速前**口径 = 分镜时长；成片实际观感要除以 `speed`）
    duration_sec: float = 0.0
    video_path: str = ""
    audio_path: str | None = None
    text: str = ""
    #: 逐句念法里的音量（如 "+5%" / "-20%"）。响度归一前会把它从文件里「减掉」，
    #: 所以归一之后要按 dB 加回来 —— 对齐的是基准音量，不是表演
    volume: str = ""
    #: 这条配音在合成时实际补了多少 dB（响度归一的结果，写进报告）
    gain_db: float = 0.0
    #: 画外音：画面里没人开口，不做「配音对齐到开口」
    voice_offscreen: bool = False
    #: 配音放在镜头里的第几秒（原声引导算出来的；>0 时不再做起点探测，也不做逐帧口型）
    voice_offset_sec: float = 0.0


class ComposeReport(BaseModel):
    """合成结果。路径都是相对项目目录的，方便整体搬移。"""

    output_path: str = ""
    subtitles_path: str | None = None
    #: 浏览器可直接播放的 WebVTT 侧挂字幕（UI 预览用；soft/burn 都导出）
    subtitles_vtt_path: str | None = None
    duration_sec: float = 0.0
    #: 变速前的原始时长（= 分镜总时长；`speed == 1` 时与 `duration_sec` 相同）。
    #: 交付的是 `duration_sec`（变速后），分镜/配音的时长口径仍是这一个
    source_duration_sec: float = 0.0
    #: 成片变速倍率（1.0 = 原速）。2.0 = 2 倍速快放，成片时长是原始时长的一半
    speed: float = 1.0
    subtitle_mode: SubtitleMode = SubtitleMode.SOFT
    #: 实际生效的字幕模式（请求 burn 但 ffmpeg 没有 libass 时会降级）
    subtitle_mode_effective: SubtitleMode | None = None
    segments: list[ComposeSegment] = Field(default_factory=list)
    bgm_id: str = ""
    bgm_path: str = ""
    #: 配乐来源：library（曲库）/ generate（本机现写）/ cli（外部模型）
    bgm_engine: str = ""
    #: 生成曲的配乐谱（相对项目目录）
    bgm_plan_path: str = ""
    #: 人声闪避是否真的生效（ffmpeg 缺 sidechaincompress 时会退回固定音量）
    bgm_duck: bool = False
    #: 逐条配音响度归一：开关、目标、每条的增益（shot_id -> dB）
    audio_normalize: bool = False
    audio_target_lufs: float = -20.0
    voice_gains: dict[str, float] = Field(default_factory=dict)
    #: 成片整体响度归一：开关、目标、渲染后实测的集成响度/真峰值
    loudness_normalize: bool = False
    target_lufs: float = -16.0
    final_lufs: float | None = None
    final_true_peak_db: float | None = None
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
            "source_duration_sec": self.source_duration_sec,
            "speed": self.speed,
            "subtitle_mode": self.subtitle_mode_effective or self.subtitle_mode,
            "subtitles": bool(self.subtitles_path),
            "subtitles_vtt": bool(self.subtitles_vtt_path),
            "bgm": self.bgm_id,
            "bgm_engine": self.bgm_engine,
            "bgm_duck": self.bgm_duck,
            "audio_normalize": self.audio_normalize,
            "voice_gain_db": (
                round(max(abs(v) for v in self.voice_gains.values()), 2)
                if self.voice_gains
                else 0.0
            ),
            "final_lufs": self.final_lufs,
            "ambient": self.ambient_mode_effective,
            "warnings": len(self.warnings),
        }


__all__ = ["ComposeReport", "ComposeSegment", "SubtitleMode"]
