"""小说改编（input_mode=novel）：切章 → 按需逐章抽取 → 增量合并进圣经 → 章节即大纲。

和「主题/大纲」模式的区别：
- **不原创大纲**：小说的章就是大纲，`StoryOutline` 由切章 + 抽取结果确定性地拼出来（$0）
- **圣经是抽出来的，不是编出来的**：逐章抽人物/场景/关键道具，再合并成剧集资产
- **增量**：只抽要拍的章；后抽的章合并进已有圣经时**已有条目 id 不变**
  （定妆照、场景图、音色都挂在 id 上，id 一变就全作废）

合并分两层：名字/别名能对上的直接确定性合并（不花钱）；对不上的候选才交给 LLM
判断「是不是已有角色的另一个称呼 / 是不是值得进圣经的新角色」。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from videogen.models.bible import (
    Character,
    Fact,
    Prop,
    Setting,
    StoryBible,
    StyleGuide,
    normalize_gender,
)
from videogen.models.novel import (
    NovelChapter,
    NovelCharacterNote,
    NovelLocationNote,
    NovelPropNote,
    NovelSource,
)
from videogen.models.project import Project
from videogen.models.script import (
    DEFAULT_CHAPTER_SCENES,
    MIN_SCENE_DURATION_SEC,
    StoryChapter,
    StoryOutline,
)
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.services import bible_service
from videogen.utils.coerce import coerce_int, coerce_optional_int, ensure_str_list
from videogen.utils.ids import allocate_id
from videogen.utils.novel import source_fingerprint, split_chapters
from videogen.utils.text import quote_source

logger = logging.getLogger(__name__)

__all__ = [
    "build_story_outline",
    "extract_chapters",
    "merge_into_bible",
    "prepare_novel",
]

#: 按章展开时，每场预估多少秒（与长篇大纲的口径一致）
SECONDS_PER_SCENE = 12
#: 圣经里最多收多少个场景 / 道具 / 事实（防止长篇越抽越多把提示词挤爆）
MAX_SETTINGS = 20
MAX_PROPS = 15
MAX_FACTS = 40

NOVEL_EXTRACT_SYSTEM = """你是小说改编的资料员。阅读给定的一章小说原文，
只做「抽取」，不创作、不评价。

硬要求：
1. 只写原文里有的信息；原文没写的外貌/年龄留空，不要编
2. 人物用原文里的名字；「已知角色」里已有的人沿用同一个名字；同一个人的其它称呼
   （外号/尊称/昵称/职务称呼）放进 aliases
3. appearance 摘录原文对外貌、衣着、体态的描写（可压缩，不改意思）；没写就留空
4. summary 150-300 字，按发生顺序写清这一章的事件；beats 3-8 条按顺序的具体情节点（动作+对象）
5. hook 写本章结尾留下的悬念（没有就留空）
6. suggested_scenes：这一章拍成短剧需要几场（1-4），按关键事件数估
7. locations 只收真实发生戏份的地点（房间/街道/建筑这一级），床、沙发、镜子这类家具陈设不算地点；
   description 摘录原文对环境的描写
8. props 只收**推动剧情**的关键道具（信物/凶器/证据/关键文件…），最多 {max_props} 个；
   手机、水杯这类随手物品和桌椅陈设都不算，没有就给空数组。description 只写物品外观
9. facts 只收对后续剧情有约束力的数字/时间/关系事实

只输出 JSON：
{{"summary": "...", "beats": ["..."], "hook": "...", "suggested_scenes": 3,
 "characters": [{{"name": "...", "aliases": ["..."], "gender": "男/女/未知", "age": "",
                 "identity": "身份/与他人的关系", "appearance": "..."}}],
 "locations": [{{"name": "...", "description": "..."}}],
 "props": [{{"name": "...", "description": "..."}}],
 "facts": ["..."]}}"""

NOVEL_MERGE_SYSTEM = """你是小说影视化改编的资产统筹。
把「新抽到的候选」并进已有的剧集资产（故事圣经）。

对每个候选只有三种处理：
- **是已有条目的另一个称呼**：same_as 填已有 id（例如候选「林总」就是已有的 char_1 林晚）
- **值得进圣经的新条目**：same_as 留空，补全字段
- **不收**：只出现一次、对剧情无影响的路人/过场地点/随手物品，直接不输出

硬要求：
1. from 列出这个条目合并了哪些候选编号（C1/L2/P3…）；同一个人的多个候选合并成一个条目
2. 新角色的 name 用原文最常用的正名，其余称呼进 aliases；新角色最多再加 {room} 个，按戏份排序
3. visual_prompt 必须是英文，只描述外貌/服装/气质（场景写环境、道具写物品本身），
   依据候选资料里的原文描写；原文没写的部分按身份/年龄/年代补一个合理、普通的外观；
   画面里要出现的文字（黑板 / 招牌 / 横幅 / 屏幕）保留原文、用引号括起来，不要翻译
4. 角色的 region 填 cn/jp/kr/west/india/mideast/africa/latam/sea/other，按**原著故事发生的
   国家**填：翻译小说里的「格里高·萨姆莎」「伊丽莎白」是欧美人（west），《一千零一夜》是 mideast，
   不要因为名字被译成中文就填 cn；
   visual_prompt 的长相与 region 一致；other 只用于非人类或确实无法判断
5. age 必须是整数：原文写了就用原文；没写就按身份、人物关系与年代**推断**一个合理的整数
   （父母 ≈ 子女年龄 + 25~30；在职的成年人至少 20 多岁；学生按学段），
   并与 visual_prompt 里的年龄段一致；只有毫无线索时才给 null
6. gender 填 male / female（候选资料里的性别、「父亲/母亲/哥哥/妹妹」这类称谓都是依据），
   实在判断不了留空
7. style 只在「已有圣经」没有类型/基调时填写（genre/tone/pacing），否则给空对象

只输出 JSON：
{{"characters": [{{"from": ["C1"], "same_as": "", "name": "...", "aliases": [], "age": 25,
   "gender": "male", "role": "主角/配角/反派", "region": "cn", "description": "中文",
   "visual_prompt": "English", "personality": ["..."]}}],
 "settings": [{{"from": ["L1"], "same_as": "", "name": "...", "description": "中文",
   "visual_prompt": "English"}}],
 "props": [{{"from": ["P1"], "same_as": "", "name": "...", "description": "中文外观",
   "visual_prompt": "English: the object itself"}}],
 "style": {{"genre": "", "tone": "", "pacing": ""}}}}"""


def _dict_list(value: object) -> object:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return value


class ChapterExtractResponse(BaseModel):
    summary: str = ""
    beats: list = Field(default_factory=list)
    hook: str = ""
    suggested_scenes: object = 0
    characters: list[dict] = Field(default_factory=list)
    locations: list[dict] = Field(default_factory=list)
    props: list[dict] = Field(default_factory=list)
    facts: list = Field(default_factory=list)

    @field_validator("characters", "locations", "props", mode="before")
    @classmethod
    def _as_dicts(cls, value: object) -> object:
        return _dict_list(value)

    @field_validator("summary", "hook", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return "" if value is None else str(value)


class MergeResponse(BaseModel):
    characters: list[dict] = Field(default_factory=list)
    settings: list[dict] = Field(default_factory=list)
    props: list[dict] = Field(default_factory=list)
    style: dict = Field(default_factory=dict)

    @field_validator("characters", "settings", "props", mode="before")
    @classmethod
    def _as_dicts(cls, value: object) -> object:
        return _dict_list(value)

    @field_validator("style", mode="before")
    @classmethod
    def _as_dict(cls, value: object) -> object:
        return value if isinstance(value, dict) else {}


# ── 切章 ──────────────────────────────────────────────


def prepare_novel(
    project: Project,
    existing: NovelSource | None,
    *,
    chunk_chars: int,
    max_chars: int,
) -> tuple[NovelSource, bool]:
    """切章。原文没变就沿用已有结果（含已花钱的抽取）；返回 (novel, 是否重新切过)。"""
    sha = source_fingerprint(project.input_text or "")
    if existing is not None and existing.source_sha == sha and existing.chapters:
        return existing, False
    raw = split_chapters(project.input_text or "", chunk_chars=chunk_chars, max_chars=max_chars)
    if not raw:
        raise ValueError("小说原文为空，无法切章")
    novel = NovelSource(
        source_sha=sha,
        source_chars=len(project.input_text or ""),
        chapters=[
            NovelChapter(id=f"ch_{i}", title=item.title, text=item.text)
            for i, item in enumerate(raw, start=1)
        ],
    )
    logger.info("小说切章 %s：%d 字 -> %d 章", project.id, novel.source_chars, len(novel.chapters))
    return novel, True


# ── 逐章抽取 ──────────────────────────────────────────


def _roster(bible: StoryBible | None) -> str:
    if bible is None or not bible.characters:
        return "（暂无）"
    return "、".join(
        f"{c.name}（{'/'.join(c.aliases)}）" if c.aliases else c.name for c in bible.characters
    )


async def _extract_one(
    llm: LLMProvider,
    chapter: NovelChapter,
    bible: StoryBible | None,
    *,
    max_props: int,
) -> None:
    user_prompt = f"""章节 ID: {chapter.id}
章节标题: {chapter.title}
已知角色（同一人请沿用这些名字）: {_roster(bible)}

## 原文
{quote_source(chapter.text)}"""
    messages = [
        LLMMessage(role="system", content=NOVEL_EXTRACT_SYSTEM.format(max_props=max_props)),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(
        messages, schema=ChapterExtractResponse, temperature=0.2, max_tokens=4096
    )
    chapter.summary = response.summary.strip()
    chapter.beats = [b for b in ensure_str_list(response.beats) if b.strip()]
    chapter.hook = response.hook.strip()
    chapter.suggested_scenes = max(0, min(4, coerce_int(response.suggested_scenes, 0)))
    chapter.characters = [
        note
        for note in (NovelCharacterNote.model_validate(item) for item in response.characters)
        if note.name
    ]
    chapter.locations = [
        note
        for note in (NovelLocationNote.model_validate(item) for item in response.locations)
        if note.name
    ]
    chapter.props = [
        note
        for note in (NovelPropNote.model_validate(item) for item in response.props)
        if note.name
    ][: max(0, max_props)]
    chapter.facts = [f for f in ensure_str_list(response.facts) if f.strip()]
    chapter.extracted = True


async def extract_chapters(
    llm: LLMProvider,
    novel: NovelSource,
    chapter_ids: list[str],
    *,
    bible: StoryBible | None = None,
    concurrency: int = 4,
    max_props: int = 3,
    on_progress: Callable[[NovelSource], None] | None = None,
) -> list[str]:
    """抽取指定章（已抽过的跳过），返回本次新抽的章 id。

    每抽完一章就回调落盘：中途失败时已经花钱抽好的章不会丢；失败的章汇总后抛错。
    """
    wanted = set(chapter_ids)
    todo = [c for c in novel.chapters if c.id in wanted and not c.extracted]
    if not todo:
        return []
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(chapter: NovelChapter) -> None:
        async with semaphore:
            await _extract_one(llm, chapter, bible, max_props=max_props)
        if on_progress:
            on_progress(novel)

    results = await asyncio.gather(*(run(c) for c in todo), return_exceptions=True)
    failed = [c.id for c, r in zip(todo, results, strict=True) if isinstance(r, BaseException)]
    if failed:
        first = next(r for r in results if isinstance(r, BaseException))
        raise ValueError(f"章节抽取失败：{', '.join(failed)}（{first}）") from first
    logger.info("小说抽取完成：%d 章", len(todo))
    return [c.id for c in todo]


# ── 合并进圣经（增量，已有 id 不变）────────────────────


def normalize_name(name: str) -> str:
    """「林小雨（主角）」与「林小雨」视为同一个名字：去括号注释、去空白、小写。"""
    text = re.sub(r"[（(][^）)]*[）)]", "", str(name or ""))
    return re.sub(r"\s+", "", text).lower()


@dataclass
class _Candidate:
    tag: str
    name: str
    aliases: list[str] = field(default_factory=list)
    chapters: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def names(self) -> list[str]:
        return [self.name, *self.aliases]

    def add(self, chapter_id: str, names: list[str], note: str) -> None:
        for name in names:
            if name and name != self.name and name not in self.aliases:
                self.aliases.append(name)
        if chapter_id not in self.chapters:
            self.chapters.append(chapter_id)
        note = " ".join(note.split())
        if note and note not in self.notes and len(self.notes) < 4:
            self.notes.append(note[:160])


def _collect(prefix: str, rows: list[tuple[str, list[str], str]]) -> list[_Candidate]:
    """把多章的同类条目按名字/别名聚成候选（同名或互为别名 = 同一个）。"""
    candidates: list[_Candidate] = []
    index: dict[str, _Candidate] = {}
    for chapter_id, names, note in rows:
        keys = [normalize_name(n) for n in names if normalize_name(n)]
        if not keys:
            continue
        found = next((index[k] for k in keys if k in index), None)
        if found is None:
            found = _Candidate(tag=f"{prefix}{len(candidates) + 1}", name=names[0])
            candidates.append(found)
        found.add(chapter_id, names, note)
        for key in (normalize_name(n) for n in found.names()):
            index.setdefault(key, found)
    return candidates


def _bible_index(items: list) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in items:
        for name in [item.name, *getattr(item, "aliases", [])]:
            key = normalize_name(name)
            if key:
                index.setdefault(key, item.id)
    return index


def _match(candidate: _Candidate, index: dict[str, str]) -> str:
    return next((index[k] for k in map(normalize_name, candidate.names()) if k in index), "")


def _add_aliases(item, names: list[str]) -> None:
    for name in names:
        name = str(name or "").strip()
        if name and name != item.name and name not in item.aliases:
            item.aliases.append(name)


def _next_id(prefix: str, items: list) -> str:
    return allocate_id(prefix, (item.id for item in items))


def _fmt_existing(bible: StoryBible) -> str:
    lines = ["角色:"]
    lines += [
        f"  - {c.id} {c.name}（别名: {'/'.join(c.aliases) or '无'}；"
        f"{c.role}；{c.description[:40]}）"
        for c in bible.characters
    ] or ["  （无）"]
    lines.append("场景:")
    lines += [f"  - {s.id} {s.name}" for s in bible.settings] or ["  （无）"]
    lines.append("道具:")
    lines += [f"  - {p.id} {p.name}" for p in bible.props] or ["  （无）"]
    return "\n".join(lines)


def _fmt_candidates(title: str, candidates: list[_Candidate]) -> str:
    if not candidates:
        return f"{title}: （无）"
    lines = [f"{title}:"]
    for c in candidates:
        alias = f"；别称: {'/'.join(c.aliases)}" if c.aliases else ""
        notes = "；".join(c.notes) or "原文无描写"
        lines.append(f"  - [{c.tag}] {c.name}{alias}；出场 {len(c.chapters)} 章；资料: {notes}")
    return "\n".join(lines)


def _resolve(
    entries: list[dict], candidates: list[_Candidate]
) -> list[tuple[dict, list[_Candidate]]]:
    """把 LLM 返回的条目对回候选（优先认 from 编号，没给就按名字对）。"""
    by_tag = {c.tag: c for c in candidates}
    by_name = {normalize_name(n): c for c in candidates for n in c.names()}
    resolved: list[tuple[dict, list[_Candidate]]] = []
    for entry in entries:
        sources = [by_tag[t] for t in ensure_str_list(entry.get("from")) if t in by_tag]
        if not sources:
            hit = by_name.get(normalize_name(str(entry.get("name") or "")))
            sources = [hit] if hit else []
        resolved.append((entry, sources))
    return resolved


async def merge_into_bible(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible | None,
    novel: NovelSource,
    chapter_ids: list[str],
    *,
    max_characters: int = 12,
) -> StoryBible:
    """把指定章的抽取结果合并进圣经，返回新圣经（已有条目的 id / 参考图 / 音色都不动）。"""
    bible = bible.model_copy(deep=True) if bible is not None else StoryBible()
    chapters = [c for c in novel.chapters if c.id in set(chapter_ids) and c.extracted]

    chars = _collect(
        "C",
        [
            (
                ch.id,
                [n.name, *n.aliases],
                "；".join(x for x in (n.gender, n.age, n.identity, n.appearance) if x),
            )
            for ch in chapters
            for n in ch.characters
        ],
    )
    locs = _collect(
        "L", [(ch.id, [n.name], n.description) for ch in chapters for n in ch.locations]
    )
    props = _collect("P", [(ch.id, [n.name], n.description) for ch in chapters for n in ch.props])

    # 第一层：名字/别名能对上已有条目的，确定性合并（只补别名，不花钱）
    pending: dict[str, list[_Candidate]] = {"characters": [], "settings": [], "props": []}
    for kind, candidates, items in (
        ("characters", chars, bible.characters),
        ("settings", locs, bible.settings),
        ("props", props, bible.props),
    ):
        index = _bible_index(items)
        for candidate in candidates:
            hit = _match(candidate, index)
            if hit:
                item = next(i for i in items if i.id == hit)
                _add_aliases(item, candidate.names())
                if isinstance(item, Character) and not item.gender:
                    item.gender = _extracted_gender([candidate])
            else:
                pending[kind].append(candidate)

    # 第二层：对不上的交给 LLM 判断（别称 or 新条目 or 路人）
    needs_style = not (bible.style.genre or bible.style.tone)
    if any(pending.values()) or (needs_style and chapters):
        await _merge_with_llm(llm, project, bible, pending, max_characters=max_characters)

    _merge_facts(bible, chapters)
    bible_service.finalize_bible(bible, project)
    if not bible.characters:
        raise ValueError("没有从原文里抽到任何角色")
    logger.info(
        "圣经合并 %s：%d 章 -> %d 角色 / %d 场景 / %d 道具",
        project.id,
        len(chapters),
        len(bible.characters),
        len(bible.settings),
        len(bible.props),
    )
    return bible


async def _merge_with_llm(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    pending: dict[str, list[_Candidate]],
    *,
    max_characters: int,
) -> None:
    room = max(0, max_characters - len(bible.characters))
    user_prompt = f"""## 已有的剧集资产（id 不可改）
{_fmt_existing(bible)}

## 新抽到的候选
{_fmt_candidates("角色候选", pending["characters"])}
{_fmt_candidates("场景候选", pending["settings"])}
{_fmt_candidates("道具候选", pending["props"])}

已有圣经{"没有" if not (bible.style.genre or bible.style.tone) else "已有"}类型/基调。"""
    constraint = bible_service.constraint_block(project)
    if constraint:
        user_prompt += f"\n\n{constraint}"
    messages = [
        LLMMessage(role="system", content=NOVEL_MERGE_SYSTEM.format(room=room)),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(messages, schema=MergeResponse, temperature=0.3, max_tokens=8192)

    _apply_characters(bible, _resolve(response.characters, pending["characters"]), room)
    settings = _resolve(response.settings, pending["settings"])
    _apply_simple(bible.settings, settings, "loc", Setting, MAX_SETTINGS)
    _apply_simple(bible.props, _resolve(response.props, pending["props"]), "prop", Prop, MAX_PROPS)

    if not (bible.style.genre or bible.style.tone) and response.style:
        style = StyleGuide.model_validate(
            {k: v for k, v in response.style.items() if k in StyleGuide.model_fields}
        )
        bible.style = style


def _apply_characters(
    bible: StoryBible, resolved: list[tuple[dict, list[_Candidate]]], room: int
) -> None:
    added = 0
    for entry, sources in resolved:
        names = [n for c in sources for n in c.names()]
        same_as = str(entry.get("same_as") or "").strip()
        existing = bible.get_character(same_as) if same_as else None
        if existing is not None:
            _add_aliases(existing, names)
            # 只补空着的性别（不改已有条目的其它字段：定妆照/音色都挂在它上面）
            if not existing.gender:
                existing.gender = normalize_gender(entry.get("gender")) or _extracted_gender(
                    sources
                )
            continue
        if added >= room:
            logger.info("角色已满（上限），跳过：%s", entry.get("name"))
            continue
        name = str(entry.get("name") or (sources[0].name if sources else "")).strip()
        if not name:
            continue
        data = {
            k: entry.get(k)
            for k in ("role", "region", "description", "visual_prompt", "personality")
            if entry.get(k) is not None
        }
        character = Character.model_validate(
            {
                **data,
                "id": _next_id("char", bible.characters),
                "name": name,
                "age": coerce_optional_int(entry.get("age")),
                "gender": normalize_gender(entry.get("gender")) or _extracted_gender(sources),
            }
        )
        _add_aliases(character, [*ensure_str_list(entry.get("aliases")), *names])
        bible.characters.append(character)
        added += 1


def _extracted_gender(sources: list[_Candidate]) -> str:
    """LLM 没给性别时，用逐章抽取到的性别（候选资料的第一项）兜底；各章说法不一就留空。"""
    found = {
        gender
        for c in sources
        for note in c.notes
        if (gender := normalize_gender(note.split("；", 1)[0]))
    }
    return found.pop() if len(found) == 1 else ""


def _apply_simple(
    items: list,
    resolved: list[tuple[dict, list[_Candidate]]],
    prefix: str,
    model: type[Setting] | type[Prop],
    cap: int,
) -> None:
    for entry, sources in resolved:
        names = [n for c in sources for n in c.names()]
        same_as = str(entry.get("same_as") or "").strip()
        existing = next((i for i in items if i.id == same_as), None) if same_as else None
        if existing is not None:
            _add_aliases(existing, names)
            continue
        if len(items) >= cap:
            continue
        name = str(entry.get("name") or (sources[0].name if sources else "")).strip()
        if not name:
            continue
        item = model.model_validate(
            {
                "id": _next_id(prefix, items),
                "name": name,
                "description": str(entry.get("description") or ""),
                "visual_prompt": str(entry.get("visual_prompt") or ""),
            }
        )
        _add_aliases(item, names)
        items.append(item)


def _merge_facts(bible: StoryBible, chapters: list[NovelChapter]) -> None:
    known = {f.content for f in bible.facts}
    for chapter in chapters:
        for content in chapter.facts:
            if content in known or len(bible.facts) >= MAX_FACTS:
                continue
            bible.facts.append(Fact(id=_next_id("fact", bible.facts), content=content))
            known.add(content)


# ── 章节即大纲 ────────────────────────────────────────


def _ids(names: list[str], index: dict[str, str]) -> list[str]:
    out: list[str] = []
    for name in names:
        hit = index.get(normalize_name(name))
        if hit and hit not in out:
            out.append(hit)
    return out


def build_story_outline(
    project: Project,
    novel: NovelSource,
    bible: StoryBible,
    existing: StoryOutline | None = None,
) -> StoryOutline:
    """把切好的章拼成长篇大纲（不调 LLM）。

    抽取过的章带正文/情节点/出场资产；没抽取的章只有标题和开头，选中展开时会先抽取。
    已抽取章的场次/时长预算如果被人工改过（旧大纲里有），沿用旧值。
    """
    old = {c.id: c for c in (existing.chapters if existing else [])}
    char_index = _bible_index(bible.characters)
    loc_index = _bible_index(bible.settings)
    prop_index = _bible_index(bible.props)

    outline = StoryOutline(
        title=project.title,
        logline=existing.logline if existing else "",
        synopsis=existing.synopsis if existing else "",
        theme=existing.theme if existing else "",
    )
    for chapter in novel.chapters:
        opening = " ".join(chapter.text.split())[:60]
        item = StoryChapter(id=chapter.id, title=chapter.title, brief=opening)
        if chapter.extracted:
            scenes = chapter.suggested_scenes or DEFAULT_CHAPTER_SCENES
            item.brief = chapter.summary[:60] or opening
            item.summary = chapter.summary
            item.beats = list(chapter.beats)
            item.hook = chapter.hook
            names = [n for c in chapter.characters for n in [c.name, *c.aliases]]
            item.characters = _ids(names, char_index)
            item.locations = _ids([n.name for n in chapter.locations], loc_index)
            item.props = _ids([n.name for n in chapter.props], prop_index)
            item.source_extracted = True
            item.scene_budget = scenes
            item.duration_budget_sec = max(MIN_SCENE_DURATION_SEC, scenes * SECONDS_PER_SCENE)
            previous = old.get(chapter.id)
            if previous is not None and previous.source_extracted:
                item.scene_budget = previous.scene_budget
                item.duration_budget_sec = previous.duration_budget_sec
        outline.chapters.append(item)
    outline.target_duration_sec = sum(
        c.duration_budget_sec for c in outline.chapters if c.source_extracted
    )
    return outline
