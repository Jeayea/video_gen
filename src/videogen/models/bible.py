from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.utils.coerce import StrList, coerce_optional_int


class Relationship(BaseModel):
    target: str = ""  # character id
    type: str = ""  # 敌对/恋人/同事/...
    note: str = ""


class Character(BaseModel):
    id: str
    name: str
    aliases: StrList = Field(default_factory=list)
    age: int | None = None
    role: str = ""  # 主角/反派/配角
    description: str = ""
    visual_prompt: str = ""
    voice_id: str = ""
    personality: StrList = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    @field_validator("age", mode="before")
    @classmethod
    def _coerce_age(cls, value: object) -> int | None:
        # LLM 常写 "25岁" / "约30" / ""，不能让它崩掉整个圣经
        return coerce_optional_int(value)


class Setting(BaseModel):
    id: str
    name: str
    description: str = ""
    visual_prompt: str = ""


class Fact(BaseModel):
    id: str
    content: str
    type: str = "general"  # number / timeline / relationship / general


class StyleGuide(BaseModel):
    genre: str = ""
    tone: str = ""
    pacing: str = ""
    taboo: StrList = Field(default_factory=list)


class StoryBible(BaseModel):
    characters: list[Character] = Field(default_factory=list)
    settings: list[Setting] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    style: StyleGuide = Field(default_factory=StyleGuide)

    def get_character(self, char_id: str) -> Character | None:
        for c in self.characters:
            if c.id == char_id:
                return c
        return None

    def get_character_by_name(self, name: str) -> Character | None:
        for c in self.characters:
            if name == c.name or name in c.aliases:
                return c
        return None

    def all_names(self) -> set[str]:
        names: set[str] = set()
        for c in self.characters:
            names.add(c.name)
            names.update(c.aliases)
        return names
