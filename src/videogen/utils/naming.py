"""项目名称与项目目录名：从用户输入里取一个看得懂的名字。

目录名（= 项目 id，也出现在 URL 里）的规则：

    {建项日期 YYYYMMDD}-{名字}        例：20261010-孔乙己-第一场试拍-h3
    重名时加序号                       例：20261010-孔乙己-第一场试拍-h3-2

- 名字取用户填的标题；没填标题时从原文里取（小说取书名那一行，其它取第一句）
- 只保留字母 / 数字（含中文）和 `-` `_`，其余一律换成 `-`：没有 `.` `/`，不会路径穿越，
  放进 URL / HTML 属性也不用转义
- 日期在前，`ls projects/` 就是按建项时间排的
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["DEFAULT_TITLE", "derive_title", "project_slug", "slugify"]

DEFAULT_TITLE = "未命名项目"

#: 名字部分最多多少个字符（日期 + 序号另算，总长仍在 64 以内）
SLUG_MAX_CHARS = 40
TITLE_MAX_CHARS = 24

# 开头的说明性括注：「【本片为虚构作品】以下只是创作起点…」
_LEADING_NOTE = re.compile(r"^\s*[【\[（(][^】\]）)]{0,40}[】\]）)]\s*")
_SENTENCE_END = re.compile(r"[。！？!?；;\n]|[，,：:]")
_BOOK_MARKS = str.maketrans("", "", "《》〈〉「」『』\"'“”‘’")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().strip("　").strip()
        if line:
            return line
    return ""


def derive_title(title: str, input_text: str = "", input_mode: str = "") -> str:
    """用户给了标题就用标题；没给就从原文里取一个短名字。"""
    title = (title or "").strip()
    if title and title != DEFAULT_TITLE:
        return title
    text = _LEADING_NOTE.sub("", (input_text or "").strip())
    if input_mode == "novel":
        # 小说原文第一行通常就是书名（「孔乙己」「变形记」）
        candidate = _first_line(text)
    else:
        candidate = _SENTENCE_END.split(_first_line(text), maxsplit=1)[0]
    candidate = candidate.translate(_BOOK_MARKS).strip()
    if not candidate:
        return DEFAULT_TITLE
    if len(candidate) > TITLE_MAX_CHARS:
        candidate = candidate[:TITLE_MAX_CHARS].rstrip() + "…"
    return candidate


def slugify(text: str, max_chars: int = SLUG_MAX_CHARS) -> str:
    """任意文本 → 只含字母 / 数字（含中文）/ `-` / `_` 的片段；英文转小写。"""
    text = unicodedata.normalize("NFKC", text or "")
    out: list[str] = []
    for char in text:
        if char.isalnum() or char == "_":
            out.append(char.lower())
        else:
            out.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(out)).strip("-_")
    if len(slug) > max_chars:
        slug = slug[:max_chars].rstrip("-_")
    return slug


def project_slug(title: str, created_at: str, input_mode: str = "") -> str:
    """项目目录名的基础部分（不含重名序号）：`YYYYMMDD-名字`。"""
    date = re.sub(r"\D", "", (created_at or "")[:10])
    name = slugify(title) or input_mode or "project"
    return f"{date}-{name}" if len(date) == 8 else name
