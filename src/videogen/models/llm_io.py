from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.models.script import DialogueLine
from videogen.models.storyboard import ShotDraft
from videogen.models.world_state import WorldStateDelta
from videogen.utils.coerce import coerce_int

__all__ = [
    "ChapterOutlineResponse",
    "ChaptersExpansionResponse",
    "OutlineResponse",
    "SceneResponse",
    "ShotsResponse",
    "StorySpineResponse",
    "VoiceLineDraft",
    "VoiceScriptResponse",
]


def _as_dict_list(value: object) -> object:
    """把 None / 单个 dict / 列表统一成 list[dict]（LLM 输出的常见走形）。"""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return value


class StorySpineResponse(BaseModel):
    """长篇大纲「骨架」阶段的期望输出（只有一句话剧情，很轻）。"""

    title: str = ""
    logline: str = ""
    synopsis: str = ""
    theme: str = ""
    chapters: list[dict] = Field(default_factory=list)

    @field_validator("chapters", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return _as_dict_list(value)


class ChaptersExpansionResponse(BaseModel):
    """长篇大纲「逐章扩写」阶段的期望输出（一次一批章）。"""

    chapters: list[dict] = Field(default_factory=list)

    @field_validator("chapters", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return _as_dict_list(value)


class ChapterOutlineResponse(BaseModel):
    """把选中的章展开成「分场大纲」的期望输出。"""

    scenes: list[dict] = Field(default_factory=list)

    @field_validator("scenes", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return _as_dict_list(value)


class OutlineResponse(BaseModel):
    """`generate_outline` 的期望输出。"""

    scenes: list[dict] = Field(default_factory=list)

    @field_validator("scenes", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return _as_dict_list(value)


class SceneResponse(BaseModel):
    """单场剧本的期望输出（含 world_state_delta）。"""

    heading: str = ""
    summary: str = ""
    dialogue: list[DialogueLine] = Field(default_factory=list)
    duration_sec: int = 10
    emotion: str = ""
    world_state_delta: WorldStateDelta = Field(default_factory=WorldStateDelta)

    @field_validator("dialogue", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, value: object) -> int:
        return max(1, coerce_int(value, 10))


class ShotsResponse(BaseModel):
    """单场分镜的期望输出。"""

    shots: list[ShotDraft] = Field(default_factory=list)

    @field_validator("shots", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value


class VoiceLineDraft(BaseModel):
    """配音文案（LLM 看着画面写/改出来的一句）。

    - `text` 为空字符串表示「这个镜头刻意留白，不要配音」
    - `rate` / `volume` / `pitch` 是念法建议，格式随便写（"+10%"、"稍慢"），
      上层会解析成 edge-tts 认的 `+N%` / `+NHz`，解析不出来就用全局设置
    """

    shot_id: str = ""
    text: str = ""
    rate: str = ""
    volume: str = ""
    pitch: str = ""
    #: 自然语言念法（云端 TTS 用）：情绪/语气/语速/停顿/气息
    instruction: str = ""
    note: str = ""


class VoiceScriptResponse(BaseModel):
    """单场配音文案的期望输出。"""

    lines: list[VoiceLineDraft] = Field(default_factory=list)

    @field_validator("lines", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value
