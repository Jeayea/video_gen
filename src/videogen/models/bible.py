from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.utils.coerce import StrList, coerce_optional_int
from videogen.utils.region import normalize_region


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
    #: 角色所属国家/地区（cn/jp/kr/west/other）：决定参考图往哪国人脸上靠。
    #: 留空就按**名字**推断（详见 utils/region.py），推不出来就跟整个剧组一致
    region: str = ""
    description: str = ""
    visual_prompt: str = ""
    voice_id: str = ""
    #: 角色一致性：参考图（相对项目目录，如 "refs/char_1.png"）+ 固定 seed
    reference_image: str = ""
    seed: int | None = None
    personality: StrList = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    @field_validator("age", mode="before")
    @classmethod
    def _coerce_age(cls, value: object) -> int | None:
        # LLM 常写 "25岁" / "约30" / ""，不能让它崩掉整个圣经
        return coerce_optional_int(value)

    @field_validator("region", mode="before")
    @classmethod
    def _coerce_region(cls, value: object) -> str:
        # 模型可能写 "中国" / "Chinese" / "CN"，统一归一到内部键；不认识就留空
        return normalize_region(value)


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
    #: 整片声明的国家/地区（生成时从 Project.cast_region 带过来；空 = 靠名字推断）。
    #: 这是下游定妆照/视频的**权威依据**：声明过就不再靠启发式猜。
    cast_region: str = ""
    characters: list[Character] = Field(default_factory=list)
    settings: list[Setting] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    style: StyleGuide = Field(default_factory=StyleGuide)

    @field_validator("cast_region", mode="before")
    @classmethod
    def _coerce_cast_region(cls, value: object) -> str:
        return normalize_region(value)

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
