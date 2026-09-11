from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.models.script import DialogueLine
from videogen.models.storyboard import ShotDraft
from videogen.models.world_state import WorldStateDelta
from videogen.utils.coerce import coerce_int

__all__ = ["OutlineResponse", "SceneResponse", "ShotsResponse"]


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
