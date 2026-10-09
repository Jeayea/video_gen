"""小说原文按章切分（纯规则，不调 LLM、不花钱）。

切分顺序：
1. 认章标题（第X章/回/节/卷、序章/楔子/尾声/番外、Chapter N…），按标题切
2. 一个标题都认不出来：按段落凑够 `chunk_chars` 字切一段
3. 单章超过 `max_chars`：按段落再拆成几部分（一次喂给 LLM 的原文要有上限）

标题前的「文案/简介」够长才保留成一章（短的多半是作者的话，丢掉）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_NUM = r"[0-9０-９零〇一二两三四五六七八九十百千万]+"
_HEADING_RE = re.compile(
    rf"""^[ \t　]*(
        第{_NUM}[章回节卷集部篇]
      | 序章 | 序言 | 序幕 | 楔子 | 引子 | 尾声 | 终章 | 后记 | 番外
      | (?:chapter|CHAPTER|Chapter)[ \t]+(?:\d+|[IVXLC]+)
      | Prologue | Epilogue
    )
    # 正文里「第三节课下课了。」这类句子也以「第X节」开头：带句末标点/引号的不算标题
    (?![^\n]*[。！？!?…“”"])
    [^\n]{{0,40}}$""",
    re.MULTILINE | re.VERBOSE,
)
#: 标题前的引言至少这么长才单独成章
_PREFACE_MIN_CHARS = 200
#: 只有标题、正文太短的「章」（如紧跟着章标题的卷标题）直接丢弃
_EMPTY_CHAPTER_CHARS = 20


@dataclass
class RawChapter:
    title: str
    text: str


def source_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 连续空行压成一个，便于按段落切
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]


def _pack(paragraphs: list[str], limit: int) -> list[str]:
    """把段落按顺序装箱，每箱不超过 limit 字（单段超长就硬切）。"""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for para in paragraphs:
        while len(para) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(para[:limit])
            para = para[limit:]
        # 段落之间要补回换行，算长度时一并计入，否则成箱后会超出 limit
        if current and size + 1 + len(para) > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        size += len(para) + (1 if current else 0)
        current.append(para)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _by_headings(text: str) -> list[RawChapter]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return []
    chapters: list[RawChapter] = []
    preface = text[: matches[0].start()].strip()
    if len(preface) >= _PREFACE_MIN_CHARS:
        chapters.append(RawChapter(title="序", text=preface))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if len(body) < _EMPTY_CHAPTER_CHARS:
            continue
        chapters.append(RawChapter(title=match.group(0).strip(), text=body))
    return chapters


def split_chapters(
    text: str, *, chunk_chars: int = 5000, max_chars: int = 12000
) -> list[RawChapter]:
    """把小说原文切成章。返回的章顺序与原文一致；空文本返回空列表。"""
    text = _normalize(text)
    if not text:
        return []
    chunk_chars = max(500, chunk_chars)
    max_chars = max(chunk_chars, max_chars)

    chapters = _by_headings(text)
    if not chapters:
        chunks = _pack(_paragraphs(text), chunk_chars)
        chapters = [RawChapter(title=f"第 {i} 段", text=c) for i, c in enumerate(chunks, 1)]

    result: list[RawChapter] = []
    for chapter in chapters:
        if len(chapter.text) <= max_chars:
            result.append(chapter)
            continue
        parts = _pack(_paragraphs(chapter.text), max_chars)
        for i, part in enumerate(parts, 1):
            result.append(RawChapter(title=f"{chapter.title}（{i}/{len(parts)}）", text=part))
    return result
