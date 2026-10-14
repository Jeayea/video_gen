from __future__ import annotations

import logging

from pydantic import BaseModel

from videogen.models.bible import Character, Prop, Setting, StoryBible
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
  （`region` 填 cn/jp/kr/west/india/mideast/africa/latam/sea/other）。中文名 → `region="cn"`
  并给中国人长相；日文名（含假名/日本姓氏）→ `region="jp"`；韩文名 → `region="kr"`；
  英文名 → `region="west"`；印度/中东/非洲/拉美/东南亚的故事分别填 india/mideast/africa/latam/sea。
  用户输入里写明了国家/城市/语言时，以用户输入为准，**不要混国籍**
- visual_prompt 必须是英文，适合 AI 视频生成（只描述外貌/服装/气质，不要写角色 id），
  画面里要出现的文字（黑板 / 招牌 / 横幅 / 屏幕）保留原文、用引号括起来，不要翻译，
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
    "india": (
        "南亚/印度。名字用印度名（例：Priya Sharma、Arjun Patel、Ananya Rao），"
        "长相是南亚人（South Asian features）"
    ),
    "mideast": (
        "中东/北非。名字用阿拉伯或波斯名（例：Layla Haddad、Omar Khalil、Darius Karimi），"
        "长相是中东人（Middle Eastern features）"
    ),
    "africa": (
        "撒哈拉以南非洲。名字用当地常见名（例：Amara Okafor、Kwame Mensah、Wanjiru Kamau），"
        "长相是非洲人（Black African features）"
    ),
    "latam": (
        "拉丁美洲。名字用西班牙语/葡萄牙语名（例：Sofía Ramírez、Mateo González、Lucas Silva），"
        "长相是拉美人（Latin American features）"
    ),
    "sea": (
        "东南亚。名字用当地名（例：Nguyen Thi Lan、Somchai Wongsa、Maria Santos），"
        "长相是东南亚人（Southeast Asian features）"
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
            f"- **每个角色都要填 `region` 字段**，值用 {region.REGION_KEYS_TEXT}，"
            "默认全部填同一个值",
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


# ── 单条重写 ─────────────────────────────────────────────

BIBLE_ITEM_SYSTEM = """你负责故事圣经条目修订：按用户的修改意见，重写故事圣经里的**一个**条目
（一个角色 / 一个场景 / 一个关键道具），其余条目只是给你看的上下文，不要改它们。

规则：
- 只输出这一个条目的 JSON 对象（不是数组，不要外层包装），字段和「当前版本」一样
- id 原样保留；意见没提到的字段尽量保持（尤其是名字、年龄、性别、和其他角色的关系）
- visual_prompt 必须是英文，只描述外貌 / 服装 / 气质（场景写可见陈设与光线，道具写材质颜色形状），
  不要写任何 id，外貌要和 region 一致；
  画面里要出现的文字（黑板 / 招牌 / 横幅 / 屏幕）保留原文、用引号括起来，不要翻译
- 角色的 age 是数字，gender 填 male / female
- 只输出 JSON"""

#: kind（URL 里的写法）→ 圣经里的列表字段与条目模型
ITEM_KINDS: dict[str, type[BaseModel]] = {
    "characters": Character,
    "settings": Setting,
    "props": Prop,
}
#: 这些字段不交给模型改：参考图 / seed / 音色是用户或后续步骤定下来的
_KEEP_FIELDS = ("id", "reference_image", "seed", "voice_id")


def find_item(bible: StoryBible, kind: str, item_id: str) -> BaseModel | None:
    items = getattr(bible, kind, None) if kind in ITEM_KINDS else None
    return next((item for item in items or [] if item.id == item_id), None)


def _context_lines(bible: StoryBible, kind: str, item_id: str) -> str:
    rows: list[str] = []
    for c in bible.characters:
        if not (kind == "characters" and c.id == item_id):
            rows.append(f"- 角色 {c.id} {c.name}（{c.role}，{c.age or '?'} 岁）：{c.description}")
    for loc in bible.settings:
        if not (kind == "settings" and loc.id == item_id):
            rows.append(f"- 场景 {loc.id} {loc.name}：{loc.description}")
    for prop in bible.props:
        if not (kind == "props" and prop.id == item_id):
            rows.append(f"- 道具 {prop.id} {prop.name}：{prop.description}")
    style = bible.style
    rows.append(f"- 风格：{style.genre} / {style.tone} / {style.pacing}")
    return "\n".join(rows)


async def regenerate_item(
    project: Project,
    bible: StoryBible,
    llm: LLMProvider,
    kind: str,
    item_id: str,
    *,
    feedback: str = "",
) -> tuple[BaseModel, bool]:
    """按修改意见重写一个圣经条目，返回 `(新条目, 外貌是否变了)`。不改 `bible` 本身。

    外貌（visual_prompt）变了时，已有的参考图 / 定妆照就和文字对不上了 —— 由调用方提示用户重画。
    """
    model = ITEM_KINDS.get(kind)
    current = find_item(bible, kind, item_id)
    if model is None or current is None:
        raise ValueError(f"圣经里没有 {kind}/{item_id}")
    note = " ".join(str(feedback or "").split()) or "重新写一版，更鲜明、更有辨识度"
    user_prompt = f"""类型: {kind}
条目 id: {item_id}

## 当前版本
{current.model_dump_json(indent=2)}

## 圣经里的其它条目（上下文，不要改）
{_context_lines(bible, kind, item_id)}

## 修改意见（优先级最高）
{note}"""
    constraint = constraint_block(project)
    if constraint:
        user_prompt += f"\n\n{constraint}"
    messages = [
        LLMMessage(role="system", content=BIBLE_ITEM_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]
    fresh = await llm.chat_json(messages, schema=model, temperature=0.7, max_tokens=2048)
    fresh = fresh.model_copy(update={field: getattr(current, field) for field in _KEEP_FIELDS
                                     if hasattr(current, field)})
    changed_look = " ".join(fresh.visual_prompt.split()) != " ".join(current.visual_prompt.split())
    return fresh, changed_look
