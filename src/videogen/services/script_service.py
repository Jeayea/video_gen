from __future__ import annotations

import logging
import re

from videogen.models.bible import StoryBible
from videogen.models.llm_io import OutlineResponse, SceneResponse
from videogen.models.project import Project
from videogen.models.script import (
    MIN_SCENE_DURATION_SEC,
    DialogueLine,
    OutlineScene,
    Scene,
    Script,
    ScriptOutline,
)
from videogen.models.world_state import WorldState
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.utils.duration import apply_duration_budget, deviation_ratio, scale_durations

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
    """生成分场大纲，并把时长预算按场次均分写进大纲。"""
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


# ── 单场 ─────────────────────────────────────────────


async def generate_scene(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    outline_scene: OutlineScene,
    prev_scenes: list[Scene],
    world_state: WorldState,
    total_scenes: int,
) -> tuple[Scene, SceneResponse]:
    """生成单场剧本，返回 (Scene, 原始响应)。"""
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
全剧预算: 共 {total_scenes} 场，合计 {project.target_duration_sec} 秒
（这是第 {len(prev_scenes) + 1} 场）
比例: {project.aspect_ratio.value}（影响构图描述）

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
