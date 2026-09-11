from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field

from videogen.models.video import ClipStatus, MediaKind


class AudioKind(str, Enum):
    """一条音频是「对白」还是「旁白」。"""

    DIALOGUE = "dialogue"
    NARRATION = "narration"


class BgmTrack(BaseModel):
    """选中的背景音乐（来自本地曲库）。"""

    id: str
    title: str = ""
    #: 曲库内的绝对路径字符串；合成时直接读，不复制进项目目录
    path: str = ""
    moods: list[str] = Field(default_factory=list)
    duration_sec: float = 0.0
    gain_db: float | None = None
    license: str = ""
    score: float = 0.0


class AudioClip(BaseModel):
    """单个镜头的配音产物（一个镜头最多一条音频）。

    设计取舍：一个镜头要么是「对白」要么是「旁白」。如果分镜同时给了两者，
    对白优先，旁白会被跳过并在 manifest.warnings 里记一笔 ——
    否则两条音频在同一镜头里会互相盖住。
    """

    id: str
    shot_id: str
    scene_id: str = ""
    kind: AudioKind = AudioKind.DIALOGUE
    #: 角色 id（与 story bible 对应），旁白固定为 narrator
    speaker: str = ""
    speaker_name: str = ""
    text: str = ""
    voice: str = ""
    status: ClipStatus = ClipStatus.PENDING
    media_kind: MediaKind = MediaKind.AUDIO
    #: 相对项目目录的路径，如 "audio/scene_1_shot_1.mp3"
    path: str | None = None
    duration_sec: float = 0.0
    provider: str = ""
    attempts: int = 0
    error: str = ""
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class DurationAdjustment(BaseModel):
    """音画对齐记录：镜头因为配音太长被拉长了多少。"""

    shot_id: str
    scene_id: str = ""
    from_sec: int
    to_sec: int
    audio_sec: float


class AudioManifest(BaseModel):
    provider: str = ""
    clips: list[AudioClip] = Field(default_factory=list)
    #: 实际选中的 BGM（可能为空 = 没有曲库/未匹配到）
    bgm: BgmTrack | None = None
    #: 说话人 -> 音色，落盘保证重跑时音色一致
    voice_map: dict[str, str] = Field(default_factory=dict)
    #: 音画对齐结果（镜头时长被拉长的记录）
    adjustments: list[DurationAdjustment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: 是否已经把时长对齐回分镜
    aligned: bool = False
    concurrency: int = 1
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    # ── 统计 ──────────────────────────────────────────
    @property
    def ready_clips(self) -> list[AudioClip]:
        return [c for c in self.clips if c.status == ClipStatus.READY]

    @property
    def failed_clips(self) -> list[AudioClip]:
        return [c for c in self.clips if c.status == ClipStatus.FAILED]

    @property
    def pending_clips(self) -> list[AudioClip]:
        return [c for c in self.clips if c.status in {ClipStatus.PENDING, ClipStatus.GENERATING}]

    @property
    def placeholder_clips(self) -> list[AudioClip]:
        return [c for c in self.ready_clips if c.media_kind == MediaKind.PLACEHOLDER]

    @property
    def has_clips(self) -> bool:
        return bool(self.clips)

    @property
    def is_complete(self) -> bool:
        # 空清单也算「完整」：分镜里没有台词时就是无声片，不该卡住流水线
        return not self.failed_clips and not self.pending_clips

    @property
    def is_usable(self) -> bool:
        """所有配音都是真实音频且没有失败 —— 才能进入合成。"""
        return self.is_complete and not self.placeholder_clips

    @property
    def total_duration_sec(self) -> float:
        return round(sum(c.duration_sec for c in self.ready_clips), 3)

    def get(self, clip_id: str) -> AudioClip | None:
        for clip in self.clips:
            if clip.id == clip_id:
                return clip
        return None

    def for_shot(self, shot_id: str) -> AudioClip | None:
        for clip in self.clips:
            if clip.shot_id == shot_id and clip.status == ClipStatus.READY:
                return clip
        return None

    def upsert(self, clip: AudioClip) -> None:
        for index, existing in enumerate(self.clips):
            if existing.id == clip.id:
                self.clips[index] = clip
                break
        else:
            self.clips.append(clip)
        self.updated_at = datetime.now(UTC).isoformat()

    def summary(self) -> dict[str, int | float | bool | str]:
        return {
            "total": len(self.clips),
            "ready": len(self.ready_clips),
            "failed": len(self.failed_clips),
            "pending": len(self.pending_clips),
            "placeholder": len(self.placeholder_clips),
            "duration_sec": self.total_duration_sec,
            "usable": self.is_usable,
            "bgm": self.bgm.title or self.bgm.id if self.bgm else "",
        }


__all__ = [
    "AudioClip",
    "AudioKind",
    "AudioManifest",
    "BgmTrack",
    "DurationAdjustment",
    "MediaKind",
]
