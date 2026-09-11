from __future__ import annotations

import logging

from videogen.models.bible import StoryBible
from videogen.models.project import InputMode, Project
from videogen.providers.llm.base import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

BIBLE_GEN_SYSTEM = """你是一位资深短剧策划。根据用户输入生成「故事圣经」(Story Bible)。

输出 JSON 格式：
{
  "characters": [
    {
      "id": "char_1",
      "name": "中文名",
      "aliases": ["别名1"],
      "age": 25,
      "role": "主角",
      "description": "中文描述",
      "visual_prompt": "English visual description for AI video generation",
      "voice_id": "",
      "personality": ["特质1", "特质2"],
      "relationships": [{"target": "char_2", "type": "敌对", "note": "说明"}]
    }
  ],
  "settings": [
    {
      "id": "loc_1",
      "name": "场景名",
      "description": "中文描述",
      "visual_prompt": "English visual description"
    }
  ],
  "facts": [
    {"id": "fact_1", "content": "事实内容", "type": "number"}
  ],
  "style": {
    "genre": "都市复仇",
    "tone": "压抑中带爽感",
    "pacing": "快节奏",
    "taboo": ["超现实"]
  }
}

要求：
- 角色 3-6 个，有明确冲突关系
- visual_prompt 必须是英文，适合 AI 视频生成（只描述外貌/服装/气质，不要写角色 id）
- id 只能是 char_1..char_6 / loc_1..loc_6 / fact_1..N 这类稳定编号
- facts 收集关键数字/日期/关系，后续生成必须遵守
- age 必须是数字（不要写 "25岁"）
- 只输出 JSON"""

_MODE_DESC = {
    InputMode.THEME: "主题/关键词",
    InputMode.OUTLINE: "故事大纲",
    InputMode.FULL_SCRIPT: "完整剧本",
}


async def generate_bible(project: Project, llm: LLMProvider) -> StoryBible:
    """根据项目输入生成 Story Bible。"""
    mode_desc = _MODE_DESC[project.input_mode]

    user_prompt = f"""输入类型: {mode_desc}
用户输入:
{project.input_text}

目标时长: {project.target_duration_sec} 秒
画面比例: {project.aspect_ratio.value}

请生成故事圣经。"""

    messages = [
        LLMMessage(role="system", content=BIBLE_GEN_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]

    logger.info("Generating story bible for project %s", project.id)
    bible = await llm.chat_json(messages, schema=StoryBible, temperature=0.7, max_tokens=4096)
    if not bible.characters:
        raise ValueError("故事圣经没有生成任何角色")

    # 兜底：模型偶尔漏 id，补成稳定编号
    for i, character in enumerate(bible.characters, start=1):
        if not character.id:
            character.id = f"char_{i}"
    for i, setting in enumerate(bible.settings, start=1):
        if not setting.id:
            setting.id = f"loc_{i}"
    for i, fact in enumerate(bible.facts, start=1):
        if not fact.id:
            fact.id = f"fact_{i}"

    logger.info(
        "Bible generated: %d characters, %d settings, %d facts",
        len(bible.characters),
        len(bible.settings),
        len(bible.facts),
    )
    return bible
