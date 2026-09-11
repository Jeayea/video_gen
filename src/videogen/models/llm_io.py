from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.models.script import DialogueLine
from videogen.models.storyboard import ShotDraft
from videogen.models.world_state import WorldStateDelta
from videogen.utils.coerce import coerce_int

__all__ = [
    "OutlineResponse",
    "SceneResponse",
    "ShotsResponse",
    "VoiceLineDraft",
    "VoiceScriptResponse",
]


class OutlineResponse(BaseModel):
    """`generate_outline` 的期望输出。"""

    scenes: list[dict] = Field(default_factory=list)

    @field_validator("scenes", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value


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
