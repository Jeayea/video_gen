from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.utils.coerce import StrList, coerce_int, coerce_seconds, ensure_str_list

DEFAULT_SCENE_DURATION_SEC = 10
MIN_SCENE_DURATION_SEC = 3

#: 长篇大纲默认章数 / 单章建议场次 / 单场建议时长 / 单批扩写章数
DEFAULT_STORY_CHAPTERS = 20
DEFAULT_CHAPTER_SCENES = 3
DEFAULT_CHAPTER_DURATION_SEC = 36
DEFAULT_CHAPTER_CHUNK = 4


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
        return max(MIN_SCENE_DURATION_SEC, coerce_seconds(value, DEFAULT_SCENE_DURATION_SEC))


class OutlineScene(BaseModel):
    """分场大纲的单场条目（先落盘，支持失败续跑）。"""

    id: str
    title: str = ""
    brief: str = ""
    duration_budget_sec: int = DEFAULT_SCENE_DURATION_SEC
    #: 由长篇大纲的哪一章展开而来（空 = 旧的「一次性分场」路径）
    chapter_id: str = ""
    #: 展开时保留的章级背景/环境线索，供分镜与视频 prompt 使用
    environment: str = ""
    #: 小说改编：这一场依据的原著片段（逐场写剧本时据此保留原著对白与事件）
    source_excerpt: str = ""


class ScriptOutline(BaseModel):
    scenes: list[OutlineScene] = Field(default_factory=list)
    target_duration_sec: int = 0

    def budget_for(self, scene_id: str) -> int | None:
        for scene in self.scenes:
            if scene.id == scene_id:
                return scene.duration_budget_sec
        return None

    def chapter_ids(self) -> set[str]:
        return {s.chapter_id for s in self.scenes if s.chapter_id}


class StoryChapter(BaseModel):
    """长篇剧集大纲里的一章。

    这是「剧情太平淡」的解药：先写完整篇的**因果链**（谁要什么、被什么挡住、
    哪一章翻转），再挑其中几章展开成场次去烧视频额度 —— 单章展开时上下文里
    有全篇弧光，就不会写出「为推进而硬拗」的流水账。
    """

    id: str
    title: str = ""
    #: 一句话剧情（spine 阶段产出，先有骨架）
    brief: str = ""
    #: 300-500 字正文（分块扩写阶段补齐）
    summary: str = ""
    #: 4-8 个情节点（按顺序，含转折）
    beats: StrList = Field(default_factory=list)
    #: 章末钩子：让人想看下一章的那个悬念
    hook: str = ""
    characters: StrList = Field(default_factory=list)
    locations: StrList = Field(default_factory=list)
    #: 本章出现的关键道具（圣经 props 的 id）
    props: StrList = Field(default_factory=list)
    #: 小说模式：这一章有没有从原文抽取过（没抽取的章展开前会先抽取）
    source_extracted: bool = False
    #: 建议展开成几场 / 大约多少秒成片（成本可见）
    scene_budget: int = DEFAULT_CHAPTER_SCENES
    duration_budget_sec: int = DEFAULT_CHAPTER_DURATION_SEC

    @field_validator("beats", "characters", "locations", "props", mode="before")
    @classmethod
    def _coerce_list(cls, value: object) -> object:
        try:
            return ensure_str_list(value)
        except Exception:  # noqa: BLE001 - 脏数据不该炸整个项目
            return []

    @field_validator("scene_budget", "duration_budget_sec", mode="before")
    @classmethod
    def _coerce_num(cls, value: object) -> int:
        return max(1, coerce_int(value, DEFAULT_CHAPTER_SCENES))

    @property
    def expanded_ready(self) -> bool:
        """扩写过的章才有正文/情节点，可直接展开成场。"""
        return bool(self.summary or self.beats)


class StoryOutline(BaseModel):
    """长篇剧集大纲（20+ 章的骨架 + 逐章正文）。"""

    title: str = ""
    logline: str = ""
    synopsis: str = ""
    theme: str = ""
    chapters: list[StoryChapter] = Field(default_factory=list)
    target_duration_sec: int = 0

    def chapter_ids(self) -> set[str]:
        return {c.id for c in self.chapters}

    def get_chapter(self, chapter_id: str) -> StoryChapter | None:
        for chapter in self.chapters:
            if chapter.id == chapter_id:
                return chapter
        return None

    @property
    def total_duration_sec(self) -> int:
        return sum(c.duration_budget_sec for c in self.chapters)

    def expandable(self) -> list[StoryChapter]:
        """已经有正文、可以展开成场的章。"""
        return [c for c in self.chapters if c.expanded_ready]


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
