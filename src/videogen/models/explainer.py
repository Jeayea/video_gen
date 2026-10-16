"""科普讲解（content_type=explainer）的 LLM 响应与中间产物。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.models.bible import StyleGuide


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
