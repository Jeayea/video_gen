from __future__ import annotations

import logging

from videogen.models.bible import StoryBible
from videogen.models.project import InputMode, Project
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.utils import region

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
      "gender": "female",
      "role": "主角",
      "region": "cn",
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
  "props": [
    {
      "id": "prop_1",
      "name": "道具名",
      "description": "中文：物品本身的外观",
      "visual_prompt": "English: the object itself (material, color, shape, wear)"
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
- **名字、地区、外貌三者必须对齐**：这是一部哪个国家的戏，全剧组就用那个国家的名字与长相
  （`region` 填 cn/jp/kr/west/other）。中文名 → `region="cn"` 并给中国人长相；
  日文名（含假名/日本姓氏）→ `region="jp"`；韩文名 → `region="kr"`；英文名 → `region="west"`。
  用户输入里写明了国家/城市/语言时，以用户输入为准，**不要混国籍**
- visual_prompt 必须是英文，适合 AI 视频生成（只描述外貌/服装/气质，不要写角色 id），
  并且**外貌要和 region 一致**（例：region=cn 就写 Chinese woman, East Asian features…），
  不要给中文名配一张欧美脸
- id 只能是 char_1..char_6 / loc_1..loc_6 / prop_1..prop_3 / fact_1..N 这类稳定编号
- props 只收 0-3 个**推动剧情**、会被特写或反复出现的关键道具（信物/证据/关键文件…）；
  手机、水杯这类随手物品和桌椅陈设都不算，没有就给空数组
- facts 收集关键数字/日期/关系，后续生成必须遵守
- age 必须是数字（不要写 "25岁"）；gender 填 male / female（配音选角按它挑男女声）
- 只输出 JSON"""

#: 声明了国家/地区时给模型的「选角说明」：名字怎么起、长相按哪国、region 字段填什么
_REGION_BRIEF = {
    "cn": (
        "中国大陆/华语。名字用中文名（姓+名，例：林悦、李薇儿、张建国、赵敏），"
        "长相是中国人（East Asian features, an average everyday Chinese face）"
    ),
    "jp": (
        "日本。名字用日文名（例：田中由纪、佐藤美咲、铃木健太、高桥凉子），"
        "长相是日本人（East Asian features, an average everyday Japanese face）"
    ),
    "kr": (
        "韩国。名字用韩文名（例：金敏俊、朴智恩、李承宇、崔秀珍），"
        "长相是韩国人（East Asian features, an average everyday Korean face）"
    ),
    "west": (
        "欧美。名字用英文名（例：Emily Carter、John Smith、Sarah Miller），"
        "长相是欧美人（European or North American features）"
    ),
    "other": "不限定国家/地区，按剧情自洽即可",
}


def constraint_block(project: Project) -> str:
    """把「国家限定 + 额外要求」拼成提示词里的一段（都没填就返回空串）。"""
    lines: list[str] = []
    brief = _REGION_BRIEF.get(region.normalize_region(project.cast_region))
    if brief:
        lines += [
            "## 国家/地区限定（硬要求，优先级高于你自己的偏好）",
            f"- 整部戏：{brief}",
            "- **每个角色都要填 `region` 字段**，值用 cn/jp/kr/west/other，默认全部填同一个值",
            "- 名字必须和这个国家一致；不要出现混国籍的剧组",
            "- 如果剧情确实需要一个外国人（留学生/外企高管等），在 description 里写明他的国籍，"
            "并且只给这个角色填不同的 region",
        ]
    notes = " ".join(str(project.bible_notes or "").split())
    if notes:
        lines += [
            "",
            "## 用户的额外要求（人物设定 / 世界观 / 其它，必须遵守）",
            notes,
        ]
    return "\n".join(lines)


_MODE_DESC = {
    InputMode.THEME: "主题/关键词",
    InputMode.OUTLINE: "故事大纲",
    InputMode.FULL_SCRIPT: "完整剧本",
    InputMode.NOVEL: "小说原文",
}


async def generate_bible(project: Project, llm: LLMProvider) -> StoryBible:
    """根据项目输入生成 Story Bible。"""
    mode_desc = _MODE_DESC[project.input_mode]

    user_prompt = f"""输入类型: {mode_desc}
用户输入:
{project.input_text}

目标时长: {project.target_duration_sec} 秒
画面比例: {project.aspect_ratio.value}"""
    constraint = constraint_block(project)
    if constraint:
        user_prompt += f"\n\n{constraint}"
    user_prompt += "\n\n请生成故事圣经。"""

    messages = [
        LLMMessage(role="system", content=BIBLE_GEN_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]

    logger.info("Generating story bible for project %s", project.id)
    bible = await llm.chat_json(messages, schema=StoryBible, temperature=0.7, max_tokens=4096)
    if not bible.characters:
        raise ValueError("故事圣经没有生成任何角色")

    finalize_bible(bible, project)

    logger.info(
        "Bible generated: %d characters, %d settings, %d facts",
        len(bible.characters),
        len(bible.settings),
        len(bible.facts),
    )
    return bible


def finalize_bible(bible: StoryBible, project: Project) -> StoryBible:
    """生成后的统一收尾：写入声明的国家、给漏掉 id 的条目补稳定编号。"""
    # 用户声明的国家写进圣经：下游（定妆照/视频）以它为准，不再靠名字猜
    declared = region.normalize_region(project.cast_region)
    if declared:
        bible.cast_region = declared
    # 兜底：模型偶尔漏 id，补成稳定编号
    for i, character in enumerate(bible.characters, start=1):
        if not character.id:
            character.id = f"char_{i}"
    for i, setting in enumerate(bible.settings, start=1):
        if not setting.id:
            setting.id = f"loc_{i}"
    for i, prop in enumerate(bible.props, start=1):
        if not prop.id:
            prop.id = f"prop_{i}"
    for i, fact in enumerate(bible.facts, start=1):
        if not fact.id:
            fact.id = f"fact_{i}"
    return bible
