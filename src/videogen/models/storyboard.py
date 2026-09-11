from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.models.script import MIN_SCENE_DURATION_SEC
from videogen.utils.coerce import StrList, coerce_int, ensure_optional_str

DEFAULT_SHOT_DURATION_SEC = 4


class VoiceDirection(BaseModel):
    """一条配音文案的念法建议（LLM 按画面情绪给，留空 = 用全局设置）。"""

    rate: str = ""
    volume: str = ""
    pitch: str = ""
    note: str = ""


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
    #: 配音文案来源标记："llm:narration" / "llm:polish" / "llm:full" = 这条是
    #: LLM 看着画面写的；空 = 剧本/人工原文。同一模式重跑会跳过，不重复花额度。
    voice_script_source: str = ""
    #: 被 LLM 改写前的原文（便于人工核对 / 回滚）
    voice_script_original: str = ""
    #: LLM 给的念法建议（rate / volume / pitch），合成配音时优先于全局设置
    voice_direction: VoiceDirection | None = None

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
    #: 是否已按配音时长对齐过（对齐只会拉长镜头，不会裁掉配音）
    audio_aligned: bool = False

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
    "VoiceDirection",
]
