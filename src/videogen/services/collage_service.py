"""科普「手账拼贴」画风（`illustration_style=notebook`）：一帧 = 资料卡 + 手绘配图 + 手写批注。

参考的是「白纸 + 一手材料 + 红笔批注」那一路财经 / 科普视频：画面上的可信度来自**原文**，
不来自好看。这里的原文就是知识底稿里通过出处校验的那句话（`Fact.source`，资料里逐字存在），
所以「红框框住原文」可以完全由代码画：字一定对、不花钱、不经过图片模型。

- **资料卡**：镜头引用的第一条「有真实出处」的事实 → 在参考资料里找到那句原文 → 连同前后文排在
  一张白纸卡片上，原文用手抖的红框圈住；卡片标题取原文前面最近的 Markdown 标题（资料里写
  `## 论文名（arXiv 编号）`）
- **手绘配图**：图片模型画的马克笔线稿（白底），用正片叠底贴到纸上，白色融进纸色
- **手写批注**：分镜给的 `annotation`（一句口语短评），手写字体写在配图上方
- 没有可用出处的镜头（开场提问、过渡句）只有配图 + 批注

只用 Pillow 处理**自己生成的图**（图片模型的输出 + 代码画的卡片），不解析用户上传的文件。
Pillow 是可选依赖（`pip install -e '.[media]'`），这个模块只在画拼贴时才 import 它。
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import math
import random
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from videogen.config import Settings, get_settings
from videogen.models.bible import StoryBible
from videogen.models.project import Project
from videogen.models.storyboard import Shot
from videogen.services import explainer_service

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image as PILImage
    from PIL import ImageFont

logger = logging.getLogger(__name__)

__all__ = [
    "CANVAS_SIZES",
    "CollageSpec",
    "Excerpt",
    "build_spec",
    "doodle_aspect",
    "find_excerpt",
    "render",
]

#: 画布尺寸（和插图 provider 的输出一致）
CANVAS_SIZES = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}
PAPER = (246, 243, 235)
CARD = (253, 252, 248)
INK = (38, 38, 38)
RED = (214, 44, 38)
#: 底部留给字幕的高度（占画布高度）
SUBTITLE_SAFE = 0.17
#: 原文前后各带多少字的上下文（放不下时逐级缩短）
CONTEXT_STEPS = (260, 170, 100, 40, 0)
ANNOTATION_MAX_CHARS = 16

_HEADING_RE = re.compile(r"^#{1,3}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_SENTENCE_END = re.compile(r"[。！？!?；;]|\.(?=\s)")
_CJK = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")
#: 折行单位：英文按词（左括号 / 左引号跟着后面的词走，不单独落在行尾），中文按字
_TOKEN = re.compile(
    r"[(\[“‘\"']?[A-Za-z0-9][A-Za-z0-9'’\-.,;:%/()\[\]”\"]*\s*|\s+|.", re.S
)
#: 标题末尾的「(arXiv:2607.07663)」这类出处，单独印成一行小字
_TITLE_TAIL = re.compile(r"^(.*?)\s*[(（]([^()（）]{3,60})[)）]\s*$")

#: 字体候选（按顺序找第一个存在的文件；配置里给了路径就只用配置的）
_HAND_FONTS = (
    "~/Library/Fonts/ZCOOLKuaiLe-Regular.ttf",
    "/Library/Fonts/ZCOOLKuaiLe-Regular.ttf",
    "/usr/share/fonts/truetype/zcool/ZCOOLKuaiLe-Regular.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
_SERIF_FONTS = (
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
)
_SERIF_BOLD_FONTS = (
    "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
)
_CJK_SERIF_FONTS = (
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)


@dataclass
class Excerpt:
    """资料卡上要排的那一段：`text[quote_start:quote_end]` 是要框住的原文。"""

    title: str
    text: str
    quote_start: int
    quote_end: int


@dataclass
class CollageSpec:
    """一帧拼贴需要的全部输入（不含配图：配图由插图 provider 画完再交进来）。"""

    shot_id: str
    aspect_ratio: str
    annotation: str = ""
    #: 按上下文由长到短的候选（排版放不下时换下一个）
    excerpts: tuple[Excerpt, ...] = ()
    hand_font: str = ""
    serif_font: str = ""
    serif_bold_font: str = ""
    cjk_serif_font: str = ""

    @property
    def has_card(self) -> bool:
        return bool(self.excerpts)


# ── 找原文 ─────────────────────────────────────────────


def _squash(text: str) -> str:
    """压空白 + NFKC：PDF 抽出来的连字（`ﬀ` `ﬁ`）衬线字体里没有字形，会印成方框。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text))


def find_excerpt(material: str, quote: str, *, context_chars: int = 200) -> Excerpt | None:
    """在资料里找到这句原文，连同前后文切出一段（前后对齐到句子边界）。找不到返回 None。"""
    span = explainer_service.locate_quote(quote, material)
    if span is None:
        return None
    start, end = span
    lo = max(0, start - context_chars)
    if lo > 0:
        # 往后挪到下一个句子的开头，不从半句开始；窗口里没有句末就至少挪到词边界
        cut = _SENTENCE_END.search(material, lo, start)
        if cut:
            lo = cut.end()
        else:
            space = re.compile(r"\s").search(material, lo, start)
            lo = space.end() if space else start
    hi = min(len(material), end + context_chars)
    # 后文不跨进下一节：下一个标题之前就停（否则「## 下一节标题」会印在正文里）
    next_heading = _HEADING_RE.search(material, end, hi)
    if next_heading:
        hi = next_heading.start()
    elif hi < len(material):
        last = None
        for match in _SENTENCE_END.finditer(material, end, hi):
            last = match
        hi = last.end() if last else hi
    # 前文里的标题行去掉（标题单独印在卡片顶上）。要在压空白之前去：压成一行后整段都像标题
    before = _squash(_HEADING_RE.sub("", material[lo:start])).lstrip()
    body = _squash(material[start:end]).strip()
    after = _squash(material[end:hi]).rstrip()
    prefix = "… " if lo > 0 and before else ""
    suffix = " …" if hi < len(material) and after and not next_heading else ""
    text = f"{prefix}{before}{body}{after}{suffix}"
    quote_start = len(prefix) + len(before)
    return Excerpt(
        title=_title_before(material, start),
        text=text,
        quote_start=quote_start,
        quote_end=quote_start + len(body),
    )


def _title_before(material: str, position: int) -> str:
    title = ""
    for match in _HEADING_RE.finditer(material, 0, position):
        title = match.group(1).strip()
    return title


def build_spec(
    shot: Shot,
    project: Project,
    bible: StoryBible | None,
    *,
    settings: Settings | None = None,
) -> CollageSpec:
    """这一镜的拼贴：引用的第一条能在资料里找到原文的事实做资料卡。"""
    settings = settings or get_settings()
    material = str(project.source_material or "")
    facts = {fact.id: fact for fact in (bible.facts if bible else [])}
    excerpts: tuple[Excerpt, ...] = ()
    if material.strip():
        for ref in _by_relevance(shot, facts):
            fact = facts.get(ref)
            source = (fact.source if fact else "").strip()
            if not source or source.startswith(
                (explainer_service.SOURCE_SELF, explainer_service.SOURCE_USER_PREFIX)
            ):
                continue
            found = [find_excerpt(material, source, context_chars=n) for n in CONTEXT_STEPS]
            excerpts = tuple(e for e in found if e is not None)
            if excerpts:
                break
    return CollageSpec(
        shot_id=shot.id,
        aspect_ratio=project.aspect_ratio.value,
        annotation=clean_annotation(shot.annotation),
        excerpts=excerpts,
        hand_font=_pick_font(settings.collage_hand_font, _HAND_FONTS),
        serif_font=_pick_font(settings.collage_serif_font, _SERIF_FONTS),
        serif_bold_font=_pick_font("", _SERIF_BOLD_FONTS),
        cjk_serif_font=_pick_font("", _CJK_SERIF_FONTS),
    )


def _bigrams(text: str) -> set[str]:
    chars = [c for c in unicodedata.normalize("NFKC", text).lower() if c.isalnum()]
    return {a + b for a, b in itertools.pairwise(chars)}


def _by_relevance(shot: Shot, facts: dict) -> list[str]:
    """一句旁白引用了几条事实时，资料卡放和这句话最像的那条（按二元组重合排序，平手保持原顺序）。

    旁白常把「背景事实」排在前面（「在函数调用上反超官方」先引了分类学那条，再引 89% vs 67%），
    画面上该框的是这句话真正在说的那条。
    """
    said = _bigrams(shot.narration or shot.dialogue_text or "")
    refs = list(dict.fromkeys(shot.fact_refs))

    def overlap(ref: str) -> int:
        fact = facts.get(ref)
        return len(said & _bigrams(fact.content)) if fact else -1

    return sorted(refs, key=lambda ref: -overlap(ref))


def clean_annotation(text: str | None) -> str:
    """批注：一行口语短评，去掉引号和多余空白，太长截断（手写体大字，长了排不下）。"""
    value = " ".join(str(text or "").split()).strip("「」“”\"'")
    return value[:ANNOTATION_MAX_CHARS]


def doodle_aspect(spec: CollageSpec) -> str:
    """配图按什么比例画：有资料卡时配图只占一侧，画成方图；没有就用整个画布的比例。"""
    return "1:1" if spec.has_card else spec.aspect_ratio


def _pick_font(configured: str, candidates: tuple[str, ...]) -> str:
    if configured.strip():
        path = Path(configured).expanduser()
        return str(path) if path.is_file() else ""
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
    return ""


# ── 渲染 ───────────────────────────────────────────────


def unavailable_reason() -> str:
    """画不了拼贴的原因（空 = 能画）。要在**买配图之前**查：图买了再失败，钱就白花了。"""
    import importlib.util

    if importlib.util.find_spec("PIL") is None:
        return "手账拼贴画风要用 Pillow 排版，但没装：pip install -e '.[media]'"
    return ""


def render(spec: CollageSpec, doodle_path: Path, output_path: Path) -> Path:
    """画一帧拼贴（同步、CPU 活；调用方放进线程跑）。"""
    from PIL import Image

    width, height = CANVAS_SIZES.get(spec.aspect_ratio, CANVAS_SIZES["16:9"])
    rng = random.Random(int(hashlib.sha256(spec.shot_id.encode("utf-8")).hexdigest()[:8], 16))
    canvas = _paper(width, height, rng)
    landscape = width >= height
    content_bottom = int(height * (1 - SUBTITLE_SAFE))
    margin = int(min(width, height) * 0.05)

    doodle = Image.open(doodle_path).convert("RGB")
    if spec.has_card:
        if landscape:
            card_box = (margin, margin, int(width * 0.58), content_bottom)
            art_box = (int(width * 0.60), margin, width - margin, content_bottom)
        else:
            card_box = (margin, int(height * 0.06), width - margin, int(height * 0.46))
            art_box = (margin, int(height * 0.48), width - margin, content_bottom)
        card = _card(spec, card_box[2] - card_box[0], card_box[3] - card_box[1], rng)
        _paste_card(canvas, card, card_box, rng)
    else:
        card_box = None
        art_box = (margin * 2, margin, width - margin * 2, content_bottom)

    note_height = 0
    if spec.annotation:
        note_height = int(min(width, height) * (0.11 if landscape else 0.09))
    art_top = art_box[1] + note_height
    _paste_doodle(canvas, doodle, (art_box[0], art_top, art_box[2], art_box[3]))
    if spec.annotation:
        _annotate(canvas, spec, (art_box[0], art_box[1], art_box[2], art_top), rng)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")
    return output_path


def _paper(width: int, height: int, rng: random.Random) -> PILImage.Image:
    """米白纸：底色 + 一层很淡的低频斑驳（小噪声图放大，看起来像纸纤维）。"""
    from PIL import Image

    base = Image.new("RGB", (width, height), PAPER)
    tile_w, tile_h = max(8, width // 12), max(8, height // 12)
    noise = Image.frombytes("L", (tile_w, tile_h), rng.randbytes(tile_w * tile_h))
    noise = noise.resize((width, height), Image.Resampling.BICUBIC).convert("RGB")
    return Image.blend(base, noise, 0.035)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    from PIL import ImageFont

    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            logger.warning("字体加载失败，用 Pillow 自带字体：%s", path)
    return ImageFont.load_default(size)


@dataclass
class _Placed:
    line: int
    x0: float
    x1: float  # 不含尾随空格
    start: int
    end: int


def _layout(text: str, font, max_width: float) -> list[list[tuple[str, _Placed]]]:
    """按宽度折行：英文按词、中文按字。记下每个词在原文里的位置，框原文时用。"""
    lines: list[list[tuple[str, _Placed]]] = [[]]
    x = 0.0
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if token.isspace():
            if lines[-1]:
                x += font.getlength(" ")
            continue
        word = token.rstrip()
        width = font.getlength(word)
        if x > 0 and x + width > max_width:
            lines.append([])
            x = 0.0
        placed = _Placed(len(lines) - 1, x, x + width, match.start(), match.start() + len(word))
        lines[-1].append((word, placed))
        x += width
        if token != word:
            x += font.getlength(" ")
    return [line for line in lines if line]


def _card(spec: CollageSpec, box_w: int, box_h: int, rng: random.Random) -> PILImage.Image:
    """白纸资料卡：标题 + 正文（原文红框）。正文放不下就缩字号，再换更短的上下文。"""
    from PIL import Image, ImageDraw

    pad = int(box_w * 0.07)
    excerpt = spec.excerpts[0]
    # 标题里有中文（「PostTrainBench · 摘要」）时用中文字体：英文衬线体没有中文字形，会印成方框
    title_path = (
        spec.cjk_serif_font
        if _CJK.search(excerpt.title)
        else (spec.serif_bold_font or spec.serif_font)
    )
    title_font = _font(title_path, max(18, int(box_w * 0.034)))
    tail_font = _font(spec.serif_font, max(14, int(box_w * 0.024)))
    title, tail = excerpt.title, ""
    if match := _TITLE_TAIL.match(title):
        title, tail = match.group(1), match.group(2)
    title_lines: list[str] = []
    if title:
        placed = _layout(title, title_font, box_w - 2 * pad)
        title_lines = [" ".join(word for word, _ in line) for line in placed]
        if len(title_lines) > 2:
            title_lines = [title_lines[0], title_lines[1].rstrip(" :,") + " …"]
    title_h = int(len(title_lines) * title_font.size * 1.35)
    if tail:
        title_h += int(tail_font.size * 1.5)
    body_top = pad + (title_h + int(box_w * 0.03) if title_lines else 0)
    body_room = box_h - body_top - pad

    chosen = None
    for candidate in spec.excerpts:
        cjk = len(_CJK.findall(candidate.text)) > len(candidate.text) * 0.3
        font_path = spec.cjk_serif_font if cjk else (spec.serif_font or spec.cjk_serif_font)
        for size in range(int(box_w * 0.04), max(15, int(box_w * 0.026)) - 1, -2):
            font = _font(font_path, size)
            lines = _layout(candidate.text, font, box_w - 2 * pad)
            if len(lines) * size * 1.55 <= body_room:
                chosen = (candidate, font, lines, size)
                break
        if chosen:
            break
    if chosen is None:  # 最短的候选、最小的字号也放不下：照排，裁掉放不下的行
        candidate = spec.excerpts[-1]
        font = _font(spec.serif_font or spec.cjk_serif_font, max(15, int(box_w * 0.026)))
        lines = _layout(candidate.text, font, box_w - 2 * pad)
        chosen = (candidate, font, lines, font.size)
    candidate, font, lines, size = chosen
    line_h = size * 1.55
    max_lines = max(1, int(body_room // line_h))
    lines = lines[:max_lines]
    used_h = len(lines) * line_h
    card_h = min(box_h, int(body_top + used_h + pad))

    card = Image.new("RGBA", (box_w, card_h), (*CARD, 255))
    draw = ImageDraw.Draw(card)
    y = pad
    for text in title_lines:
        w = title_font.getlength(text)
        draw.text(((box_w - w) / 2, y), text, font=title_font, fill=INK)
        y += title_font.size * 1.35
    if tail:
        w = tail_font.getlength(tail)
        draw.text(((box_w - w) / 2, y + tail_font.size * 0.2), tail, font=tail_font,
                  fill=(120, 120, 120))
        y += tail_font.size * 1.5
    if title_lines:
        rule_y = int(y + box_w * 0.012)
        draw.line([(pad, rule_y), (box_w - pad, rule_y)], fill=(170, 170, 170), width=1)

    for index, line in enumerate(lines):
        top = body_top + index * line_h
        for word, placed in line:
            draw.text((pad + placed.x0, top), word, font=font, fill=(52, 52, 52))
    # 原文红框：每一行一个框（和手工在打印稿上框字一样）
    for index, line in enumerate(lines):
        hits = [
            placed
            for _, placed in line
            if placed.end > candidate.quote_start and placed.start < candidate.quote_end
        ]
        if not hits:
            continue
        top = body_top + index * line_h
        box = (
            pad + hits[0].x0 - size * 0.25,
            top - size * 0.18,
            pad + hits[-1].x1 + size * 0.25,
            top + size * 1.28,
        )
        _wobbly_box(draw, box, rng, width=max(3, size // 9))
    return card


def _wobbly_box(draw, box: tuple[float, float, float, float], rng: random.Random, width: int):
    """手抖的红框：四条边各自一笔，带轻微起伏和出头。"""
    x0, y0, x1, y1 = box
    jitter = max(1.5, width * 0.6)
    overshoot = width * 2.5
    edges = (
        ((x0 - overshoot, y0), (x1 + overshoot * 0.4, y0)),
        ((x1, y0 - overshoot * 0.4), (x1, y1 + overshoot)),
        ((x1 + overshoot * 0.3, y1), (x0 - overshoot * 0.2, y1)),
        ((x0, y1 + overshoot * 0.2), (x0, y0 - overshoot)),
    )
    for (ax, ay), (bx, by) in edges:
        length = math.hypot(bx - ax, by - ay)
        steps = max(2, int(length / 45))
        points = []
        for k in range(steps + 1):
            t = k / steps
            dx = rng.uniform(-jitter, jitter) if 0 < k < steps else 0.0
            points.append((ax + (bx - ax) * t + (dx if ay == by else 0),
                           ay + (by - ay) * t + (dx if ax == bx else 0)))
        draw.line(points, fill=RED, width=width, joint="curve")


def _paste_card(canvas, card, box: tuple[int, int, int, int], rng: random.Random) -> None:
    """卡片带一点倾斜和软阴影贴到纸上（像把打印稿摊在桌上）。"""
    from PIL import Image, ImageFilter

    angle = rng.uniform(-1.2, 1.2)
    rotated = card.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    shadow = Image.new("RGBA", rotated.size, (0, 0, 0, 0))
    alpha = rotated.getchannel("A").point(lambda v: int(v * 0.28))
    shadow.putalpha(alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    x = box[0] + max(0, (box[2] - box[0] - rotated.width) // 2)
    y = box[1] + max(0, (box[3] - box[1] - rotated.height) // 2)
    canvas.paste(shadow, (x + 8, y + 10), shadow)
    canvas.paste(rotated, (x, y), rotated)


def _paste_doodle(canvas, doodle, box: tuple[int, int, int, int]) -> None:
    """配图按比例缩进区域，用正片叠底贴上：白底融进纸色，线条照旧是黑的。"""
    from PIL import Image, ImageChops

    box_w, box_h = box[2] - box[0], box[3] - box[1]
    if box_w <= 0 or box_h <= 0:
        return
    scale = min(box_w / doodle.width, box_h / doodle.height)
    size = (max(1, int(doodle.width * scale)), max(1, int(doodle.height * scale)))
    art = doodle.resize(size, Image.Resampling.LANCZOS)
    x = box[0] + (box_w - size[0]) // 2
    y = box[1] + (box_h - size[1]) // 2
    region = canvas.crop((x, y, x + size[0], y + size[1]))
    canvas.paste(ImageChops.multiply(region, art), (x, y))


def _annotate(canvas, spec: CollageSpec, box: tuple[int, int, int, int], rng) -> None:
    """手写批注：手写字体、深色墨水、微微倾斜，下面一笔红色波浪线。"""
    from PIL import Image, ImageDraw

    box_w, box_h = box[2] - box[0], box[3] - box[1]
    if box_w <= 0 or box_h <= 0:
        return
    size = int(box_h * 0.62)
    font = _font(spec.hand_font, size)
    while size > 18 and font.getlength(spec.annotation) > box_w * 0.95:
        size -= 2
        font = _font(spec.hand_font, size)
    text_w = int(font.getlength(spec.annotation))
    layer = Image.new("RGBA", (text_w + size, int(size * 1.7)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text((size // 2, 0), spec.annotation, font=font, fill=(*INK, 255))
    base_y = int(size * 1.28)
    points = [
        (size // 2 + k * text_w / 12, base_y + (4 if k % 2 else -2) * rng.uniform(0.6, 1.2))
        for k in range(13)
    ]
    draw.line(points, fill=(*RED, 255), width=max(3, size // 14), joint="curve")
    layer = layer.rotate(rng.uniform(-3.0, 2.0), resample=Image.Resampling.BICUBIC, expand=True)
    x = box[0] + (box_w - layer.width) // 2
    y = box[1] + (box_h - layer.height) // 2
    canvas.paste(layer, (x, y), layer)
