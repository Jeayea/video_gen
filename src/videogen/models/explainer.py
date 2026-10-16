"""科普讲解（content_type=explainer）的 LLM 响应与中间产物。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.models.bible import StyleGuide
from videogen.utils.coerce import StrList, coerce_int


class KnowledgeFact(BaseModel):
    content: str = ""
    #: 资料里的原句（有资料时）/ 用户文稿里的原句（大纲、完整旁白模式）/ 空 = 模型自述
    source: str = ""
    #: concept / misconception / number / general
    type: str = "general"

    @field_validator("content", "source", "type", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value)


class KnowledgeItem(BaseModel):
    """演示场景 / 演示道具（id 由代码分配，模型给的不认）。"""

    name: str = ""
    description: str = ""
    visual_prompt: str = ""

    @field_validator("name", "description", "visual_prompt", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value)


class KnowledgeResponse(BaseModel):
    """知识底稿一次抽取的结果（整篇一次，或资料的一块）。"""

    facts: list[KnowledgeFact] = Field(default_factory=list)
    settings: list[KnowledgeItem] = Field(default_factory=list)
    props: list[KnowledgeItem] = Field(default_factory=list)
    style: StyleGuide = Field(default_factory=StyleGuide)

    @field_validator("facts", "settings", "props", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return [] if value is None else value


class SourceChunk(BaseModel):
    index: int
    text: str
    result: KnowledgeResponse | None = None


class ExplainerSource(BaseModel):
    """参考资料分块抽取的进度（逐块落盘：中途失败重试时跳过已抽好的块，不重复花钱）。

    底稿生成成功后删掉；`fingerprint` 对不上（资料或题目改了）就整份重来。
    """

    fingerprint: str = ""
    chunks: list[SourceChunk] = Field(default_factory=list)


class NarrationLine(BaseModel):
    line: str = ""
    fact_refs: StrList = Field(default_factory=list)

    @field_validator("line", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value)


class NewFact(KnowledgeFact):
    """旁白阶段模型提出的新事实：`key`（new_1…）只在这一次输出里用来引用，正式 id 由代码分配。"""

    key: str = ""

    @field_validator("key", mode="before")
    @classmethod
    def _stringify_key(cls, value: object) -> str:
        return "" if value is None else str(value)


class ExplainerSceneResponse(BaseModel):
    heading: str = ""
    summary: str = ""
    emotion: str = ""
    lines: list[NarrationLine] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)

    @field_validator("lines", "new_facts", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return [] if value is None else value


class AnnotatedLine(BaseModel):
    index: int = 0
    fact_refs: StrList = Field(default_factory=list)

    @field_validator("index", mode="before")
    @classmethod
    def _coerce_index(cls, value: object) -> int:
        return coerce_int(value, 0)


class AnnotatedScene(BaseModel):
    title: str = ""
    summary: str = ""
    lines: list[AnnotatedLine] = Field(default_factory=list)


class AnnotateResponse(BaseModel):
    """完整旁白模式的反向标注：只分场、只标 fact_refs，不改字。"""

    scenes: list[AnnotatedScene] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)

    @field_validator("scenes", "new_facts", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return [] if value is None else value


class VisualDraft(BaseModel):
    """科普分镜：模型只给画面字段（id / 时长 / 旁白 / fact_refs 由代码按句子定）。"""

    index: int = 0
    camera: str = ""
    visual_description: str = ""
    character_in_frame: StrList = Field(default_factory=list)
    mood: str = ""
    video_prompt: str = ""
    location: str = ""
    props_in_frame: StrList = Field(default_factory=list)

    @field_validator("index", mode="before")
    @classmethod
    def _coerce_index(cls, value: object) -> int:
        return coerce_int(value, 0)

    @field_validator("camera", "visual_description", "mood", "video_prompt", "location",
                     mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value)


class VisualsResponse(BaseModel):
    shots: list[VisualDraft] = Field(default_factory=list)

    @field_validator("shots", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value
