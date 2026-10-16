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
    #: male / female / 空（未知）：配音选角优先认它，空时才从描述里的性别词推断
    gender: str = ""
    role: str = ""  # 主角/反派/配角
    #: 角色所属国家/地区（cn/jp/kr/west/india/mideast/africa/latam/sea/other）：
    #: 决定参考图往哪国人脸上靠。india 及之后的地区只能显式声明，名字推断不会得出它们。
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

    @field_validator("gender", mode="before")
    @classmethod
    def _coerce_gender(cls, value: object) -> str:
        return normalize_gender(value)

    @field_validator("region", mode="before")
    @classmethod
    def _coerce_region(cls, value: object) -> str:
        # 模型可能写 "中国" / "Chinese" / "CN"，统一归一到内部键；不认识就留空
        return normalize_region(value)


_GENDER_WORDS = {
    "male": "male", "m": "male", "man": "male", "男": "male", "男性": "male",
    "female": "female", "f": "female", "woman": "female", "女": "female", "女性": "female",
}


def normalize_gender(value: object) -> str:
    """把 "男" / "Male" / "女性" 这类写法归一到 male / female，认不出来就留空。"""
    return _GENDER_WORDS.get(str(value or "").strip().lower(), "")


class Setting(BaseModel):
    id: str
    name: str
    aliases: StrList = Field(default_factory=list)
    description: str = ""
    visual_prompt: str = ""
    #: 场景参考图（相对项目目录，如 "refs/loc_1.png"）：视频生成时作为背景参考
    reference_image: str = ""


class Prop(BaseModel):
    """关键道具：推动剧情、会被特写或反复出现的物品（不是场景陈设）。

    有参考图时随镜头一起送进视频模型，保证道具外观跨镜头一致。
    """

    id: str
    name: str
    aliases: StrList = Field(default_factory=list)
    description: str = ""
    visual_prompt: str = ""
    reference_image: str = ""


FACT_STATUSES = ("unverified", "confirmed", "rejected")


class Fact(BaseModel):
    id: str
    content: str
    type: str = "general"  # number / timeline / relationship / general
    #: 科普：出处 —— 参考资料里的原句，或「模型自述」。短剧不用
    source: str = ""
    #: 科普：人工核对状态（unverified / confirmed / rejected）。短剧不用
    status: str = "unverified"

    @field_validator("source", mode="before")
    @classmethod
    def _stringify_source(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: object) -> str:
        # 不认识的值一律当「未核对」：宁可多核一遍，也不能把笔误当成「已确认」
        text = str(value or "").strip().lower()
        return text if text in FACT_STATUSES else "unverified"


class StyleGuide(BaseModel):
    genre: str = ""
    tone: str = ""
    pacing: str = ""
    taboo: StrList = Field(default_factory=list)
    #: 科普：模型按题目推荐的语气 / 插图画风（`explainer_service.TONES` / `ILLUSTRATION_STYLES`
    #: 的 key）。项目上这两项还空着时才采用，用户选过的不动
    explainer_tone: str = ""
    illustration_style: str = ""


class StoryBible(BaseModel):
    #: 整片声明的国家/地区（生成时从 Project.cast_region 带过来；空 = 靠名字推断）。
    #: 这是下游定妆照/视频的**权威依据**：声明过就不再靠启发式猜。
    cast_region: str = ""
    characters: list[Character] = Field(default_factory=list)
    settings: list[Setting] = Field(default_factory=list)
    props: list[Prop] = Field(default_factory=list)
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

    def get_setting(self, setting_id: str) -> Setting | None:
        return next((s for s in self.settings if s.id == setting_id), None)

    def get_prop(self, prop_id: str) -> Prop | None:
        return next((p for p in self.props if p.id == prop_id), None)

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
