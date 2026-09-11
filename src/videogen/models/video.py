from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class ClipStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
    SKIPPED = "skipped"


class MediaKind(str, Enum):
    """产物是不是真正可用的媒体。

    mock provider 在缺少 ffmpeg 时会产出 `placeholder`，这类文件不能进入合成。
    """

    VIDEO = "video"
    AUDIO = "audio"
    PLACEHOLDER = "placeholder"


class VideoClip(BaseModel):
    shot_id: str
    scene_id: str = ""
    status: ClipStatus = ClipStatus.PENDING
    media_kind: MediaKind = MediaKind.VIDEO
    #: 相对项目目录的路径（便于整体搬移），如 "clips/scene_1_shot_1.mp4"
    path: str | None = None
    duration_sec: int = 0
    aspect_ratio: str = "9:16"
    provider: str = ""
    task_id: str | None = None
    attempts: int = 0
    error: str = ""
    prompt: str = ""
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class VideoManifest(BaseModel):
    provider: str = ""
    aspect_ratio: str = "9:16"
    clips: list[VideoClip] = Field(default_factory=list)
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: 生成时的并发度，方便排查
    concurrency: int = 1

    # ── 统计 ──────────────────────────────────────────
    @property
    def ready_clips(self) -> list[VideoClip]:
        return [c for c in self.clips if c.status == ClipStatus.READY]

    @property
    def failed_clips(self) -> list[VideoClip]:
        return [c for c in self.clips if c.status == ClipStatus.FAILED]

    @property
    def placeholder_clips(self) -> list[VideoClip]:
        return [c for c in self.ready_clips if c.media_kind == MediaKind.PLACEHOLDER]

    @property
    def has_placeholders(self) -> bool:
        return bool(self.placeholder_clips)

    @property
    def is_usable(self) -> bool:
        """所有片段都是真实视频且没有失败/待生成 —— 才能进入合成。"""
        return self.is_complete and not self.has_placeholders

    @property
    def pending_clips(self) -> list[VideoClip]:
        return [c for c in self.clips if c.status in {ClipStatus.PENDING, ClipStatus.GENERATING}]

    @property
    def total_duration_sec(self) -> int:
        return sum(c.duration_sec for c in self.ready_clips)

    @property
    def is_complete(self) -> bool:
        return bool(self.clips) and not self.failed_clips and not self.pending_clips

    def get(self, shot_id: str) -> VideoClip | None:
        for clip in self.clips:
            if clip.shot_id == shot_id:
                return clip
        return None

    def upsert(self, clip: VideoClip) -> None:
        for index, existing in enumerate(self.clips):
            if existing.shot_id == clip.shot_id:
                self.clips[index] = clip
                break
        else:
            self.clips.append(clip)
        self.updated_at = datetime.now(UTC).isoformat()

    def summary(self) -> dict[str, int | bool]:
        return {
            "total": len(self.clips),
            "ready": len(self.ready_clips),
            "failed": len(self.failed_clips),
            "pending": len(self.pending_clips),
            "placeholder": len(self.placeholder_clips),
            "duration_sec": self.total_duration_sec,
            "usable": self.is_usable,
        }
