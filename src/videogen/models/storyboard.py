from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.models.script import MIN_SCENE_DURATION_SEC
from videogen.utils.coerce import StrList, coerce_int, ensure_optional_str

DEFAULT_SHOT_DURATION_SEC = 4


class ShotDraft(BaseModel):
    """LLM 返回的镜头草稿（id/scene_id/shot_no 由系统分配，不信任模型输出）。"""

    shot_no: int | None = None
    duration_sec: int = DEFAULT_SHOT_DURATION_SEC
    camera: str = ""
    visual_description: str = ""
    character_in_frame: StrList = Field(default_factory=list)
    mood: str = ""
    video_prompt: str = ""
    narration: str | None = None
    dialogue_text: str | None = None
    dialogue_speaker: str | None = None

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, value: object) -> int:
        return max(1, coerce_int(value, DEFAULT_SHOT_DURATION_SEC))

    @field_validator("narration", "dialogue_text", "dialogue_speaker", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> str | None:
        return ensure_optional_str(value)


class Shot(BaseModel):
    id: str
    scene_id: str
    shot_no: int
    duration_sec: int = DEFAULT_SHOT_DURATION_SEC
    camera: str = ""  # e.g. "中景 · 固定"
    visual_description: str = ""
    character_in_frame: StrList = Field(default_factory=list)
    mood: str = ""
    video_prompt: str = ""
    narration: str | None = None
    dialogue_text: str | None = None
    dialogue_speaker: str | None = None

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, value: object) -> int:
        return max(1, coerce_int(value, DEFAULT_SHOT_DURATION_SEC))

    @field_validator("narration", "dialogue_text", "dialogue_speaker", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> str | None:
        return ensure_optional_str(value)


class Storyboard(BaseModel):
    shots: list[Shot] = Field(default_factory=list)
    duration_normalized: bool = False

    @property
    def total_duration_sec(self) -> int:
        return sum(s.duration_sec for s in self.shots)

    def shots_for_scene(self, scene_id: str) -> list[Shot]:
        return [s for s in self.shots if s.scene_id == scene_id]

    def scene_ids(self) -> set[str]:
        return {s.scene_id for s in self.shots}


__all__ = [
    "DEFAULT_SHOT_DURATION_SEC",
    "MIN_SCENE_DURATION_SEC",
    "Shot",
    "ShotDraft",
    "Storyboard",
]
