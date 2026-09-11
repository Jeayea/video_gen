from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class AspectRatio(str, Enum):
    VERTICAL = "9:16"
    HORIZONTAL = "16:9"
    SQUARE = "1:1"


class InputMode(str, Enum):
    THEME = "theme"
    OUTLINE = "outline"
    FULL_SCRIPT = "full_script"


class ProjectStatus(str, Enum):
    """阶段状态机。

    文本链路（当前已实现）：
        DRAFT → SCRIPT_REVIEW → SCRIPT_CONFIRMED
              → STORYBOARD_REVIEW → STORYBOARD_CONFIRMED

    媒体链路（Phase 2+ 预留）：VIDEO_* / AUDIO_* / COMPOSING / DONE
    """

    DRAFT = "draft"
    SCRIPT_REVIEW = "script_review"
    SCRIPT_CONFIRMED = "script_confirmed"
    STORYBOARD_REVIEW = "storyboard_review"
    STORYBOARD_CONFIRMED = "storyboard_confirmed"

    # ── Phase 2+ 预留（目前没有任何入口会写入）──
    VIDEO_GENERATING = "video_generating"
    VIDEO_READY = "video_ready"
    AUDIO_GENERATING = "audio_generating"
    AUDIO_READY = "audio_ready"
    COMPOSING = "composing"
    DONE = "done"
    FAILED = "failed"

    # ── 历史遗留值（旧数据里出现过，仅用于兼容读取）──
    SCRIPT_GENERATED = "script_generated"
    STORYBOARD_GENERATED = "storyboard_generated"


#: 旧状态 → 新状态。旧代码把 SCRIPT_GENERATED 当「剧本已确认」用。
LEGACY_STATUS_MAP: dict[str, str] = {
    ProjectStatus.SCRIPT_GENERATED.value: ProjectStatus.SCRIPT_CONFIRMED.value,
    ProjectStatus.STORYBOARD_GENERATED.value: ProjectStatus.STORYBOARD_CONFIRMED.value,
}

#: 旧 VIDEO_READY 在 Phase 2 落地前等价于「分镜已确认」，仅在磁盘上确实没有视频产物时迁移
LEGACY_VIDEO_READY = ProjectStatus.VIDEO_READY.value
LEGACY_VIDEO_READY_TARGET = ProjectStatus.STORYBOARD_CONFIRMED.value


class Project(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    title: str = "未命名项目"
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: ProjectStatus = ProjectStatus.DRAFT
    aspect_ratio: AspectRatio = AspectRatio.VERTICAL
    target_duration_sec: int = 45
    input_mode: InputMode = InputMode.THEME
    input_text: str = ""

    @property
    def output_resolution(self) -> tuple[int, int]:
        return {
            AspectRatio.VERTICAL: (1080, 1920),
            AspectRatio.HORIZONTAL: (1920, 1080),
            AspectRatio.SQUARE: (1080, 1080),
        }[self.aspect_ratio]
