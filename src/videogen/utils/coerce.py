from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BeforeValidator

__all__ = [
    "StrList",
    "coerce_int",
    "coerce_optional_int",
    "ensure_optional_str",
    "ensure_str_list",
]

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def coerce_int(value: Any, default: int) -> int:
    """把 LLM 输出的任意值容错成 int。

    覆盖 `7.5` / `"10"` / `"约10秒"` / `"第3场"` / `None` / 其他垃圾值。
    无法解析时返回 default，绝不抛异常 —— 单场字段不应该拖垮整个项目。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return round(value)
        except (OverflowError, ValueError):
            return default
    if isinstance(value, str):
        match = _NUMBER_RE.search(value)
        if match:
            try:
                return round(float(match.group(0)))
            except (OverflowError, ValueError):
                return default
        return default
    return default


def coerce_optional_int(value: Any) -> int | None:
    """同 coerce_int，但无法解析时返回 None（用于可空字段）。"""
    if value is None or (isinstance(value, str) and not _NUMBER_RE.search(value)):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(value)
    return coerce_int(value, 0)


def ensure_str_list(value: Any) -> list[str]:
    """把标量/None/字符串容错成 list[str]，并丢弃非标量元素。"""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            text = str(item)
        else:
            continue
        if text:
            out.append(text)
    return out


def ensure_optional_str(value: Any) -> str | None:
    """把 None / 空串 / "null" / "None" 归一成 None，其余转 str。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text.lower() in {"null", "none", "无"}:
            return None
        return text
    return str(value)


#: 用于 list[str] 字段，容忍 LLM 返回 null / 单个字符串
StrList = Annotated[list[str], BeforeValidator(ensure_str_list)]
