"""知识讲解型科普（`content_type=explainer`）。

设计见 docs/EXPLAINER_DESIGN.md：沿用镜头模型，一句旁白一个镜头，每句旁白挂到知识底稿里的事实上。
现有 service 只在入口处按内容类型分派到这里，不在短剧的函数里堆分支。
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import Counter
from collections.abc import Callable

from videogen.config import Settings, get_settings
from videogen.models.bible import Fact, Prop, Setting, StoryBible, StyleGuide
from videogen.models.explainer import (
    ExplainerSource,
    KnowledgeItem,
    KnowledgeResponse,
    SourceChunk,
)
from videogen.models.project import ContentType, InputMode, Project
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.services import bible_service
from videogen.utils.novel import source_fingerprint, split_chapters
from videogen.utils.text import quote_source

logger = logging.getLogger(__name__)

#: 科普配乐比全局 BGM_GAIN_DB 再低多少 dB：旁白是主线，音乐只垫底
MUSIC_GAIN_OFFSET_DB = -6.0
#: 科普默认烧字幕样式：观众常静音看，半透明底条在任何画面上都读得清
SUBTITLE_STYLE = "box"
MUSIC_MOOD = "neutral"


def is_explainer(project: Project | None) -> bool:
    return getattr(project, "content_type", None) == ContentType.EXPLAINER


def apply_creation_defaults(project: Project, settings: Settings | None = None) -> Project:
    """建项目时给科普项目填默认值（用户没填的才填；短剧原样返回）。

    写在项目字段上而不是运行时判断：面板上看得见、改得了，和用户自己设的值走同一条路。
    """
    if not is_explainer(project):
        return project
    settings = settings or get_settings()
    if not project.subtitle_style:
        project.subtitle_style = SUBTITLE_STYLE
    if not project.music_mood:
        project.music_mood = MUSIC_MOOD
    if project.music_gain_db is None:
        project.music_gain_db = max(-60.0, float(settings.bgm_gain_db) + MUSIC_GAIN_OFFSET_DB)
    return project


# ── 知识底稿 ──────────────────────────────────────────

#: 事实出处的固定写法（界面和检查都认这几个前缀）
SOURCE_SELF = "模型自述"
SOURCE_NOT_FOUND = "模型自述（引文未在资料中找到）"
SOURCE_USER_PREFIX = "用户文稿："
#: 引文和资料「近似一致」的门槛：模型摘抄时常改一两个字，严格子串会大量误报
QUOTE_SIMILARITY = 0.9

KNOWLEDGE_SYSTEM = """你是科普知识底稿编辑。为一支 30-120 秒的科普讲解短片整理「知识底稿」：
讲解要用到的事实，以及能把这些事实拍成画面的演示场景。只整理，不写旁白。

输出 JSON：
{
  "facts": [{"content": "一条可核对的事实（一句中文）", "source": "出处原句", "type": "concept"}],
  "settings": [{"name": "演示场景名", "description": "中文：这里能拍到什么",
                "visual_prompt": "English: visible objects, materials, lighting"}],
  "props": [{"name": "演示物", "description": "中文：外观",
             "visual_prompt": "English: the object itself (material, color, shape)"}],
  "style": {"genre": "科普", "tone": "语气", "pacing": "节奏"}
}

要求：
- facts：讲清这个题目必需的事实，8-20 条。type 填 concept（核心概念）/ misconception
  （常见误解，content 写成「常见误解：……；实际上……」）/ number（数字、日期、量级）/ general
- 每条事实只说一件事，能被人逐条核对；拿不准的不要写，不要编数字
- source 按用户消息里的「出处要求」写
- settings 是演示环境（实验室、天空、细胞内部……）2-5 个；props 是需要反复出现、外观要一致的
  演示物 0-3 个，没有就给空数组
- visual_prompt 必须是英文，只写看得见的东西；不要出现公式、数据、标注这类知识性文字
- 不要输出任何 id（由系统分配）
- 只输出 JSON"""


def _source_rule(project: Project, has_material: bool, min_chars: int) -> str:
    if has_material:
        return (
            "## 出处要求\n"
            f"source 必须**逐字**摘抄<原文>里支撑这条事实的那一句（至少 {min_chars} 个字，"
            "不改字、不拼接两处）；资料里找不到依据的事实不要写"
        )
    if project.input_mode in (InputMode.OUTLINE, InputMode.FULL_SCRIPT):
        return (
            "## 出处要求\n"
            "用户文稿里陈述的事实：source 逐字摘抄用户文稿里的那一句；"
            "为讲清楚而必须补充的背景事实：source 留空"
        )
    return "## 出处要求\nsource 一律留空（系统会标成「模型自述」，由人工核对）"


_INPUT_DESC = {
    InputMode.THEME: "题目",
    InputMode.OUTLINE: "用户写好的知识点（大纲）",
    InputMode.FULL_SCRIPT: "用户写好的完整旁白",
}


def _knowledge_prompt(
    project: Project, chunk: str | None, index: int, total: int, min_chars: int
) -> str:
    audience = " ".join(project.audience.split()) or "未指定（按没有专业背景的成年人写）"
    lines = [
        f"## {_INPUT_DESC.get(project.input_mode, '题目')}",
        project.input_text,
        "",
        f"目标观众：{audience}",
        f"目标时长：{project.target_duration_sec} 秒",
    ]
    if chunk is not None:
        part = f"（第 {index}/{total} 块，只从这一块里取事实）" if total > 1 else ""
        lines += ["", f"## 参考资料{part}", quote_source(chunk)]
    lines += ["", _source_rule(project, chunk is not None, min_chars)]
    constraint = bible_service.constraint_block(project)
    if constraint:
        lines += ["", constraint]
    lines += ["", "请整理知识底稿。"]
    return "\n".join(lines)


def source_fingerprint_for(project: Project) -> str:
    """资料 + 题目 + 输入方式：任何一个变了，已抽好的块就不再对应。"""
    key = "\n".join([project.input_mode.value, project.input_text, project.source_material])
    return source_fingerprint(key)


def split_source(text: str, max_chars: int) -> list[str]:
    """参考资料按段落切块（复用小说切章：有章节标题按标题切，超长再按段落拆）。"""
    size = max(500, int(max_chars))
    return [c.text for c in split_chapters(text, chunk_chars=size, max_chars=size)]


async def generate_knowledge_base(
    project: Project,
    llm: LLMProvider,
    *,
    previous: StoryBible | None = None,
    progress: ExplainerSource | None = None,
    on_progress: Callable[[ExplainerSource], None] | None = None,
    settings: Settings | None = None,
) -> StoryBible:
    """生成知识底稿（科普项目的「圣经」）。

    有参考资料时按块抽取、每块抽完就回调落盘；`progress` 的指纹对得上时跳过已抽好的块。
    `previous`（旧底稿）用来保留：内容没变的事实沿用 id 与核对状态，
    同名场景 / 道具沿用 id 与参考图，人工建的讲解人物原样保留。
    """
    settings = settings or get_settings()
    min_chars = settings.explainer_min_quote_chars
    material = project.source_material.strip()
    results: list[KnowledgeResponse] = []
    if material:
        fingerprint = source_fingerprint_for(project)
        if progress is None or progress.fingerprint != fingerprint or not progress.chunks:
            progress = ExplainerSource(
                fingerprint=fingerprint,
                chunks=[
                    SourceChunk(index=i, text=text)
                    for i, text in enumerate(
                        split_source(material, settings.explainer_source_max_chars), start=1
                    )
                ],
            )
        total = len(progress.chunks)
        for chunk in progress.chunks:
            if chunk.result is None:
                chunk.result = await _ask_knowledge(
                    llm, _knowledge_prompt(project, chunk.text, chunk.index, total, min_chars)
                )
                if on_progress is not None:
                    on_progress(progress)
            results.append(chunk.result)
    else:
        results.append(
            await _ask_knowledge(llm, _knowledge_prompt(project, None, 1, 1, min_chars))
        )

    bible = merge_knowledge(results, previous, max_facts=settings.explainer_max_facts)
    verify_sources(bible.facts, material, min_chars=min_chars)
    if not bible.facts:
        raise ValueError("知识底稿没有生成任何事实")
    logger.info(
        "知识底稿：%d 条事实 / %d 个演示场景 / %d 个演示物（资料 %d 块）",
        len(bible.facts),
        len(bible.settings),
        len(bible.props),
        len(results) if material else 0,
    )
    return bible


async def _ask_knowledge(llm: LLMProvider, user_prompt: str) -> KnowledgeResponse:
    messages = [
        LLMMessage(role="system", content=KNOWLEDGE_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]
    return await llm.chat_json(
        messages, schema=KnowledgeResponse, temperature=0.3, max_tokens=4096
    )


def _compact(text: str) -> str:
    return " ".join(str(text or "").split())


def next_fact_number(facts: list[Fact]) -> int:
    """下一个事实编号：比出现过的最大编号大。

    不复用空出来的编号（不用 `novel_service._next_id` 的「找第一个空位」）：重新生成底稿时被丢掉的
    fact_3 如果还被旁白引用，复用这个号会让引用悄悄指向另一条内容，检查也就报不出来了。
    """
    numbers = [0]
    for fact in facts:
        match = re.fullmatch(r"fact_(\d+)", fact.id)
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers) + 1


def merge_knowledge(
    results: list[KnowledgeResponse], previous: StoryBible | None, *, max_facts: int
) -> StoryBible:
    old = previous or StoryBible()
    old_facts = {_compact(f.content): f for f in old.facts}
    used = {f.id for f in old.facts}
    number = next_fact_number(old.facts)
    facts: list[Fact] = []
    seen: set[str] = set()
    for result in results:
        for item in result.facts:
            content = _compact(item.content)
            if not content or content in seen or len(facts) >= max_facts:
                continue
            seen.add(content)
            source = _compact(item.source)
            kept = old_facts.get(content)
            if kept is not None:
                # 内容一字不差：沿用 id；出处也没变才沿用核对状态
                status = kept.status if _compact(kept.source) == source else "unverified"
                facts.append(
                    Fact(id=kept.id, content=content, type=item.type or kept.type,
                         source=source, status=status)
                )
                continue
            while f"fact_{number}" in used:
                number += 1
            fact_id = f"fact_{number}"
            used.add(fact_id)
            number += 1
            facts.append(Fact(id=fact_id, content=content, type=item.type or "general",
                              source=source))

    bible = StoryBible(
        cast_region=old.cast_region,
        characters=list(old.characters),
        facts=facts,
        style=next((r.style for r in results if r.style.genre or r.style.tone), StyleGuide()),
    )
    bible.settings = _merge_items(
        [i for r in results for i in r.settings], old.settings, Setting, "loc"
    )
    bible.props = _merge_items([i for r in results for i in r.props], old.props, Prop, "prop")
    return bible


def _merge_items(items: list[KnowledgeItem], old: list, model: type, prefix: str) -> list:
    """同名的场景 / 道具合并成一条；和旧底稿同名的沿用 id 与参考图（重画要花钱）。"""
    by_name = {_compact(x.name): x for x in old}
    used = {x.id for x in old}
    merged: list = []
    seen: set[str] = set()
    for item in items:
        name = _compact(item.name)
        if not name or name in seen:
            continue
        seen.add(name)
        kept = by_name.get(name)
        if kept is not None:
            merged.append(
                kept.model_copy(
                    update={
                        "description": item.description or kept.description,
                        "visual_prompt": item.visual_prompt or kept.visual_prompt,
                    }
                )
            )
            continue
        n = 1
        while f"{prefix}_{n}" in used:
            n += 1
        used.add(f"{prefix}_{n}")
        merged.append(
            model(id=f"{prefix}_{n}", name=name, description=item.description,
                  visual_prompt=item.visual_prompt)
        )
    return merged


def reset_changed_facts(old: StoryBible | None, new: StoryBible) -> list[str]:
    """人工改了事实的内容或出处：核对状态退回 unverified（确认的是旧内容，不是新内容）。"""
    before = {f.id: f for f in (old.facts if old else [])}
    reset: list[str] = []
    for fact in new.facts:
        prior = before.get(fact.id)
        changed = prior is None or (
            _compact(prior.content) != _compact(fact.content)
            or _compact(prior.source) != _compact(fact.source)
        )
        if changed and fact.status != "unverified":
            fact.status = "unverified"
            reset.append(fact.id)
    return reset


# ── 出处校验（确定性，$0）────────────────────────────


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """去掉空白和标点（只留字母、数字、汉字），并记下每个字在原文里的位置。"""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isalnum():
            chars.append(ch.lower())
            index.append(i)
    return "".join(chars), index


def match_quote(quote: str, material: str, *, min_chars: int) -> tuple[str, str]:
    """引文能不能在资料里找到。返回 `(状态, 资料里的真实原句)`。

    状态：`ok` = 找到（子串或相似度 ≥ 0.9）/ `short` = 引文太短不算数 / `missing` = 找不到。
    找到时返回的是**资料里的原句**（模型改过的字以资料为准）。
    """
    norm_quote, _ = _normalize_with_map(quote)
    if len(norm_quote) < min_chars:
        return "short", quote
    norm_material, index = _normalize_with_map(material)
    size = len(norm_quote)
    pos = norm_material.find(norm_quote)
    if pos >= 0:
        return "ok", material[index[pos] : index[pos + size - 1] + 1]

    # 近似匹配：用引文里的二元组在资料里投票找候选起点，只对票数最多的几十个位置算相似度
    votes: Counter[int] = Counter()
    step = max(1, size // 12)
    for k in range(0, size - 1, step):
        bigram = norm_quote[k : k + 2]
        start, hits = 0, 0
        while hits < 300 and (found := norm_material.find(bigram, start)) != -1:
            votes[found - k] += 1
            start, hits = found + 1, hits + 1
    best, best_span = 0.0, (0, 0)
    for begin, _count in votes.most_common(40):
        for length in range(size - 2, size + 3):
            lo = max(0, begin)
            window = norm_material[lo : lo + length]
            if len(window) < min_chars:
                continue
            matcher = difflib.SequenceMatcher(None, norm_quote, window, autojunk=False)
            if matcher.quick_ratio() < QUOTE_SIMILARITY:
                continue
            ratio = matcher.ratio()
            if ratio > best:
                best, best_span = ratio, (lo, lo + len(window))
    if best >= QUOTE_SIMILARITY:
        lo, hi = best_span
        return "ok", material[index[lo] : index[hi - 1] + 1]
    return "missing", quote


def verify_sources(facts: list[Fact], material: str, *, min_chars: int) -> None:
    """生成后统一收尾每条事实的出处。

    - 没有资料：空出处标成「模型自述」
    - 有资料：引文找到了就换成资料里的真实原句；找不到说明模型编了「原句」，
      标成「模型自述（引文未在资料中找到）」；引文太短的原样留着，检查时报「引文过短」
    通过校验的事实仍然是 unverified：引文是真的，不等于结论没被歪曲。
    """
    material = material.strip()
    for fact in facts:
        source = _compact(fact.source)
        if not material:
            fact.source = source or SOURCE_SELF
            continue
        if not source or source.startswith(SOURCE_SELF) or source.startswith(SOURCE_USER_PREFIX):
            fact.source = source if source.startswith(SOURCE_SELF) else SOURCE_NOT_FOUND
            continue
        state, real = match_quote(source, material, min_chars=min_chars)
        if state == "ok":
            fact.source = real
        elif state == "missing":
            fact.source = SOURCE_NOT_FOUND
        else:
            fact.source = source


def source_state(fact: Fact, material: str, *, min_chars: int) -> str:
    """检查用：`none`（没有资料，不查）/ `ok` / `short` / `missing`。"""
    material = material.strip()
    if not material:
        return "none"
    source = _compact(fact.source)
    if not source or source.startswith(SOURCE_SELF) or source.startswith(SOURCE_USER_PREFIX):
        return "missing"
    return match_quote(source, material, min_chars=min_chars)[0]
