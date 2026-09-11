from __future__ import annotations

import logging
import re

from videogen.models.bible import StoryBible
from videogen.models.llm_io import ShotsResponse
from videogen.models.project import Project
from videogen.models.script import Scene, Script
from videogen.models.storyboard import Shot, Storyboard
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.utils.duration import scale_durations

logger = logging.getLogger(__name__)

STORYBOARD_SYSTEM = """你是一位专业分镜师。根据剧本场景生成分镜脚本。

每个场景拆成 2-5 个镜头。每个镜头输出：
{
  "shot_no": 1,
  "duration_sec": 4,
  "camera": "景别 · 运镜（如：中景 · 固定 / 近景 · 推）",
  "visual_description": "中文画面描述",
  "character_in_frame": ["char_1"],
  "mood": "画面情绪",
  "video_prompt": "English prompt: subject + action + environment + lighting + style",
  "narration": null 或 "旁白文字",
  "dialogue_text": null 或 "台词文字",
  "dialogue_speaker": null 或 "char_id"
}

要求：
- video_prompt 必须全英文，面向 AI 视频生成模型
- video_prompt 里禁止出现 char_1 / loc_1 这类内部 id，必须写成具体的外貌/场景描述
- character_in_frame 必须是故事圣经里已有的角色 id
- 构图提示随 aspect_ratio 变化
- 台词尽量对准角色镜头
- 只输出 JSON: {"shots": [...]}"""

_ASPECT_HINTS = {
    "9:16": "vertical 9:16, emphasize vertical composition, full-body or upper-body framing",
    "16:9": "horizontal 16:9, emphasize wide composition, landscape framing",
    "1:1": "square 1:1, centered composition",
}

_CHAR_ID_RE = re.compile(r"\bchar_[0-9]+\b")
_LOC_ID_RE = re.compile(r"\bloc_[0-9]+\b")


def _format_brief_bible(bible: StoryBible) -> str:
    lines = ["角色（id 仅用于 character_in_frame / dialogue_speaker，不要写进 video_prompt）:"]
    for c in bible.characters:
        lines.append(f"  - id={c.id} 姓名={c.name} 外貌={c.visual_prompt or c.description}")
    lines.append("场景:")
    for s in bible.settings:
        lines.append(f"  - id={s.id} 名称={s.name} 环境={s.visual_prompt or s.description}")
    return "\n".join(lines)


def _character_phrase(bible: StoryBible, char_id: str) -> str:
    character = bible.get_character(char_id)
    if not character:
        return "the character"
    if character.visual_prompt:
        first_clause = re.split(r"[,.;，。；]", character.visual_prompt)[0].strip()
        if first_clause:
            return first_clause
    return character.description or "the character"


def sanitize_video_prompt(prompt: str, bible: StoryBible) -> str:
    """把 video_prompt 里的内部 id 换成对视频模型有意义的外貌/环境描述。"""
    text = prompt or ""

    def replace_char(match: re.Match[str]) -> str:
        return _character_phrase(bible, match.group(0))

    def replace_loc(match: re.Match[str]) -> str:
        for setting in bible.settings:
            if setting.id == match.group(0):
                return setting.name
        return "the location"

    text = _CHAR_ID_RE.sub(replace_char, text)
    text = _LOC_ID_RE.sub(replace_loc, text)
    text = re.sub(r"(\s*,\s*){2,}", ", ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


async def generate_shots_for_scene(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    scene: Scene,
) -> list[Shot]:
    """为一个场景生成镜头列表（id / shot_no / scene_id 由系统分配）。"""
    aspect_hint = _ASPECT_HINTS.get(project.aspect_ratio.value, _ASPECT_HINTS["9:16"])
    dialogue_text = "\n".join(f"  {d.speaker}: {d.line}" for d in scene.dialogue) or "（无对白）"

    user_prompt = f"""## 角色与场景参考
{_format_brief_bible(bible)}

## 本场剧本
场次: {scene.id}
标题: {scene.heading}
摘要: {scene.summary}
情绪: {scene.emotion}
时长: {scene.duration_sec} 秒
对白:
{dialogue_text}

## 要求
- 画面比例: {project.aspect_ratio.value} → {aspect_hint}
- 拆成 2-5 个镜头，所有镜头时长加起来必须正好等于 {scene.duration_sec} 秒
- video_prompt 必须是英文，且不得包含 char_N / loc_N 这类 id
- 台词对应的镜头要标记 dialogue_text 和 dialogue_speaker（speaker 用角色 id）
- 无对白的空镜/过渡镜 narration 可为 null

输出 JSON: {{"shots": [...]}}"""

    messages = [
        LLMMessage(role="system", content=STORYBOARD_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(messages, schema=ShotsResponse, temperature=0.5, max_tokens=4096)

    shots: list[Shot] = []
    for index, draft in enumerate(response.shots, start=1):
        shots.append(
            Shot(
                id=f"{scene.id}_shot_{index}",
                scene_id=scene.id,
                shot_no=index,
                duration_sec=draft.duration_sec,
                camera=draft.camera,
                visual_description=draft.visual_description,
                character_in_frame=draft.character_in_frame,
                mood=draft.mood,
                video_prompt=sanitize_video_prompt(draft.video_prompt, bible),
                narration=draft.narration,
                dialogue_text=draft.dialogue_text,
                dialogue_speaker=draft.dialogue_speaker,
            )
        )
    return shots


def normalize_storyboard_durations(storyboard: Storyboard, script: Script) -> bool:
    """让每个场景的镜头时长之和精确等于该场剧本时长。"""
    changed = False
    for scene in script.scenes:
        shots = storyboard.shots_for_scene(scene.id)
        if not shots:
            continue
        current = [s.duration_sec for s in shots]
        if sum(current) == scene.duration_sec:
            continue
        for shot, duration in zip(
            shots, scale_durations(current, scene.duration_sec, 1), strict=True
        ):
            shot.duration_sec = duration
        changed = True
    storyboard.duration_normalized = changed
    return changed
