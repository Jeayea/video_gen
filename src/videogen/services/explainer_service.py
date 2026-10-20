"""知识讲解型科普（`content_type=explainer`）。

设计见 docs/EXPLAINER_DESIGN.md：沿用镜头模型，一句旁白一个镜头，每句旁白挂到知识底稿里的事实上。
现有 service 只在入口处按内容类型分派到这里，不在短剧的函数里堆分支。
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import json
import logging
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable

from videogen.config import Settings, get_settings
from videogen.models.bible import Fact, Prop, Setting, StoryBible, StyleGuide
from videogen.models.consistency import ConsistencyIssue, IssueSeverity
from videogen.models.explainer import (
    AnnotateResponse,
    ExplainerSceneResponse,
    ExplainerSource,
    KnowledgeItem,
    KnowledgeResponse,
    NewFact,
    SourceChunk,
    VisualsResponse,
)
from videogen.models.llm_io import OutlineResponse
from videogen.models.project import ContentType, InputMode, Project
from videogen.models.script import (
    MIN_SCENE_DURATION_SEC,
    NARRATOR,
    DialogueLine,
    OutlineScene,
    Scene,
    Script,
    ScriptOutline,
)
from videogen.models.storyboard import Shot, Storyboard
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.services import bible_service, script_service, storyboard_service
from videogen.utils.duration import SPEECH_CHARS_PER_SEC, apply_duration_budget, speech_seconds
from videogen.utils.novel import pack_paragraphs, source_fingerprint
from videogen.utils.text import MATERIAL_GUARD, quote_source

logger = logging.getLogger(__name__)

#: 科普配乐比全局 BGM_GAIN_DB 再低多少 dB：旁白是主线，音乐只垫底
MUSIC_GAIN_OFFSET_DB = -6.0
#: 科普默认烧字幕样式：观众常静音看，半透明底条在任何画面上都读得清
SUBTITLE_STYLE = "box"
MUSIC_MOOD = "neutral"


def is_explainer(project: Project | None) -> bool:
    return getattr(project, "content_type", None) == ContentType.EXPLAINER


#: 科普的两种画面来源
VISUAL_STILL = "still"
VISUAL_VIDEO = "video"


def uses_stills(project: Project | None) -> bool:
    """这个项目的画面是不是每句一张 AI 示意图（`visual_mode=still`，只对科普生效）。"""
    return is_explainer(project) and getattr(project, "visual_mode", "") == VISUAL_STILL


#: 科普语气（项目 `tone`）：key → (界面名, 写稿要求, 画面要求, 念法要求)。
#: 空 = 生成知识底稿时由模型按题目推荐；推荐也没给时不加语气要求（老项目行为）
TONES: dict[str, tuple[str, str, str, str]] = {
    "serious": (
        "严谨",
        "语气严谨克制：不用网络流行语，不开玩笑；比喻要准确、朴素",
        "画面朴素清楚，不搞笑",
        "念法沉稳、清楚，不俏皮",
    ),
    "light": (
        "轻松",
        "语气轻松口语，像跟朋友聊天：多用生活化的比喻，偶尔一句俏皮话；不刻意玩网络梗",
        "画面亲切生动，人物表情自然",
        "念法轻松自然，像在跟朋友聊天",
    ),
    "meme": (
        "玩梗",
        "语气幽默、玩梗：开场钩子、过渡句和比喻可以用这个热词本身的梗、网络流行语、反差、自嘲、"
        "一听就是玩笑的夸张比喻，让人想笑、想转发。讲事实的句子照底稿说：数字和结论不夸张，"
        "不为了好笑改意思。不拿疾病、患者或某一类人开涮",
        "画面要有喜剧感：夸张的表情和动作、反差、会做表情的拟人化物件（水瓶、器官、细胞），"
        "像表情包；讲事实的镜头仍要让人一眼看懂这件事",
        "念法俏皮、有节奏感，梗要念出笑点；讲到事实和建议时稳下来",
    ),
}

#: 科普插图画风（项目 `illustration_style`）：key → (界面名, 英文画风词)。
#: `editorial` 的画风词取配置 `EXPLAINER_ILLUSTRATION_STYLE`；不是 key 的值当作用户自填的英文画风。
#: 古典画风容易被画上铭文 / 卷轴字，所以显式写「没有铭文」（统一的「禁止文字」规则照旧追加）
ILLUSTRATION_STYLES: dict[str, tuple[str, str]] = {
    "editorial": ("杂志插画（默认）", ""),
    "flat_cartoon": (
        "扁平卡通",
        "flat vector cartoon illustration, bold clean outlines, bright cheerful colors, "
        "simple rounded characters with expressive faces, plain background",
    ),
    "comic": (
        "手绘漫画",
        # 只写 comic 会被画成分格的漫画页（「大水理论」试拍 10 张里 1 张三格）
        "a single full-bleed comic panel (not a comic page, no panel grid), hand-drawn comic "
        "illustration, expressive exaggerated characters and reactions, ink outlines with flat "
        "colors, playful energetic composition",
    ),
    "watercolor": (
        "水彩绘本",
        "soft watercolor picture-book illustration, gentle washes of color, "
        "visible paper texture, warm and calm",
    ),
    "vintage_science": (
        "复古科学图鉴",
        "vintage scientific illustration, engraved line art with hand-colored tints, "
        "aged cream paper texture, precise and elegant",
    ),
    "greek_vase": (
        "古希腊陶瓶画",
        "ancient Greek red-figure pottery painting style, terracotta orange figures on a black "
        "glaze ground, elegant fine black line work, decorative meander border, no inscriptions",
    ),
    "roman_mosaic": (
        "罗马马赛克",
        "ancient Roman floor mosaic style made of small stone tesserae, earthy ochre, terracotta, "
        "cream and deep blue tones, no inscriptions",
    ),
    "ink_wash": (
        "中国水墨",
        "traditional Chinese ink wash painting, expressive brush strokes, muted colors with "
        "light color accents, generous empty space, no calligraphy, no seals",
    ),
}
#: 画风 key 的写法；自填画风不能和它撞
_STYLE_KEY = re.compile(r"^[a-z_]+$")


def tone_of(project: Project | None) -> str:
    """科普项目的语气 key；短剧和没定语气的科普项目为空。"""
    if not is_explainer(project):
        return ""
    tone = str(getattr(project, "tone", "") or "")
    return tone if tone in TONES else ""


def tone_block(project: Project | None, part: int = 1) -> str:
    """进提示词的语气要求（`part`：1 写稿 / 2 画面 / 3 念法）。没定语气时为空。"""
    tone = tone_of(project)
    if not tone:
        return ""
    label, *rules = TONES[tone]
    return f"语气：{label}。{rules[part - 1]}"


def validate_illustration_style(value: str) -> str:
    """校验项目的画风值，返回规整后的值；不合法抛 ValueError（API 转 400）。"""
    value = " ".join(str(value or "").split())
    if not value or value in ILLUSTRATION_STYLES:
        return value
    if _STYLE_KEY.match(value):
        raise ValueError(f"没有这个画风：{value}")
    if re.search(r"[　-鿿가-힯＀-￯]", value):
        raise ValueError("自定义画风请用英文写：画风词会进图片 prompt，中文会被画进画面")
    return value


def illustration_prompt(project: Project | None, settings: Settings | None = None) -> str:
    """插图的英文画风词：项目选的预设 / 自填画风，都没有时用配置的默认画风。"""
    settings = settings or get_settings()
    value = str(getattr(project, "illustration_style", "") or "").strip()
    preset = ILLUSTRATION_STYLES.get(value)
    if preset is not None:
        return preset[1] or settings.explainer_illustration_style
    if value and not _STYLE_KEY.match(value):
        return value
    return settings.explainer_illustration_style


def illustration_tag(project: Project | None) -> str:
    """画风的短标记，拼进插图 provider 的模型名：画风变了 = 换模型，已画好的图全部重画。

    默认画风（空 / editorial）不加标记，老项目不会因此重画。
    """
    value = str(getattr(project, "illustration_style", "") or "").strip()
    if not value or value == "editorial":
        return ""
    if value in ILLUSTRATION_STYLES:
        return value
    return "custom-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def illustration_label(project: Project | None) -> str:
    value = str(getattr(project, "illustration_style", "") or "").strip()
    preset = ILLUSTRATION_STYLES.get(value)
    if preset is not None:
        return preset[0]
    return f"自定义：{value}" if value else ILLUSTRATION_STYLES["editorial"][0]


def apply_recommendation(project: Project, bible: StoryBible) -> bool:
    """把知识底稿里模型推荐的语气 / 画风填进项目（用户已经选了的不动）。返回项目是否改了。"""
    changed = False
    tone = bible.style.explainer_tone
    if not project.tone and tone in TONES:
        project.tone = tone
        changed = True
    style = bible.style.illustration_style
    if not project.illustration_style and style in ILLUSTRATION_STYLES:
        project.illustration_style = style
        changed = True
    return changed


def apply_creation_defaults(project: Project, settings: Settings | None = None) -> Project:
    """建项目时给科普项目填默认值（用户没填的才填；短剧原样返回）。

    写在项目字段上而不是运行时判断：面板上看得见、改得了，和用户自己设的值走同一条路。
    """
    if not is_explainer(project):
        return project
    settings = settings or get_settings()
    if not project.visual_mode:
        project.visual_mode = VISUAL_STILL
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
  "settings": [{"name": "演示场景名", "description": "这里能拍到什么（中文）",
                "visual_prompt": "a dim lab bench, a glass prism, a white screen, sunlight"}],
  "props": [{"name": "演示物", "description": "外观（中文）",
             "visual_prompt": "a clear triangular glass prism with polished edges"}],
  "style": {"genre": "科普", "tone": "语气", "pacing": "节奏",
            "explainer_tone": "light", "illustration_style": "editorial"}
}

要求：
- facts：讲清这个题目必需的事实，8-20 条。type 填 concept（核心概念）/ misconception
  （常见误解，content 写成「常见误解：……；实际上……」）/ number（数字、日期、量级）/ general
- 每条事实只说一件事，能被人逐条核对；拿不准的不要写，不要编数字
- source 按用户消息里的「出处要求」写
- settings 是演示环境（实验室、天空、细胞内部……）2-5 个；props 是需要反复出现、外观要一致的
  演示物 0-3 个，没有就给空数组
- visual_prompt 必须是英文，只写看得见的东西；不要出现公式、数据、标注这类知识性文字。
  物品外观也不要写刻度、读数、计时标记、标签和包装文字：这段描述会跟着这件物品进每一张图，
  图片模型照着画出来的数字几乎都是错的
- 瓶、壶、桶、杯这类容器的 visual_prompt 写明开口：在哪、几个、什么样（"a single open screw-top
  mouth at the top, no other openings"）。不写的话每张图的出水方式都不一样（拧盖、泵头、龙头），
  人喝水时嘴也对不上瓶口
- style.explainer_tone 按题目选一个：serious（严肃的医疗、安全、灾难、历史悲剧）/ light（一般科普）/
  meme（网络热词、流行现象、段子式的问题，即使和健康有关也可以选）
- style.illustration_style 按题目选一个最合适的插图画风：{styles}
- 不要输出任何 id（由系统分配）
- 只输出 JSON""".replace(
    "{styles}",
    " / ".join(f"{key}（{label}）" for key, (label, _) in ILLUSTRATION_STYLES.items())
    + "。讲古希腊罗马、古代中国这类历史题目时选对应时代的画风",
)


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
        lines += ["", f"## 参考资料{part}", quote_source(chunk, guard=MATERIAL_GUARD)]
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
    """参考资料按段落装箱切块（不认章节标题）。

    以前复用小说切章：标题前不到 200 字的引言、正文不到 20 字的「章」会被丢掉 ——
    对小说合理（那是目录 / 题记），对参考资料就是丢了内容。
    """
    return pack_paragraphs(text, max(500, int(max_chars)))


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
    keep_status_if_source_unchanged(bible.facts, previous)
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
                # 内容一字不差：沿用 id 和核对状态。出处有没有变要等 `verify_sources`
                # 把引文收尾（换成资料原句 / 标「模型自述」）之后再比
                # （`keep_status_if_source_unchanged`）
                facts.append(
                    Fact(id=kept.id, content=content, type=item.type or kept.type,
                         source=source, status=kept.status)
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


@functools.lru_cache(maxsize=8)
def _normalize_with_map(text: str) -> tuple[str, tuple[int, ...]]:
    """去掉空白和标点（只留字母、数字、汉字），并记下每个字在原文里的位置。

    逐字做 NFKC + casefold：全角数字 / 字母（「１５００」）和模型引文里的半角写法对得上；
    一个字展开成几个字时（`İ` → `i̇`）每个都记原来那个位置，切回原文时不会错位。
    缓存：同一份资料要和每条事实比一次，以前每条都重新归一化整份资料。
    """
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        for norm in unicodedata.normalize("NFKC", ch).casefold():
            if norm.isalnum():
                chars.append(norm)
                index.append(i)
    return "".join(chars), tuple(index)


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


def keep_status_if_source_unchanged(facts: list[Fact], previous: StoryBible | None) -> None:
    """重新生成底稿后：沿用了 id 的事实，出处（收尾之后）变了才退回 unverified。

    以前在收尾之前比：旧出处已经是「模型自述」/ 资料原句，新出处是模型这次的原始引文
    （空串 / 改过字的引文），两边永远不等 —— 没有资料的项目重新生成一次，所有「已确认」都被打回。
    """
    old = {f.id: f for f in (previous.facts if previous else [])}
    for fact in facts:
        prior = old.get(fact.id)
        if prior is not None and _compact(prior.source) != _compact(fact.source):
            fact.status = "unverified"


def source_state(fact: Fact, material: str, *, min_chars: int) -> str:
    """检查用：`none`（没有资料，不查）/ `ok` / `short` / `missing`。"""
    material = material.strip()
    if not material:
        return "none"
    source = _compact(fact.source)
    if not source or source.startswith(SOURCE_SELF) or source.startswith(SOURCE_USER_PREFIX):
        return "missing"
    return match_quote(source, material, min_chars=min_chars)[0]


# ── 时长 ──────────────────────────────────────────────


def line_seconds(line: str, settings: Settings | None = None) -> int:
    """一句旁白（= 一个镜头）的时长：ceil(念白秒数 + 余量)。"""
    settings = settings or get_settings()
    return max(1, math.ceil(speech_seconds(line) + settings.explainer_line_pad_sec))


def scene_seconds(scene: Scene, settings: Settings | None = None) -> int:
    """一场的时长 = 各句旁白镜头时长之和（科普的场次时长由旁白决定，不由大纲预算决定）。"""
    total = sum(line_seconds(d.line, settings) for d in scene.dialogue if d.line.strip())
    return max(MIN_SCENE_DURATION_SEC, total)


def narration_seconds(script: Script, settings: Settings | None = None) -> float:
    """全片旁白念完要多久：念白秒数 + 句间停顿（不含取整余量，合成时按配音实长裁镜头）。"""
    settings = settings or get_settings()
    lines = [d.line for s in script.scenes for d in s.dialogue if d.line.strip()]
    return sum(speech_seconds(line) for line in lines) + settings.explainer_line_gap_sec * len(
        lines
    )


# ── 大纲 ──────────────────────────────────────────────

#: 知识点场数的上下限（开场提问和小结另算）
MIN_POINTS = 3
MAX_POINTS = 5

OUTLINE_SYSTEM = """你是科普讲解的结构编辑。把一个题目排成固定结构的分场大纲（不写旁白）：
1. 第一场：开场提问 —— 一个反常识的问题或现象，让观众想知道答案
2. 中间 3-5 场：每场一个知识点，先给直觉（日常看得到的现象、比喻），再讲原理；
   后一个知识点建立在前一个之上，用到的概念必须前面已经讲过
3. 最后一场：小结 —— 回答开场提出的问题

输出 JSON：
{"scenes": [{"title": "这一场的标题", "brief": "这一场讲什么，用到哪几条事实（写 fact id）"}]}
只输出 JSON。"""


#: 大纲场数不够时最多要几次
OUTLINE_ATTEMPTS = 2


def _format_facts(bible: StoryBible) -> str:
    rows = []
    for fact in bible.facts:
        flag = "（已驳回，不能用）" if fact.status == "rejected" else ""
        rows.append(f"- [{fact.id}] {fact.content}{flag}")
    return "\n".join(rows) or "（底稿里还没有事实）"


def _audience(project: Project) -> str:
    return " ".join(project.audience.split()) or "没有专业背景的成年人"


async def generate_outline(
    llm: LLMProvider, project: Project, bible: StoryBible
) -> ScriptOutline:
    lines = [
        f"## 题目\n{project.input_text}" if project.input_mode == InputMode.THEME else
        "## 用户写好的知识点（每个知识点一场，不增不删、不改顺序；你只补开场提问和小结）\n"
        f"{project.input_text}",
        f"\n目标观众：{_audience(project)}",
        tone_block(project),
        f"目标时长：{project.target_duration_sec} 秒",
        f"\n## 知识底稿\n{_format_facts(bible)}",
    ]
    messages = [
        LLMMessage(role="system", content=OUTLINE_SYSTEM),
        LLMMessage(role="user", content="\n".join(lines)),
    ]
    # 用户自己写了知识点：一个知识点一场、不增不删，只要求「提问 + 至少一个知识点 + 小结」。
    # 以前一律要求至少 MIN_POINTS 个知识点：用户只写了 2 个时，要么报错、
    # 要么逼模型违背用户的大纲去拆；超过 MAX_POINTS 个时多出来的被悄悄截掉
    user_points = project.input_mode == InputMode.OUTLINE
    need = 3 if user_points else MIN_POINTS + 2
    split_hint = (
        "请按用户写好的知识点逐个成场，补上开场提问和小结。"
        if user_points
        else "请把知识点拆开，重写完整大纲。"
    )
    raw: list[dict] = []
    # 场数不够时把要求告诉模型再要一次：同一份输入 gemini-3.8-flash 有时给 5 场、有时给 4 场
    for attempt in range(OUTLINE_ATTEMPTS):
        response = await llm.chat_json(messages, schema=OutlineResponse, temperature=0.5)
        raw = [s for s in response.scenes if isinstance(s, dict)]
        if len(raw) >= need:
            break
        logger.warning("科普大纲只有 %d 场（第 %d 次），要求重写", len(raw), attempt + 1)
        messages = [
            *messages,
            LLMMessage(role="assistant", content=json.dumps({"scenes": raw}, ensure_ascii=False)),
            LLMMessage(
                role="user",
                content=f"只有 {len(raw)} 场，不够：至少要 {need} 场"
                f"（1 场开场提问 + 至少 {need - 2} 场知识点 + 1 场小结）。" + split_hint,
            ),
        ]
    else:
        raise ValueError(
            f"科普大纲至少要 {need} 场（提问 + {need - 2} 个知识点 + 小结），"
            f"模型 {OUTLINE_ATTEMPTS} 次都只给了 {len(raw)} 场"
        )
    if not user_points and len(raw) > MAX_POINTS + 2:
        raw = [raw[0], *raw[1 : MAX_POINTS + 1], raw[-1]]
    budget = apply_duration_budget(project.target_duration_sec, len(raw), MIN_SCENE_DURATION_SEC)
    return ScriptOutline(
        target_duration_sec=project.target_duration_sec,
        scenes=[
            OutlineScene(
                id=f"scene_{i}",
                title=str(item.get("title") or f"第 {i} 场"),
                brief=str(item.get("brief") or item.get("summary") or ""),
                duration_budget_sec=budget,
            )
            for i, item in enumerate(raw, start=1)
        ],
    )


# ── 逐场旁白 ──────────────────────────────────────────

SCENE_SYSTEM = """你是科普旁白撰稿。你在逐场写一支科普讲解短片的旁白：
全片只有一个旁白声音，没有角色对白。

规则：
1. 每句旁白只讲一个意思，15-30 字，口语、念得顺。一句话就是一个镜头，每句都要能配一个画面
2. 每句标 fact_refs：这句话依据知识底稿里的哪几条事实（写 id）；纯过渡句（「我们来看看」）给空数组。
   不标事实的句子只能是提问、过渡或比喻：不能自己下结论、给数字，开场钩子也不能为了抓人把事实说重
   （底稿说「可能」，就不能写成「容易」「一定」「会」）
3. 只讲底稿里有的事实，「已驳回」的事实不能用。这句话需要、但底稿里没有的事实写进 new_facts
   （key 写 new_1、new_2……，在 fact_refs 里引用这个 key）；拿不准的不要写，不要编数字
4. 术语第一次出现时当场解释；难度按目标观众来
5. line 里只写要念出来的话：不写舞台提示、画面描述、「旁白：」这类前缀

输出 JSON：
{
  "heading": "这一场的标题",
  "summary": "一句话概括这一场",
  "emotion": "好奇 / 惊讶 / 恍然大悟……",
  "lines": [{"line": "旁白句", "fact_refs": ["fact_1"]}],
  "new_facts": [{"key": "new_1", "content": "事实", "source": "出处", "type": "general"}]
}
只输出 JSON。"""


def _new_fact_rule(project: Project, min_chars: int) -> str:
    if project.source_material.strip():
        return (
            "new_facts 的 source 必须逐字摘抄<原文>里的一句（至少 "
            f"{min_chars} 个字）；资料里没有依据的事实不要提"
        )
    return "new_facts 的 source 留空（系统会标成「模型自述」，由人工核对）"


def _prev_block(prev_scenes: list[Scene]) -> str:
    if not prev_scenes:
        return "（这是第一场）"
    prev = prev_scenes[-1]
    said = "\n".join(f"  {d.line}" for d in prev.dialogue[-3:])
    return f"上一场 [{prev.id}] {prev.heading}：{prev.summary}\n最后几句：\n{said}"


async def generate_scene(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    outline_scene: OutlineScene,
    prev_scenes: list[Scene],
    total_scenes: int,
    *,
    revision: Scene | None = None,
    feedback: str = "",
    settings: Settings | None = None,
) -> tuple[Scene, list[NewFact]]:
    """写一场旁白，返回 `(场次, 模型提出的新事实)`。新事实由调用方 `add_new_facts` 并进底稿。"""
    settings = settings or get_settings()
    budget = outline_scene.duration_budget_sec
    chars = int(budget * SPEECH_CHARS_PER_SEC * 0.9)
    material = project.source_material.strip()
    parts = [
        f"## 知识底稿（只读）\n{_format_facts(bible)}",
        f"\n目标观众：{_audience(project)}",
        tone_block(project),
        f"\n## 上下文\n{_prev_block(prev_scenes)}",
        "\n## 本场任务",
        f"场次 ID: {outline_scene.id}",
        f"标题: {outline_scene.title}",
        f"概述: {outline_scene.brief}",
        f"本场旁白合计不超过 {chars} 字（约 {budget} 秒，按每秒 {SPEECH_CHARS_PER_SEC} 字念）",
        f"这是第 {len(prev_scenes) + 1} 场，共 {total_scenes} 场",
        _new_fact_rule(project, settings.explainer_min_quote_chars),
    ]
    if material:
        excerpt = material[: settings.explainer_source_max_chars]
        parts.append(f"\n## 参考资料\n{quote_source(excerpt, guard=MATERIAL_GUARD)}")
    if revision is not None:
        said = "\n".join(f"  {d.line}" for d in revision.dialogue) or "  （无）"
        note = " ".join(str(feedback or "").split()) or "整体重写一版，讲得更清楚"
        parts.append(
            f"\n## 重写本场（按修改意见）\n当前版本：\n{said}\n\n修改意见（优先级最高）：{note}\n"
            "意见没提到的部分尽量保持。"
        )
    parts.append("\n请写本场旁白（JSON）。")
    messages = [
        LLMMessage(role="system", content=SCENE_SYSTEM),
        LLMMessage(role="user", content="\n".join(parts)),
    ]
    response = await llm.chat_json(
        messages, schema=ExplainerSceneResponse, temperature=0.5, max_tokens=4096
    )
    dialogue = []
    for item in response.lines:
        text = script_service.clean_dialogue_line(item.line)
        if text:
            dialogue.append(DialogueLine(speaker=NARRATOR, line=text, fact_refs=item.fact_refs))
    if not dialogue:
        raise ValueError(f"{outline_scene.id} 没有写出旁白")
    scene = Scene(
        id=outline_scene.id,
        heading=response.heading or outline_scene.title,
        summary=response.summary or outline_scene.brief,
        dialogue=dialogue,
        emotion=response.emotion,
    )
    scene.duration_sec = scene_seconds(scene, settings)
    return scene, response.new_facts


def add_new_facts(
    bible: StoryBible,
    new_facts: list[NewFact],
    material: str,
    settings: Settings | None = None,
) -> dict[str, str]:
    """把旁白阶段提出的新事实并进底稿（unverified），返回 `key → 正式 id`。

    内容和已有事实一字不差的不重复加，key 直接指向已有的那条。
    """
    settings = settings or get_settings()
    existing = {_compact(f.content): f.id for f in bible.facts}
    mapping: dict[str, str] = {}
    added: list[Fact] = []
    for item in new_facts:
        content = _compact(item.content)
        if not content:
            continue
        fact_id = existing.get(content)
        if fact_id is None:
            fact_id = f"fact_{next_fact_number(bible.facts + added)}"
            fact = Fact(id=fact_id, content=content, type=item.type or "general",
                        source=_compact(item.source))
            added.append(fact)
            existing[content] = fact_id
        if item.key:
            mapping[item.key.strip()] = fact_id
    verify_sources(added, material, min_chars=settings.explainer_min_quote_chars)
    bible.facts.extend(added)
    return mapping


def resolve_refs(scene: Scene, mapping: dict[str, str]) -> None:
    """把旁白里引用的临时 key（new_1）换成正式事实 id，去重保序。"""
    for line in scene.dialogue:
        refs: list[str] = []
        for ref in line.fact_refs:
            real = mapping.get(ref.strip(), ref.strip())
            if real and real not in refs:
                refs.append(real)
        line.fact_refs = refs


# ── 完整旁白：反向标注（不改字）──────────────────────

ANNOTATE_SYSTEM = """你是科普文稿标注员。用户已经写好了一支科普短片的完整旁白，按编号逐句给出。
你**不改任何一个字**，只做两件事：
1. 分场：按讲解结构（开场提问 / 每个知识点一场 / 小结）把句子分成几场，
   每场是编号连续的若干句，全部句子按顺序各用一次
2. 标注：给每句标 fact_refs（依据知识底稿里的哪几条事实）；句子陈述了、但底稿里没有的事实写进
   new_facts（key 写 new_1、new_2……，在 fact_refs 里引用这个 key）；纯过渡句给空数组

输出 JSON：
{
  "scenes": [{"title": "标题", "summary": "一句话概括",
              "lines": [{"index": 1, "fact_refs": ["fact_1"]}]}],
  "new_facts": [{"key": "new_1", "content": "事实", "source": "出处", "type": "general"}]
}
只输出 JSON。"""

_TERMINATORS = frozenset("。！？!?…")
_CLOSERS = frozenset("”」』’\"')）)]")
_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』"}
#: 句末标点后面整段都在括号里的片段（「天为什么是蓝的？（先想一想）」）：属于前一句，不单独成句
_BRACKET_TAIL_RE = re.compile(r"^\s*[（(][^）)]*[）)]\s*$")


def _ends_sentence(text: str, i: int) -> bool:
    """`text[i]` 是不是句末标点（英文句点要后面是空白 / 结尾、前面不是数字：3.14 不切）。"""
    ch = text[i]
    if ch in _TERMINATORS:
        return True
    if ch == ".":
        before = text[i - 1] if i else ""
        after = text[i + 1] if i + 1 < len(text) else ""
        return not before.isdigit() and (not after or after.isspace())
    return False


def _split_paragraph(paragraph: str) -> list[str]:
    """按句末标点切，引号里的句末标点不切（「“天是蓝的！”他说。」是一句）。

    句子原样拼回去就是原段落（只差句间空白）：开头的「……」这类标点并进后一句，不丢字。
    """
    sentences: list[str] = []
    quotes: list[str] = []
    start = i = 0
    while i < len(paragraph):
        ch = paragraph[i]
        if ch in _QUOTE_PAIRS:
            # 后面没有配对的闭引号（漏打 / 打成了另一个开引号）就不当引号：
            # 否则从这里到段尾都切不开，整段并成一句 = 一个超长镜头
            if _QUOTE_PAIRS[ch] in paragraph[i + 1 :]:
                quotes.append(_QUOTE_PAIRS[ch])
        elif quotes and ch == quotes[-1]:
            quotes.pop()
            # 引号里是一整句、引号外紧跟的不是正文（下一句引语 / 空白 / 段尾）：句子在这里结束
            after = paragraph[i + 1] if i + 1 < len(paragraph) else ""
            prev = paragraph[i - 1] if i else ""
            whole_quote = not after or after.isspace() or after in _QUOTE_PAIRS
            if not quotes and prev in _TERMINATORS and whole_quote:
                sentences.append(paragraph[start : i + 1])
                start = i + 1
        elif not quotes and _ends_sentence(paragraph, i):
            end = i + 1
            while end < len(paragraph) and (
                paragraph[end] in _TERMINATORS or paragraph[end] in _CLOSERS
            ):
                end += 1
            piece = paragraph[start:end]
            if any(c not in _TERMINATORS and c not in _CLOSERS and not c.isspace() for c in piece):
                sentences.append(piece)
                start = end
            i = end
            continue
        i += 1
    if paragraph[start:].strip():
        sentences.append(paragraph[start:])
    return sentences


def split_sentences(text: str) -> list[tuple[int, str]]:
    """完整旁白按句切开：`(段落序号, 句子)`。句子原样保留（只去掉首尾空白）。"""
    out: list[tuple[int, str]] = []
    paragraphs = [p for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    for index, paragraph in enumerate(paragraphs):
        sentences: list[str] = []
        for piece in _split_paragraph(paragraph):
            if sentences and _BRACKET_TAIL_RE.match(piece):
                sentences[-1] += piece
            elif piece.strip():
                sentences.append(piece)
        out.extend((index, sentence.strip()) for sentence in sentences)
    return out


def _grouping_from(response: AnnotateResponse, count: int) -> list[list[int]] | None:
    """模型给的分场能不能用：每句恰好一次、按顺序、每场连续。不能用返回 None。"""
    groups = [[line.index for line in scene.lines] for scene in response.scenes]
    flat = [i for group in groups for i in group]
    if not groups or any(not g for g in groups) or flat != list(range(1, count + 1)):
        return None
    return groups


async def annotate_full_script(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    settings: Settings | None = None,
) -> tuple[ScriptOutline, Script]:
    """完整旁白模式：用户的旁白原样进剧本，模型只负责分场和标 fact_refs。

    新事实直接并进 `bible`。模型的分场不合规（漏句、重句、乱序）时按用户的段落分场 ——
    宁可分得粗，也不能改动或丢掉用户的句子。
    """
    settings = settings or get_settings()
    sentences = split_sentences(project.input_text)
    if not sentences:
        raise ValueError("完整旁白是空的")
    numbered = "\n".join(f"{i}. {text}" for i, (_, text) in enumerate(sentences, start=1))
    user = (
        f"## 知识底稿\n{_format_facts(bible)}\n\n"
        f"{_new_fact_rule(project, settings.explainer_min_quote_chars)}\n\n"
        f"## 用户的旁白（共 {len(sentences)} 句）\n{numbered}"
    )
    messages = [
        LLMMessage(role="system", content=ANNOTATE_SYSTEM),
        LLMMessage(role="user", content=user),
    ]
    response = await llm.chat_json(
        messages, schema=AnnotateResponse, temperature=0.2, max_tokens=4096
    )
    mapping = add_new_facts(bible, response.new_facts, project.source_material, settings)
    refs = {line.index: line.fact_refs for scene in response.scenes for line in scene.lines}

    groups = _grouping_from(response, len(sentences))
    titles = [(s.title, s.summary) for s in response.scenes] if groups else []
    if groups is None:
        logger.warning("反向标注的分场不合规，按用户的段落分场")
        by_paragraph: dict[int, list[int]] = {}
        for i, (paragraph, _) in enumerate(sentences, start=1):
            by_paragraph.setdefault(paragraph, []).append(i)
        groups = list(by_paragraph.values())

    outline = ScriptOutline(target_duration_sec=project.target_duration_sec)
    script = Script(title=project.title, logline="")
    for n, group in enumerate(groups, start=1):
        title, summary = titles[n - 1] if titles else ("", "")
        scene = Scene(
            id=f"scene_{n}",
            heading=title or f"第 {n} 场",
            summary=summary or sentences[group[0] - 1][1],
            dialogue=[
                DialogueLine(speaker=NARRATOR, line=sentences[i - 1][1], fact_refs=refs.get(i, []))
                for i in group
            ],
        )
        resolve_refs(scene, mapping)
        scene.duration_sec = scene_seconds(scene, settings)
        script.scenes.append(scene)
        outline.scenes.append(
            OutlineScene(id=scene.id, title=scene.heading, brief=scene.summary,
                         duration_budget_sec=scene.duration_sec)
        )
    return outline, script


# ── 审稿 ──────────────────────────────────────────────

REVIEW_SYSTEM = """你是严格的科普审校。只找会让这支科普短片「讲错」或「讲不清」的问题：
1. 讲错了：旁白和它引用的事实不符，或者事实本身有明显的科学错误
2. 概念顺序倒置：用到了后面才解释的概念
3. 术语没解释：术语第一次出现时没有当场解释
4. 难度和目标观众不匹配
5. 结尾没有回答开场提出的问题
6. 没有依据的句子说过头：标着「（无依据）」的句子只该是提问、过渡或比喻。它如果下了结论、给了数字，
   或者把底稿里的「可能」说成「容易」「一定」「会」（开场钩子最常见），要报出来

要求：
- 只报确定的问题，不报文风偏好，不凑数；每条写明 scene_id
- severity: "error" = 讲错了 / 结尾没回答开场问题 / 无依据的句子改变了事实的意思；
  "warning" = 讲不清、顺序或难度问题 / 无依据的句子语气比底稿更绝对
- 最多 6 条，按严重程度排序

只输出 JSON：
{"issues": [{"severity": "error", "scene_id": "scene_2", "category": "accuracy",
             "message": "问题是什么", "suggestion": "怎么改"}]}"""


async def review_script(
    llm: LLMProvider, project: Project, script: Script, bible: StoryBible
) -> list[ConsistencyIssue]:
    """科普审稿：LLM 当第二双眼睛。它和写稿通常是同一类模型，不能代替人工核对事实。"""
    if not script.scenes:
        return []
    facts = {f.id: f.content for f in bible.facts}
    lines = [f"目标观众：{_audience(project)}"]
    if tone_of(project) == "meme":
        lines.append(
            "语气：玩梗。一听就是玩笑的夸张比喻、网络流行语不算问题；"
            "只看事实有没有被说错、说重（第 6 条照查）"
        )
    lines.append(f"\n## 知识底稿\n{_format_facts(bible)}")
    for scene in script.scenes:
        said = "\n".join(
            f"  - {d.line}"
            + (f"（依据 {', '.join(d.fact_refs)}）" if d.fact_refs else "（无依据）")
            for d in scene.dialogue
        )
        lines.append(f"\n## {scene.id}｜{scene.heading}\n{said}")
    cited = {r for s in script.scenes for d in s.dialogue for r in d.fact_refs}
    unknown = sorted(cited - set(facts))
    if unknown:
        lines.append(f"\n（注：{', '.join(unknown)} 不在底稿里，已由系统另行报错，不用再报）")
    return await script_service.ask_review(llm, REVIEW_SYSTEM, "\n".join(lines), script)


# ── 确定性检查 ────────────────────────────────────────

#: 全片旁白念完的时长超过目标多少倍就提示
LENGTH_WARN_RATIO = 1.2


def check_facts(
    script: Script, bible: StoryBible, material: str, *, min_chars: int
) -> list[ConsistencyIssue]:
    """旁白引用的事实：不存在 / 已驳回 → ERROR；未核对、出处找不到、引文过短 → WARNING。"""
    facts = {f.id: f for f in bible.facts}
    issues: list[ConsistencyIssue] = []
    cited: list[str] = []
    for scene in script.scenes:
        for line in scene.dialogue:
            for ref in line.fact_refs:
                fact = facts.get(ref)
                if fact is None:
                    issues.append(_issue(
                        IssueSeverity.ERROR, scene.id,
                        f"旁白引用了底稿里不存在的事实 {ref}：「{line.line[:20]}」",
                        "在知识底稿里补上这条事实，或改掉这句旁白的引用",
                    ))
                elif fact.status == "rejected":
                    issues.append(_issue(
                        IssueSeverity.ERROR, scene.id,
                        f"旁白引用了已驳回的事实 {ref}（{fact.content[:20]}）："
                        f"「{line.line[:20]}」",
                        "改写这句旁白，或重新核对这条事实",
                    ))
                if fact is not None and ref not in cited:
                    cited.append(ref)

    unverified = [ref for ref in cited if facts[ref].status == "unverified"]
    if unverified:
        issues.append(_issue(
            IssueSeverity.WARNING, "",
            f"{len(unverified)} 条被旁白引用的事实还没核对：{'、'.join(unverified)}",
            "在知识底稿面板里逐条确认或驳回",
        ))
    if material.strip():
        for ref in cited:
            state = source_state(facts[ref], material, min_chars=min_chars)
            if state == "missing":
                issues.append(_issue(
                    IssueSeverity.WARNING, "",
                    f"事实 {ref} 的出处在参考资料里找不到（{facts[ref].content[:20]}）",
                    "核对这条事实；资料里确实没有就驳回，或补上资料里的原句",
                ))
            elif state == "short":
                issues.append(_issue(
                    IssueSeverity.WARNING, "",
                    f"事实 {ref} 的引文太短（不到 {min_chars} 个字），几乎总能匹配上，不算核对过",
                    "把出处改成资料里完整的那一句",
                ))
    return issues


def check_length(
    script: Script, target_duration_sec: int, settings: Settings | None = None
) -> list[ConsistencyIssue]:
    """旁白总量 vs 目标时长。按镜头量语速没有意义（镜头时长本来就按字数算），要卡的是总量。"""
    if target_duration_sec <= 0:
        return []
    seconds = narration_seconds(script, settings)
    if seconds <= target_duration_sec * LENGTH_WARN_RATIO:
        return []
    return [_issue(
        IssueSeverity.WARNING, "",
        f"旁白念完约 {seconds:.0f} 秒，超过目标 {target_duration_sec} 秒的 "
        f"{LENGTH_WARN_RATIO:.0%}，成片会被拉长",
        "删句或合并知识点；确实要讲这么多就把目标时长调长",
        category="duration",
    )]


def check_speakers(script: Script, bible: StoryBible) -> list[ConsistencyIssue]:
    """科普默认纯旁白：出现已知角色的台词只提示（有讲解人物时是正常的）。

    未知说话人另由通用的说话人检查报 ERROR。
    """
    known = {c.id for c in bible.characters}
    issues: list[ConsistencyIssue] = []
    for scene in script.scenes:
        speakers = sorted({d.speaker for d in scene.dialogue if d.speaker in known})
        if speakers:
            issues.append(_issue(
                IssueSeverity.WARNING, scene.id,
                f"这一场有旁白以外的说话人：{'、'.join(speakers)}",
                "科普默认纯旁白；有讲解人物时可以忽略",
                category="name",
            ))
    return issues


def _issue(
    severity: IssueSeverity, scene_id: str, message: str, suggestion: str, *, category: str = "fact"
) -> ConsistencyIssue:
    return ConsistencyIssue(
        severity=severity, scene_id=scene_id, category=category, message=message,
        suggestion=suggestion,
    )


# ── 分镜：一句旁白一个镜头 ─────────────────────────────

#: 科普镜头里模型能改的字段（画面）。旁白、fact_refs、时长、id 由代码按句子定，重写分镜也不动它们
VISUAL_FIELDS = (
    "camera",
    "visual_description",
    "character_in_frame",
    "mood",
    "video_prompt",
    "location",
    "props_in_frame",
)

VISUALS_SYSTEM = """你是科普画面分镜师。一支科普讲解短片的旁白已经定稿，一句旁白就是一个镜头，
镜头的数量、顺序、时长都已经定了。你只负责给每个镜头设计画面：怎么把这句话拍成观众看得懂的画面。

每个镜头输出：
{
  "index": 1,
  "camera": "景别 · 运镜（如：特写 · 缓推 / 全景 · 固定）",
  "visual_description": "中文画面描述",
  "character_in_frame": [],
  "mood": "画面情绪（英文情绪词：curious / calm / amazed …）",
  "video_prompt": "subject + action + environment + lighting（全英文）",
  "location": "loc_id（知识底稿里的演示场景 id，没有合适的留空）",
  "props_in_frame": ["prop_1"]
}

要求：
- index 对应旁白句的编号，**每一句都要有、只能有一个镜头**：不许合并、拆分、跳过句子，也不要改写旁白
- 画面服务于这句话：优先用实物、日常现象、比喻、过程演示（慢动作、微距、剖面、示意性的动态）
- **不要让画面里出现知识性的文字**：公式、数据、图表标注、术语、字幕 —— 视频模型写错一个字就是讲错。
  video_prompt 里写明 no text, no labels, no captions；场景里自然出现的招牌、标语不受限制
- **物品上也不要有字和数字**：不要写带刻度、读数、计时标记、包装文字的东西
  （量杯刻度、仪表盘、水瓶上的毫升数），模型画出来的数字几乎都是错的；
  要表达「多少」就用对比（大杯 vs 小杯、满 vs 半满）
- **人和物的接触要写清楚**：喝、倒、接、拧这类动作，写明接触点和方向（"lips sealed around the jug's
  only opening at the top, jug tilted so the opening points down into the mouth"）。只写「豪饮」
  「猛灌」时，图片模型会把嘴贴在瓶身随便一处、再补画一个瓶口
- 画面里本来就不需要人时不要硬塞人；character_in_frame 只能填角色列表里已有的 id，没有就给空数组
- video_prompt 必须全英文，禁止出现 char_1 / loc_1 这类内部 id，背景写到具体的可见物、光从哪来
- 一个镜头只写一个连续的画面，不要在一句 video_prompt 里塞多个画面切换
- 相邻镜头的画面要连得上（同一个演示过程的前后两步、同一个地点换景别）
- 只输出 JSON: {"shots": [...]}"""


def shots_from_scene(
    scene: Scene, bible: StoryBible, settings: Settings | None = None
) -> list[Shot]:
    """一句旁白一个镜头：id、顺序、时长、旁白、fact_refs 都由代码定（确定性，模型碰不到）。

    旁白落 `narration`；有讲解人物（说话人是圣经里的角色）时那句落成对白。
    """
    known = {c.id for c in bible.characters}
    shots: list[Shot] = []
    for line in scene.dialogue:
        text = line.line.strip()
        if not text:
            continue
        index = len(shots) + 1
        speaker = line.speaker if line.speaker in known else None
        shots.append(
            Shot(
                id=f"{scene.id}_shot_{index}",
                scene_id=scene.id,
                shot_no=index,
                duration_sec=line_seconds(text, settings),
                narration=None if speaker else text,
                dialogue_text=text if speaker else None,
                dialogue_speaker=speaker,
                character_in_frame=[speaker] if speaker else [],
                fact_refs=list(line.fact_refs),
            )
        )
    return shots


def _shot_line(index: int, shot: Shot, *, with_visuals: bool) -> str:
    text = shot.narration or shot.dialogue_text or ""
    who = f"（{shot.dialogue_speaker} 说）" if shot.dialogue_speaker else ""
    row = f"{index}. {who}{text}（{shot.duration_sec} 秒）"
    if with_visuals and (shot.video_prompt or shot.visual_description):
        row += f"\n   当前画面：{shot.camera}；{shot.visual_description}；{shot.video_prompt}"
    return row


async def generate_visuals(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    scene: Scene,
    shots: list[Shot],
    *,
    feedback: str = "",
    target_shot: str = "",
    revision: bool = False,
) -> list[str]:
    """给这一场已经按句子排好的镜头填画面字段（原地改 `shots`），返回模型漏掉的镜头 id。

    `revision=True`：按 `feedback` 重写已有画面；再给 `target_shot` 时只改那一个镜头。
    漏掉的镜头 `video_prompt` 留空，分镜校验会报 ERROR，可以单镜重写补上。
    """
    if not shots:
        return []
    numbered = "\n".join(
        _shot_line(i, shot, with_visuals=revision) for i, shot in enumerate(shots, start=1)
    )
    target_index = next((i for i, s in enumerate(shots, start=1) if s.id == target_shot), 0)
    parts = [
        f"## 知识底稿里的演示场景与角色\n{storyboard_service.format_brief_bible(bible)}",
        f"\n目标观众：{_audience(project)}",
        tone_block(project, 2),
        (f"插图画风：{illustration_label(project)}（画面描述要能用这种画风画出来）"
         if uses_stills(project) else ""),
        "\n## 本场",
        f"场次: {scene.id}",
        f"标题: {scene.heading}",
        f"摘要: {scene.summary}",
        f"\n## 旁白（每句一个镜头，共 {len(shots)} 个）\n{numbered}",
        f"\n画面比例: {project.aspect_ratio.value} → {storyboard_service.aspect_hint(project)}",
    ]
    if revision:
        note = " ".join(str(feedback or "").split()) or "整体重画一版，画面更直观"
        parts.append(f"\n## 按修改意见重画\n修改意见（优先级最高）：{note}")
        if target_index:
            parts.append(f"只输出编号 {target_index} 这一个镜头，其余镜头只是给你看上下文。")
        parts.append("旁白不改；意见没提到的部分尽量保持。")
    parts.append('\n输出 JSON: {"shots": [...]}')
    messages = [
        LLMMessage(role="system", content=VISUALS_SYSTEM),
        LLMMessage(role="user", content="\n".join(parts)),
    ]
    response = await llm.chat_json(
        messages, schema=VisualsResponse, temperature=0.5, max_tokens=4096
    )

    drafts = list(response.shots)
    by_index = {d.index: d for d in drafts if d.index}
    if not by_index and len(drafts) == (1 if target_index else len(shots)):
        # 模型没写编号、但条数对得上：按顺序对应
        start = target_index or 1
        by_index = {start + i: d for i, d in enumerate(drafts)}
    wanted = [target_index] if target_index else range(1, len(shots) + 1)
    known_chars = {c.id for c in bible.characters}
    known_locs = {s.id for s in bible.settings}
    known_props = {p.id for p in bible.props}
    missing: list[str] = []
    for index in wanted:
        shot = shots[index - 1]
        draft = by_index.get(index)
        if draft is None:
            missing.append(shot.id)
            continue
        location = draft.location.strip()
        shot.camera = draft.camera
        shot.visual_description = draft.visual_description
        # 讲解人物说话的镜头：人一定在画面里（同短剧的「说话人补进画面」）
        chars = [c for c in draft.character_in_frame if c in known_chars]
        if shot.dialogue_speaker and shot.dialogue_speaker not in chars:
            chars.append(shot.dialogue_speaker)
        shot.character_in_frame = chars
        shot.mood = draft.mood
        shot.video_prompt = storyboard_service.sanitize_video_prompt(draft.video_prompt, bible)
        shot.location = location if location in known_locs else ""
        shot.props_in_frame = [p for p in draft.props_in_frame if p in known_props]
    if missing:
        logger.warning("科普分镜 %s：模型漏了 %d 个镜头的画面：%s", scene.id, len(missing),
                       "、".join(missing))
    return missing


# ── 配音文案模式 ────────────────────────────────────────

#: 科普只允许的配音文案模式：narration / polish / full 会补写或改写旁白，句子和 fact_refs 就对不上了
VOICE_SCRIPT_MODES = ("off", "perform")


def voice_script_mode(project: Project | None, mode: str) -> tuple[str, str]:
    """返回 `(实际模式, 告警)`。科普项目只认 off / perform，别的模式退回 perform；短剧原样返回。"""
    if not is_explainer(project) or mode in VOICE_SCRIPT_MODES:
        return mode, ""
    return "perform", (
        f"科普项目的配音文案只允许 off / perform（{mode} 会改写旁白，"
        "旁白和它引用的事实就对不上了），已按 perform 处理：只给念法、不改字"
    )


# ── AI 生成标识 ─────────────────────────────────────────

#: 片头显式标识停留多久（秒，变速前口径）。
#: 《人工智能生成合成内容标识办法》要求视频起始画面加显著提示
AI_LABEL_SEC = 3.0
_AI_LABEL_TEXT = {"zh": "本视频由 AI 生成", "en": "This video is AI-generated"}


def ai_label_text(project: Project | None, settings: Settings | None = None) -> str:
    """片头烧进画面的标识文字；不是科普项目或关了 `ai_label` 时返回空串。"""
    if not is_explainer(project) or not project.ai_label:
        return ""
    settings = settings or get_settings()
    return _AI_LABEL_TEXT.get(settings.language_for(project), _AI_LABEL_TEXT["zh"])


def ai_metadata(project: Project | None, settings: Settings | None = None) -> dict[str, str]:
    """隐式标识：写进 mp4 文件元数据（ffmpeg `-metadata`）。"""
    text = ai_label_text(project, settings)
    if not text:
        return {}
    note = f"AIGC: {text}（画面与旁白由 AI 生成）"
    return {"comment": note, "description": note}


# ── 批量放行 ────────────────────────────────────────────


def batch_hold_reasons(
    project: Project,
    bible: StoryBible | None,
    script: Script | None,
    issues: list[ConsistencyIssue],
    settings: Settings | None = None,
) -> list[str]:
    """批量链路里科普项目能不能跳过人工确认往下跑；返回停下来的理由（空 = 放行）。

    三条都要满足（docs/EXPLAINER_DESIGN.md 第 5 节）：有参考资料、旁白引用的事实全部通过出处校验、
    剧本检查（含 LLM 审稿）没有 ERROR。批量不该自动交付没人核对过的知识。
    """
    settings = settings or get_settings()
    reasons: list[str] = []
    material = project.source_material.strip()
    if not material:
        reasons.append("没有参考资料：事实全是模型自述，要人工在知识底稿里核对")
    else:
        facts = {f.id: f for f in (bible.facts if bible else [])}
        cited: list[str] = []
        for scene in script.scenes if script else []:
            for line in scene.dialogue:
                cited.extend(ref for ref in line.fact_refs if ref not in cited)
        failed = [
            ref
            for ref in cited
            if ref not in facts
            or source_state(facts[ref], material, min_chars=settings.explainer_min_quote_chars)
            != "ok"
        ]
        if failed:
            reasons.append(
                f"{len(failed)} 条被旁白引用的事实没通过出处校验：{'、'.join(failed[:5])}"
                f"{' …' if len(failed) > 5 else ''}"
            )
    errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
    if errors:
        reasons.append(f"剧本检查有 {len(errors)} 条 ERROR（含 LLM 审稿）：{errors[0].message}")
    return reasons


# ── 插图检查 ────────────────────────────────────────────


def check_illustrations(project: Project, storyboard: Storyboard | None) -> list[ConsistencyIssue]:
    """插图模式下讲事实的镜头画面只有 AI 示意图：提醒一次（不拦）。

    AI 示意图适合比喻、氛围、过程；真实事物和数据要换成真实图片或图表（设计文档第四期）。
    """
    if not uses_stills(project) or storyboard is None:
        return []
    factual = [shot.id for shot in storyboard.shots if shot.fact_refs]
    if not factual:
        return []
    return [_issue(
        IssueSeverity.WARNING, "",
        f"{len(factual)} 个讲事实的镜头画面是 AI 示意图（不是真实照片或数据图）",
        "比喻、氛围、过程演示用示意图没问题；画面要当证据的（具体的物种、天体、仪器、数据），"
        "暂时只能在分镜里改成不会被误认成真实照片的画面",
        category="storyboard",
    )]
