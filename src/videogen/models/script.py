from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.utils.coerce import coerce_int

DEFAULT_SCENE_DURATION_SEC = 10
MIN_SCENE_DURATION_SEC = 3


class DialogueLine(BaseModel):
    speaker: str = ""  # character id
    line: str = ""

    @field_validator("speaker", "line", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value)


class Scene(BaseModel):
    id: str
    heading: str = ""  # e.g. "INT. 办公室 - 日"
    summary: str = ""
    dialogue: list[DialogueLine] = Field(default_factory=list)
    duration_sec: int = DEFAULT_SCENE_DURATION_SEC
    emotion: str = ""

    @field_validator("dialogue", mode="before")
    @classmethod
    def _empty_dialogue(cls, value: object) -> object:
        return [] if value is None else value

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, value: object) -> int:
        return max(MIN_SCENE_DURATION_SEC, coerce_int(value, DEFAULT_SCENE_DURATION_SEC))


class OutlineScene(BaseModel):
    """分场大纲的单场条目（先落盘，支持失败续跑）。"""

    id: str
    title: str = ""
    brief: str = ""
    duration_budget_sec: int = DEFAULT_SCENE_DURATION_SEC


class ScriptOutline(BaseModel):
    scenes: list[OutlineScene] = Field(default_factory=list)
    target_duration_sec: int = 0

    def budget_for(self, scene_id: str) -> int | None:
        for scene in self.scenes:
            if scene.id == scene_id:
                return scene.duration_budget_sec
        return None


class Script(BaseModel):
    title: str = ""
    logline: str = ""
    scenes: list[Scene] = Field(default_factory=list)
    #: 生成时把每场时长按目标归一化过（用于前端提示）
    duration_normalized: bool = False

    @property
    def total_duration_sec(self) -> int:
        return sum(s.duration_sec for s in self.scenes)

    def scene_ids(self) -> set[str]:
        return {s.id for s in self.scenes}
