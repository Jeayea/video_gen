from __future__ import annotations

import logging
import re
from collections.abc import Callable

from pydantic import BaseModel, Field

from videogen.config import get_settings
from videogen.models.bible import StoryBible
from videogen.models.consistency import ConsistencyIssue, IssueSeverity
from videogen.models.llm_io import (
    ChapterOutlineResponse,
    ChaptersExpansionResponse,
    OutlineResponse,
    SceneResponse,
    StorySpineResponse,
)
from videogen.models.project import Project
from videogen.models.script import (
    DEFAULT_CHAPTER_CHUNK,
    DEFAULT_CHAPTER_DURATION_SEC,
    DEFAULT_CHAPTER_SCENES,
    DEFAULT_SCENE_DURATION_SEC,
    DEFAULT_STORY_CHAPTERS,
    MIN_SCENE_DURATION_SEC,
    DialogueLine,
    OutlineScene,
    Scene,
    Script,
    ScriptOutline,
    StoryChapter,
    StoryOutline,
)
from videogen.models.world_state import WorldState
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.utils.coerce import coerce_int, coerce_seconds, ensure_str_list
from videogen.utils.duration import (
    DIALOGUE_SHARE,
    SPEECH_CHARS_PER_SEC,
    apply_duration_budget,
    deviation_ratio,
    dialogue_char_budget,
    scale_durations,
)
from videogen.utils.language import EN_WORDS_PER_SEC
from videogen.utils.text import quote_source

logger = logging.getLogger(__name__)

#: 单场时长偏离目标超过这个比例就整体归一化
DURATION_TOLERANCE = 0.15

SCENE_SYSTEM = """你是一位资深短剧编剧。你正在逐场编写短剧剧本。

严格遵守以下规则：
1. 人名/说话人只能使用「故事圣经」中已有的角色 id，禁止新造角色
2. 数字、金额、日期等必须与「关键事实」一致，如需新数字请在 world_state_delta 中新增
3. 称呼只能使用角色的 name 或已有 aliases
4. 对白要短促有力，符合短剧风格
5. 每场必须有明确的情绪走向
6. dialogue.line 只能是角色说出口的台词，禁止写入「（转身离开）」这类舞台提示/动作描述

输出 JSON 格式：
{
  "heading": "INT./EXT. 地点 - 日/夜",
  "summary": "本场内容一句话概括",
  "dialogue": [{"speaker": "char_id", "line": "台词"}],
  "duration_sec": 数字（秒）,
  "emotion": "本场主情绪",
  "world_state_delta": {
    "current_scene": "scene_N",
    "timeline": "时间线描述",
    "character_states": [{"character_id": "char_1", "location": "loc_1", "physical": "正常",
                          "emotional": "愤怒", "knows": ["..."], "possesses": ["..."],
                          "active_goals": ["..."]}],
    "open_threads": ["未闭合悬念"],
    "established_facts_this_session": ["本场确立的新事实"]
  }
}

只输出 JSON，不要解释文字。"""

_BRACKET_SEG = r"[（(][^）)]{0,60}[）)]"
_LEADING_BRACKETS = re.compile(rf"^(?:\s*{_BRACKET_SEG}\s*)+")
_TRAILING_BRACKETS = re.compile(rf"(?:\s*{_BRACKET_SEG}\s*)+$")
_QUOTE_PAIRS = {'"': '"', "“": "”", "'": "'", "‘": "’"}


def scene_count_for(target_duration_sec: int) -> int:
    """按时长决定场数：每场约 12 秒，限制在 3-6 场。"""
    return min(6, max(3, round(target_duration_sec / 12)))


def clean_dialogue_line(line: str) -> str:
    """清掉台词里的舞台提示和包裹引号，避免 TTS 把括号内容念出来。"""
    text = (line or "").strip()
    text = text.replace("**", "").strip()
    for _ in range(3):
        stripped = _LEADING_BRACKETS.sub("", text)
        stripped = _TRAILING_BRACKETS.sub("", stripped).strip()
        if stripped == text:
            break
        text = stripped
    if len(text) >= 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def _format_bible(bible: StoryBible) -> str:
    parts = ["## 角色"]
    for c in bible.characters:
        parts.append(
            f"- [{c.id}] {c.name} (别名: {', '.join(c.aliases) or '无'}) | {c.role} | {c.age}岁"
        )
        parts.append(f"  描述: {c.description}")
        parts.append(f"  性格: {', '.join(c.personality)}")
        if c.relationships:
            rels = "; ".join(f"→{r.target}: {r.type}({r.note})" for r in c.relationships)
            parts.append(f"  关系: {rels}")
    parts.append("\n## 场景设定")
    for s in bible.settings:
        parts.append(f"- [{s.id}] {s.name}: {s.description}")
        if s.visual_prompt:
            # 环境视觉描述必须往下传：背景不够丰富，根因就是这一层信息在中途丢了
            parts.append(f"  环境视觉: {s.visual_prompt}")
    parts.append("\n## 关键事实")
    for f in bible.facts:
        parts.append(f"- [{f.id}] ({f.type}) {f.content}")
    parts.append("\n## 风格")
    parts.append(
        f"- 类型: {bible.style.genre} | 语气: {bible.style.tone} | 节奏: {bible.style.pacing}"
    )
    if bible.style.taboo:
        parts.append(f"- 禁忌: {', '.join(bible.style.taboo)}")
    return "\n".join(parts)


def _dialogue_budget_line(project: Project, budget: int) -> str:
    """本场台词量上限：中文按字、英文按词（英文项目的提示词里写「字」模型会数错）。"""
    if get_settings().language_for(project) == "en":
        words = int(budget * EN_WORDS_PER_SEC * DIALOGUE_SHARE)
        return (
            f"本场全部台词合计不超过 {words} 个英文单词\n"
            f"（按每秒约 {EN_WORDS_PER_SEC} 个词念，还要给动作和停顿留时间）"
        )
    return (
        f"本场全部台词合计不超过 {dialogue_char_budget(budget)} 字\n"
        f"（按每秒约 {SPEECH_CHARS_PER_SEC} 字念，还要给动作和停顿留时间）"
    )


def _format_prev_scenes(scenes: list[Scene]) -> str:
    if not scenes:
        return "（这是第一场）"
    prev = scenes[-1]
    lines = [f"上一场 [{prev.id}] {prev.heading}", f"摘要: {prev.summary}", f"情绪: {prev.emotion}"]
    if prev.dialogue:
        lines.append("对白要点:")
        for d in prev.dialogue[-3:]:
            lines.append(f"  {d.speaker}: {d.line}")
    return "\n".join(lines)


def _format_world_state(ws: WorldState) -> str:
    if not ws.current_scene and not ws.character_states:
        return "（初始状态，尚无）"
    lines = [f"当前时间线: {ws.timeline}", f"进行到: {ws.current_scene}"]
    for cs in ws.character_states:
        lines.append(f"- {cs.character_id}: 在{cs.location} | {cs.physical} | {cs.emotional}")
        if cs.knows:
            lines.append(f"  已知: {'; '.join(cs.knows)}")
        if cs.active_goals:
            lines.append(f"  目标: {'; '.join(cs.active_goals)}")
    if ws.open_threads:
        lines.append(f"未闭合悬念: {'; '.join(ws.open_threads)}")
    return "\n".join(lines)


# ── 大纲 ─────────────────────────────────────────────


async def generate_outline(llm: LLMProvider, project: Project, bible: StoryBible) -> ScriptOutline:
    """生成分场大纲，并把时长预算按场次均分写进大纲。

    这是**单章/短剧**路径（一次成型、只覆盖 project.target_duration_sec）。
    长篇剧集走 `generate_story_outline` + `expand_chapters`。
    """
    count = scene_count_for(project.target_duration_sec)
    budget = apply_duration_budget(project.target_duration_sec, count, MIN_SCENE_DURATION_SEC)

    user_prompt = f"""根据以下信息，生成 {project.target_duration_sec} 秒短剧的分场大纲。

## 故事圣经
{_format_bible(bible)}

用户输入 ({project.input_mode.value}): {project.input_text}

要求：
- 必须正好 {count} 场
- 全剧总时长 {project.target_duration_sec} 秒，平均每场约 {budget} 秒
- 每场给出: title (一句话标题), brief (2-3句内容概述)
- 节奏快，每场有冲突或反转

输出 JSON: {{"scenes": [{{"title": "...", "brief": "..."}}]}}"""

    messages = [
        LLMMessage(role="system", content="你是短剧大纲策划。只输出 JSON。"),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(messages, schema=OutlineResponse, temperature=0.7)
    raw_scenes = [s for s in response.scenes if isinstance(s, dict)]
    if not raw_scenes:
        raise ValueError("大纲生成失败：LLM 未返回场景列表")
    if len(raw_scenes) > 8:
        raw_scenes = raw_scenes[:8]

    # id 与预算都由系统分配，不信任模型输出
    outline = ScriptOutline(target_duration_sec=project.target_duration_sec)
    for i, item in enumerate(raw_scenes, start=1):
        outline.scenes.append(
            OutlineScene(
                id=f"scene_{i}",
                title=str(item.get("title") or f"第 {i} 场"),
                brief=str(item.get("brief") or item.get("summary") or ""),
                duration_budget_sec=apply_duration_budget(
                    project.target_duration_sec, len(raw_scenes), MIN_SCENE_DURATION_SEC
                ),
            )
        )
    logger.info(
        "Outline generated for %s: %d scenes, %ds/scene budget",
        project.id,
        len(outline.scenes),
        outline.scenes[0].duration_budget_sec,
    )
    return outline


# ── 长篇剧集大纲（20+ 章）─────────────────────────────

STORY_SPINE_SYSTEM = """你是长篇剧集的总编剧。你只写「骨架」：不写台词，不写分场。

硬要求：
1. 只使用给定故事圣经里已有的角色/场景，不允许新增角色
2. 全篇必须是一条**因果链**：每一章都是因为上一章的某个结果才发生
3. 主角必须有明确、可被阻碍的具体目标；阻力要逐章升级（换手段、换代价，不是重复同一种冲突）
4. 每一章都要有**局势变化**（关系翻转 / 信息揭露 / 代价兑现 / 目标转移），
   禁止「又发生了一次类似的挫折」这种平铺
5. 必须交代钩子（开场悬念）与收束（结局与代价）；不要写成流水账式日常
6. 章与章之间情绪节奏要起伏：高压章之后给一个喘息章，再推向更大的危机

只输出 JSON：
{"title": "剧名", "logline": "一句话卖点（≤50字）", "synopsis": "全篇梗概 300-500 字，写清起承转合",
 "theme": "主题（≤20字）",
 "chapters": [{"title": "章标题", "brief": "这一章发生了什么（30-60字，含转折）"}]}"""

CHAPTER_EXPAND_SYSTEM = """你是长篇剧集的总编剧。现在把已经定稿的**章节骨架**扩写成正文级内容。

硬要求：
1. 不得改变骨架里已定的事件顺序与因果关系；只能补充细节
2. summary 要写清「谁想要什么 → 被什么挡住 → 做了什么选择 → 结果与代价」，
   不要写「她很努力，最后成功了」这类没有具体动作的概括
3. beats 是 4-8 个**按顺序**的情节点，每个都要有具体动作/对象（例如「她把名片推了回去」），
   不要写抽象情绪（例如「她很难过」）
4. hook 是章末钩子：一个让观众必须看下一章的具体悬念
5. characters / locations 只能填故事圣经里已有的 id
6. scene_budget 按本章信息量给 2-4 场；duration_budget_sec 按每场约 12 秒估算

只输出 JSON：
{"chapters": [{"id": "ch_1", "title": "...", "summary": "300-500字", "beats": ["...", "..."],
  "hook": "...", "characters": ["char_1"], "locations": ["loc_1"],
  "scene_budget": 3, "duration_budget_sec": 36}]}"""

CHAPTER_SCENE_SYSTEM = """你是分场编剧。把指定的**章**展开成可直接拍摄的分场大纲（不写台词）。

每一场必须包含：
1. 具体地点（用故事圣经里的 loc id 对应的场景）+ 该场环境里的**可见细节**
   （陈设、道具、光线、天气、人群），不要只写「在办公室」
2. 人物的具体动作与目标（他要拿到/阻止/证明什么）
3. 冲突或信息变化（这场结束时局势和开场不一样）
4. 环境细节要服务情绪（例如冷白顶灯下的空会议室）

禁止：把一章写成多场同质重复；禁止没有冲突的过场；禁止一句概述带过整章

只输出 JSON：
{"scenes": [{"title": "场标题",
  "brief": "3-5句：地点+环境细节 / 谁要什么 / 阻力是什么 / 这场结束时局势怎么变了",
  "environment": "这场环境的关键视觉元素（给分镜与视频模型用，写成名词短语）",
  "duration_sec": 12}]}"""


NOVEL_CHAPTER_SCENE_SYSTEM = """你是原著改编的分场编剧。
把指定的**章**按原著改编成可直接拍摄的分场大纲（不写台词）。

硬要求：
1. 忠于原著：事件顺序、因果、人物动机都以原文为准；不新增原著没有的情节与人物
2. 篇幅有限时做「取舍与压缩」：保留推动剧情的关键事件与冲突，删掉旁支、重复与大段心理描写；
   相邻的小事件可以合并成一场
3. 心理活动/叙述性信息改写成可拍的动作、表情或少量对白，不要写「她心想……」
4. 每场写清：地点（用故事圣经里的 loc id 对应的场景）+ 可见环境细节、谁要什么、
   这场结束时局势怎么变了
5. source_excerpt：从原文里**原样摘录**这一场依据的段落（200-600 字，尽量包含这场的原文对白）

只输出 JSON：
{"scenes": [{"chapter_id": "ch_1", "title": "场标题",
  "brief": "3-5句：地点+环境细节 / 谁要什么 / 阻力是什么 / 这场结束时局势怎么变了",
  "environment": "这场环境的关键视觉元素（名词短语）",
  "source_excerpt": "原文摘录", "duration_sec": 12}]}"""

#: 单场带进剧本提示词的原著片段上限（摘录是改编依据，不是全文）
SOURCE_EXCERPT_MAX_CHARS = 1500


def _format_chapter_index(outline: StoryOutline) -> str:
    lines = []
    for chapter in outline.chapters:
        mark = "（已扩写）" if chapter.expanded_ready else ""
        lines.append(f"- [{chapter.id}] {chapter.title}：{chapter.brief}{mark}")
    return "\n".join(lines) or "（无）"


async def generate_story_outline(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    *,
    chapters: int = DEFAULT_STORY_CHAPTERS,
    chunk_size: int = DEFAULT_CHAPTER_CHUNK,
    existing: StoryOutline | None = None,
    force_spine: bool = False,
    on_progress: Callable[[StoryOutline], None] | None = None,
) -> StoryOutline:
    """生成长篇剧集大纲：先出骨架，再分批扩写正文（每批落盘、可中断续跑）。

    分两阶段的原因：20 章一次写完必然被输出长度截断（模型会开始糊弄），
    所以骨架一次给全（保证因果链完整），正文按 3-4 章一批写细。
    """
    outline = existing or StoryOutline()
    if not outline.chapters or force_spine:
        spine = await _generate_story_spine(llm, project, bible, chapters=chapters)
        # 重出骨架时保留「章 id + 标题」都还对得上的旧正文，其余丢弃
        keep = {c.id: c for c in outline.chapters}
        outline.title = spine.title
        outline.logline = spine.logline
        outline.synopsis = spine.synopsis
        outline.theme = spine.theme
        outline.chapters = []
        for chapter in spine.chapters:
            old = keep.get(chapter.id)
            if old is not None and old.title == chapter.title and old.expanded_ready:
                chapter.summary = old.summary
                chapter.beats = list(old.beats)
                chapter.hook = old.hook
                chapter.characters = list(old.characters)
                chapter.locations = list(old.locations)
                chapter.scene_budget = old.scene_budget
                chapter.duration_budget_sec = old.duration_budget_sec
            outline.chapters.append(chapter)
        if on_progress:
            on_progress(outline)

    pending = [c for c in outline.chapters if not c.expanded_ready]
    step = max(1, chunk_size)
    for start in range(0, len(pending), step):
        batch = pending[start : start + step]
        await _expand_chapters(llm, project, bible, outline, {c.id for c in batch})
        if on_progress:
            on_progress(outline)
        logger.info(
            "长篇大纲扩写 %s：%d/%d 章",
            project.id,
            sum(1 for c in outline.chapters if c.expanded_ready),
            len(outline.chapters),
        )
    outline.target_duration_sec = outline.total_duration_sec
    return outline


async def _generate_story_spine(
    llm: LLMProvider, project: Project, bible: StoryBible, *, chapters: int
) -> StoryOutline:
    user_prompt = f"""请为下面这个项目写 {chapters} 章的长篇剧集骨架。

## 故事圣经
{_format_bible(bible)}

## 用户输入（{project.input_mode.value}）
{project.input_text}

## 篇幅约束
- 共 {chapters} 章，每章一句话剧情（brief）
- 分幕：前 1/4 建置与钩子，中段 1/2 升级与翻转，最后 1/4 高潮与收束
- 每章都要有局势变化，至少 5 章要有**大翻转**（身份/关系/信息/代价层面的不可逆变化）

只输出 JSON。"""
    messages = [
        LLMMessage(role="system", content=STORY_SPINE_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(
        messages, schema=StorySpineResponse, temperature=0.8, max_tokens=4096
    )
    raw = [c for c in response.chapters if isinstance(c, dict)]
    if not raw:
        raise ValueError("长篇大纲生成失败：LLM 未返回章节列表")

    outline = StoryOutline(
        title=str(response.title or project.title or ""),
        logline=str(response.logline or ""),
        synopsis=str(response.synopsis or ""),
        theme=str(response.theme or ""),
    )
    for i, item in enumerate(raw, start=1):
        outline.chapters.append(
            StoryChapter(
                id=f"ch_{i}",
                title=str(item.get("title") or f"第 {i} 章"),
                brief=str(item.get("brief") or item.get("summary") or ""),
            )
        )
    logger.info("长篇骨架完成 %s：%d 章", project.id, len(outline.chapters))
    return outline


async def _expand_chapters(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    outline: StoryOutline,
    chapter_ids: set[str],
) -> None:
    """把一批章扩写成正文（summary/beats/hook/characters/locations/预算）。"""
    targets = [c for c in outline.chapters if c.id in chapter_ids]
    if not targets:
        return
    index = [f"- [{c.id}] {c.title}：{c.brief}" for c in targets]
    user_prompt = f"""## 故事圣经
{_format_bible(bible)}

## 全篇
标题: {outline.title}
一句话卖点: {outline.logline}
梗概: {outline.synopsis}
主题: {outline.theme}

## 全篇章节骨架（作为上下文，不要改写）
{_format_chapter_index(outline)}

## 本批要扩写的章
{chr(10).join(index)}

请只输出本批 {len(targets)} 章的正文 JSON，id 必须与上面给出的 id 完全一致。"""
    messages = [
        LLMMessage(role="system", content=CHAPTER_EXPAND_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(
        messages, schema=ChaptersExpansionResponse, temperature=0.7, max_tokens=8192
    )
    by_id: dict[str, dict] = {}
    for item in response.chapters:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "").strip()
        if key:
            by_id[key] = item
    # 模型偶尔漏 id：按顺序兜底认领
    missing = [c for c in targets if c.id not in by_id]
    loose = [
        item
        for item in response.chapters
        if isinstance(item, dict) and not str(item.get("id") or "").strip()
    ]
    for chapter, item in zip(missing, loose, strict=False):
        by_id[chapter.id] = item

    known_chars = {c.id for c in bible.characters}
    known_locs = {s.id for s in bible.settings}
    for chapter in targets:
        item = by_id.get(chapter.id)
        if not item:
            logger.warning("章 %s 扩写缺失，保留骨架", chapter.id)
            continue
        chapter.title = str(item.get("title") or chapter.title)
        chapter.summary = str(item.get("summary") or item.get("brief") or chapter.brief)
        chapter.beats = [str(b) for b in ensure_str_list(item.get("beats")) if str(b).strip()]
        chapter.hook = str(item.get("hook") or "")
        chapter.characters = [
            c
            for c in ensure_str_list(item.get("characters"))
            if not known_chars or c in known_chars
        ]
        chapter.locations = [
            loc
            for loc in ensure_str_list(item.get("locations"))
            if not known_locs or loc in known_locs
        ]
        chapter.scene_budget = max(
            1, coerce_int(item.get("scene_budget"), DEFAULT_CHAPTER_SCENES)
        )
        chapter.duration_budget_sec = max(
            MIN_SCENE_DURATION_SEC,
            coerce_seconds(item.get("duration_budget_sec"), chapter.scene_budget * 12),
        )


async def expand_chapters(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    story_outline: StoryOutline,
    chapter_ids: list[str],
    *,
    start_index: int = 0,
    sources: dict[str, str] | None = None,
    previous: list[OutlineScene] | None = None,
    feedback: str = "",
) -> ScriptOutline:
    """把选中的章展开成「分场大纲」（id 由系统分配，续接已有场次编号）。

    `previous` + `feedback`：重做一章时给出上一版的场次和修改意见。

    `sources`（章 id → 原文）给了就是**原著改编**：原文进提示词，换用改编口径的
    system prompt，并让模型为每场摘出依据的原文片段（逐场写剧本时据此保留原著对白）。
    """
    wanted = [story_outline.get_chapter(cid) for cid in chapter_ids]
    chapters = [c for c in wanted if c is not None]
    if not chapters:
        raise ValueError("没有可展开的章：请先在长篇大纲里选择要拍的章节")

    lines: list[str] = []
    for chapter in chapters:
        detail = [f"## {chapter.id}｜{chapter.title}"]
        if chapter.summary:
            detail.append(f"正文: {chapter.summary}")
        if chapter.beats:
            beats = "\n".join(f"  {i}. {b}" for i, b in enumerate(chapter.beats, 1))
            detail.append("情节点:\n" + beats)
        if chapter.hook:
            detail.append(f"章末钩子: {chapter.hook}")
        if chapter.locations:
            detail.append(f"可用场景: {', '.join(chapter.locations)}")
        if chapter.characters:
            detail.append(f"在场角色: {', '.join(chapter.characters)}")
        if chapter.props:
            detail.append(f"关键道具: {', '.join(chapter.props)}")
        source = (sources or {}).get(chapter.id, "")
        if source:
            detail.append(f"原著原文:\n{quote_source(source)}")
        detail.append(
            f"请展开成 {chapter.scene_budget} 场，合计约 {chapter.duration_budget_sec} 秒"
        )
        lines.append("\n".join(detail))

    user_prompt = f"""## 故事圣经
{_format_bible(bible)}

## 全篇
标题: {story_outline.title}
一句话卖点: {story_outline.logline}
梗概: {story_outline.synopsis}
主题: {story_outline.theme}

## 全篇章节（保证展开的场次与全篇弧光一致）
{_format_chapter_index(story_outline)}

## 要展开的章
{(chr(10) * 2).join(lines)}

请逐章展开成分场大纲。场次数量与每场时长按上面给出的预算；
environment 必须具体到可见细节（不要写「办公室」这种泛称）。只输出 JSON。"""

    if previous is not None:
        before = "\n".join(
            f"  - {s.title}（{s.duration_budget_sec} 秒）：{s.brief}" for s in previous
        ) or "  （无）"
        note = " ".join(str(feedback or "").split()) or "重新展开一版，场次划分和冲突写得更好"
        user_prompt += f"""

## 重做这一章（按修改意见）
上一版的分场：
{before}

修改意见（优先级最高）：{note}
意见没提到的部分（章的情节点、钩子、时长预算）保持不变。"""
    adapting = any((sources or {}).get(c.id) for c in chapters)
    if adapting:
        user_prompt += "\n\n这是原著改编：按原文取舍压缩，不要新增情节；每场附上 source_excerpt。"
    messages = [
        LLMMessage(
            role="system", content=NOVEL_CHAPTER_SCENE_SYSTEM if adapting else CHAPTER_SCENE_SYSTEM
        ),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(
        messages,
        schema=ChapterOutlineResponse,
        temperature=0.4 if adapting else 0.7,
        max_tokens=8192,
    )
    raw = [s for s in response.scenes if isinstance(s, dict)]
    if not raw:
        raise ValueError("章展开失败：LLM 未返回场次列表")

    wanted_ids = {c.id for c in chapters}
    # 模型有时不回 chapter_id：按各章的场次预算顺序认领（保证每章都有场）
    claim: list[str] = []
    for chapter in chapters:
        claim.extend([chapter.id] * max(1, chapter.scene_budget))

    outline = ScriptOutline()
    next_no = start_index
    for i, item in enumerate(raw):
        cid = str(item.get("chapter_id") or "").strip()
        if cid not in wanted_ids:
            cid = claim[i] if i < len(claim) else chapters[-1].id
        next_no += 1
        chapter = story_outline.get_chapter(cid)
        budget = (
            apply_duration_budget(
                chapter.duration_budget_sec or DEFAULT_CHAPTER_DURATION_SEC,
                max(1, chapter.scene_budget),
                MIN_SCENE_DURATION_SEC,
            )
            if chapter
            else DEFAULT_SCENE_DURATION_SEC
        )
        outline.scenes.append(
            OutlineScene(
                id=f"scene_{next_no}",
                title=str(item.get("title") or f"第 {next_no} 场"),
                brief=str(item.get("brief") or item.get("summary") or ""),
                duration_budget_sec=max(
                    MIN_SCENE_DURATION_SEC, coerce_seconds(item.get("duration_sec"), budget)
                ),
                chapter_id=cid,
                environment=str(item.get("environment") or ""),
                source_excerpt=str(item.get("source_excerpt") or "")[:SOURCE_EXCERPT_MAX_CHARS],
            )
        )
    outline.target_duration_sec = sum(s.duration_budget_sec for s in outline.scenes)
    logger.info(
        "章展开完成 %s：%d 章 -> %d 场 / %ds",
        project.id,
        len(chapters),
        len(outline.scenes),
        outline.target_duration_sec,
    )
    return outline


def chapter_total_duration(story_outline: StoryOutline, chapter_ids: list[str]) -> int:
    """选中章的成片时长预算（成本可见用）。"""
    total = 0
    for cid in chapter_ids:
        chapter = story_outline.get_chapter(cid)
        if chapter is not None:
            total += chapter.duration_budget_sec or DEFAULT_CHAPTER_DURATION_SEC
    return total


# ── 单场 ─────────────────────────────────────────────


REVIEW_SYSTEM = """你是严格的剧本医生（story doctor）。只做一件事：找出这个剧本的故事层硬伤。

逐项检查：
1. 钩子：开场有没有抓住观众的疑问/危机？
2. 目标与阻力：主角想要什么？谁/什么在阻止？够不够具体？
3. 转折：中段有没有让局势**变了**的事件（而不是重复冲突）？
4. 高潮：矛盾有没有在结尾前被推到最紧并解决？
5. 收束：结尾有没有交代结果与代价（悬念可以留，但当前这条线要收）？
6. 因果链：每个场景是不是**因为**上一场才发生？有没有为推进而硬拗的巧合？
7. 人物动机：角色的关键选择是否符合人设与处境？

要求：
- 只报**可执行的硬伤**，不要报文风/节奏偏好，不要凑数
- 每条必须能指出是哪一场（scene_id），没有硬伤就返回空数组
- severity: "error" = 观众会觉得故事不成立/烂尾；"warning" = 明显薄弱但不致命
- 最多 6 条，按严重程度排序

只输出 JSON：
{"issues": [{"severity": "error", "scene_id": "scene_2", "category": "structure",
             "message": "问题是什么", "suggestion": "怎么改"}]}"""


class StructureReview(BaseModel):
    """LLM 结构评审的返回（宽松解析，模型多写字段也不炸）。"""

    class Item(BaseModel):
        severity: str = "warning"
        scene_id: str = ""
        category: str = "structure"
        message: str = ""
        suggestion: str = ""

    issues: list[Item] = Field(default_factory=list)


#: 选中的章只是全篇的一部分时，追加给剧本医生的说明
_PARTIAL_NOTE = """## 重要：这一段不是全篇
这是长篇剧集里**选中的一段**（后面还有未展开的章）。因此：
- **不要**因为结尾留悬念/未收束而报问题 —— 那是有意留的钩子
- **不要**要求在本段内解决全篇矛盾；只评估这一段的内部逻辑（因果、动机、场景合理性）
- 与「全篇尚未展开」有关的问题一律不要报；只报这一段自己站不住的地方"""


#: 原著改编时追加给剧本医生的说明：情节走向是原著定的，不该被「医」掉
_ADAPTATION_NOTE = """## 重要：这是原著改编
情节、结局与人物选择以原著为准。**不要**要求改动原著情节、加入新冲突或改结局；
原著自带的设定（超自然/荒诞前提、没有解释原因的事件、开放式结局）**不是问题，不要报**。
只报**改编造成**的问题：压缩后因果断裂、关键信息没交代、人物动机在剧本里看不出来。"""


async def review_structure(
    llm: LLMProvider,
    script: Script,
    bible: StoryBible,
    *,
    partial: bool = False,
    adaptation: bool = False,
) -> list[ConsistencyIssue]:
    """让 LLM 当剧本医生做一遍结构评审，返回 ERROR/WARNING 级的结构问题。

    这是「故事严谨」里**机器判不了**的那部分（钩子/转折/高潮/因果）。失败一律降级：
    评审挂了只记 warning，绝不因为它把生成链路打死。

    `partial=True`：只展开了长篇剧集里的几章，结尾的悬念是有意留的，不能算硬伤。
    """
    if not script.scenes:
        return []
    total = sum(scene.duration_sec for scene in script.scenes)
    lines = [f"## 目标时长\n{total} 秒 / {len(script.scenes)} 场"]
    if partial:
        lines.append(_PARTIAL_NOTE)
    if adaptation:
        lines.append(_ADAPTATION_NOTE)
    lines.append("## 角色\n" + "；".join(
        f"{c.id}={c.name}（{c.role}，{c.description[:40]}）" for c in bible.characters
    ))
    for scene in script.scenes:
        dialogue = " / ".join(f"{line.speaker}：{line.line}" for line in scene.dialogue)
        lines.append(
            f"## {scene.id}｜{scene.heading}（{scene.duration_sec}s）\n"
            f"概述：{scene.summary}\n情绪：{scene.emotion}\n台词：{dialogue}"
        )
    messages = [
        LLMMessage(role="system", content=REVIEW_SYSTEM),
        LLMMessage(role="user", content="\n".join(lines)),
    ]
    try:
        result = await llm.chat_json(
            messages, schema=StructureReview, temperature=0.2, max_tokens=2048
        )
    except Exception:
        logger.warning("剧本结构评审失败（不影响生成）", exc_info=True)
        return []

    known = {scene.id for scene in script.scenes}
    issues: list[ConsistencyIssue] = []
    for item in result.issues[:6]:
        message = " ".join(str(item.message or "").split())
        if not message:
            continue
        # 改编时评审看不到原著，分不清「改编弄断的」和「原著本来就这样」（实测把原著的
        # 荒诞前提报成 ERROR 拦住确认），所以只提示、不拦
        severity = (
            IssueSeverity.ERROR
            if str(item.severity).strip().lower() == "error" and not adaptation
            else IssueSeverity.WARNING
        )
        issues.append(
            ConsistencyIssue(
                severity=severity,
                scene_id=item.scene_id if item.scene_id in known else "",
                category="structure",
                message=message,
                suggestion=" ".join(str(item.suggestion or "").split())[:200],
            )
        )
    return issues


async def generate_scene(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    outline_scene: OutlineScene,
    prev_scenes: list[Scene],
    world_state: WorldState,
    total_scenes: int,
    *,
    revision: Scene | None = None,
    feedback: str = "",
) -> tuple[Scene, SceneResponse]:
    """生成单场剧本，返回 (Scene, 原始响应)。

    `revision` + `feedback`：按修改意见重写已有的这一场（`prev_scenes` 只给它之前的场次）。
    """
    budget = outline_scene.duration_budget_sec
    user_prompt = f"""## 故事圣经（只读，不可违反）
{_format_bible(bible)}

## 上下文
{_format_prev_scenes(prev_scenes)}

## 当前世界状态
{_format_world_state(world_state)}

## 本场任务
场次 ID: {outline_scene.id}
标题: {outline_scene.title}
概述: {outline_scene.brief}
本场时长: 必须为 {budget} 秒（允许 ±2 秒）
台词总量: {_dialogue_budget_line(project, budget)}；
超了就删句、并句，只留推动剧情的那几句
全剧预算: 共 {total_scenes} 场，合计 {project.target_duration_sec} 秒
（这是第 {len(prev_scenes) + 1} 场）
比例: {project.aspect_ratio.value}（影响构图描述）
{_source_block(outline_scene)}{_revision_block(revision, feedback)}
请生成本场完整内容（JSON）。"""

    messages = [
        LLMMessage(role="system", content=SCENE_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(messages, schema=SceneResponse, temperature=0.6, max_tokens=4096)

    dialogue: list[DialogueLine] = []
    for line in response.dialogue:
        cleaned = clean_dialogue_line(line.line)
        if cleaned:
            dialogue.append(DialogueLine(speaker=line.speaker.strip(), line=cleaned))

    scene = Scene(
        id=outline_scene.id,
        heading=response.heading,
        summary=response.summary or outline_scene.brief,
        dialogue=dialogue,
        duration_sec=response.duration_sec,
        emotion=response.emotion,
    )
    return scene, response


def _revision_block(revision: Scene | None, feedback: str) -> str:
    """重写一场时：当前版本 + 用户的修改意见（意见没提到的地方尽量保持）。"""
    if revision is None:
        return ""
    lines = "\n".join(f"  {d.speaker}: {d.line}" for d in revision.dialogue) or "  （无对白）"
    note = " ".join(str(feedback or "").split()) or "整体重写一版，保持剧情走向不变，写得更好"
    return f"""
## 重写本场（按修改意见）
当前版本：
标题: {revision.heading}
摘要: {revision.summary}
情绪: {revision.emotion}
对白:
{lines}

修改意见（优先级最高）：{note}
只按修改意见改；意见没提到的部分（人物、因果、和前后场的衔接、时长）尽量保持。
"""


def _source_block(outline_scene: OutlineScene) -> str:
    """原著改编：本场依据的原文片段 + 改编要求（非改编场次返回空串）。"""
    excerpt = (outline_scene.source_excerpt or "").strip()
    if not excerpt:
        return ""
    return f"""
## 原著片段（本场改编依据）
{quote_source(excerpt[:SOURCE_EXCERPT_MAX_CHARS])}

改编要求：台词从原著对白里**挑**最关键的几句（可删减、口语化，不得改变意思），
总量服从上面的「台词总量」，不要把原文对白整段搬进来；叙述与心理描写改写成
动作或少量对白；不要加入原著没有的情节；说话人必须映射到故事圣经里的角色 id。
"""


def normalize_script_durations(script: Script, target_duration_sec: int) -> bool:
    """把全剧时长拉回目标。返回是否发生了归一化。"""
    if not script.scenes or target_duration_sec <= 0:
        return False
    current = [s.duration_sec for s in script.scenes]
    if deviation_ratio(current, target_duration_sec) <= DURATION_TOLERANCE:
        script.duration_normalized = False
        return False

    fixed = scale_durations(current, target_duration_sec, MIN_SCENE_DURATION_SEC)
    for scene, duration in zip(script.scenes, fixed, strict=True):
        scene.duration_sec = duration
    script.duration_normalized = True
    logger.warning(
        "剧本时长归一化：%ds -> %ds（目标 %ds）",
        sum(current),
        script.total_duration_sec,
        target_duration_sec,
    )
    return True


async def generate_logline(llm: LLMProvider, script: Script) -> str:
    scene_summaries = "\n".join(f"- {s.summary}" for s in script.scenes)
    messages = [
        LLMMessage(role="system", content="用一句中文概括短剧核心冲突和看点，不超过50字。"),
        LLMMessage(role="user", content=f"场景列表:\n{scene_summaries}"),
    ]
    try:
        text = await llm.chat(messages, temperature=0.5, max_tokens=100)
        return text.strip().strip('"').strip()
    except Exception as exc:  # noqa: BLE001 - logline 是锦上添花，不该拖垮整条流水线
        logger.warning("logline 生成失败，降级使用首场摘要: %s", exc)
        return script.scenes[0].summary if script.scenes else ""
