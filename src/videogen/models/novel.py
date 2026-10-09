from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.utils.coerce import StrList, coerce_int


class NovelCharacterNote(BaseModel):
    """某一章里抽到的人物资料（只记原文写了的，合并成圣经前不做推断）。"""

    name: str = ""
    aliases: StrList = Field(default_factory=list)
    gender: str = ""
    age: str = ""
    #: 身份 / 与他人的关系
    identity: str = ""
    #: 原文对外貌、衣着、体态的描写（可压缩，不改意思）
    appearance: str = ""

    @field_validator("name", "gender", "age", "identity", "appearance", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value).strip()


class NovelLocationNote(BaseModel):
    name: str = ""
    description: str = ""

    @field_validator("name", "description", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value).strip()


class NovelPropNote(NovelLocationNote):
    """关键道具（只写物品外观，不写剧情用途）。"""


class NovelChapter(BaseModel):
    """小说的一章：原文 + 逐章抽取的结果（抽取是可续跑的，`extracted` 标记做没做过）。"""

    id: str
    title: str = ""
    text: str = ""
    extracted: bool = False
    #: 抽取结果是否已经合并进圣经（合并是增量的，只并没并过的章）
    merged: bool = False
    summary: str = ""
    beats: StrList = Field(default_factory=list)
    hook: str = ""
    #: 这一章拍成短剧建议几场（抽取时估，1-4）
    suggested_scenes: int = 0
    characters: list[NovelCharacterNote] = Field(default_factory=list)
    locations: list[NovelLocationNote] = Field(default_factory=list)
    props: list[NovelPropNote] = Field(default_factory=list)
    facts: StrList = Field(default_factory=list)

    @field_validator("suggested_scenes", mode="before")
    @classmethod
    def _coerce_scenes(cls, value: object) -> int:
        return max(0, min(4, coerce_int(value, 0)))


class NovelSource(BaseModel):
    """小说原文切章后的落盘形态（`novel.json`）。

    `source_sha` 是原文的指纹：项目的 input_text 改了，切章与抽取结果一起作废。
    `title/logline/synopsis/theme` 由合并圣经那一步写入，供章节大纲展示。
    """

    source_sha: str = ""
    source_chars: int = 0
    chapters: list[NovelChapter] = Field(default_factory=list)
    title: str = ""
    logline: str = ""
    synopsis: str = ""
    theme: str = ""

    def get_chapter(self, chapter_id: str) -> NovelChapter | None:
        for chapter in self.chapters:
            if chapter.id == chapter_id:
                return chapter
        return None

    def extracted(self) -> list[NovelChapter]:
        return [c for c in self.chapters if c.extracted]
