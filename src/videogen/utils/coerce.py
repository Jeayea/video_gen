from __future__ import annotations

import math
import re
from typing import Annotated, Any

from pydantic import BeforeValidator

__all__ = [
    "StrList",
    "coerce_int",
    "coerce_optional_int",
    "coerce_seconds",
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
    if isinstance(value, float) and not math.isfinite(value):
        return None  # json.loads 认 NaN / Infinity
    if isinstance(value, (int, float)):
        return round(value)
    return coerce_int(value, 0)


#: 时长里的「时:分:秒」/「分:秒」写法
_CLOCK_RE = re.compile(r"(?<![\d.])(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)(?![\d.])")
#: 带单位的时长片段：数字 + 小时 / 分钟 / 秒（中英文）
_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(小时|钟头|hours?|hrs?|h|分钟|分|minutes?|mins?|m(?!s)|秒钟|秒|seconds?|secs?|s)",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"小时": 3600, "钟头": 3600, "分钟": 60, "分": 60, "秒钟": 1, "秒": 1}


def _unit_seconds(unit: str) -> int:
    unit = unit.lower()
    if unit in _UNIT_SECONDS:
        return _UNIT_SECONDS[unit]
    if unit.startswith("h"):
        return 3600
    if unit.startswith("m"):
        return 60
    return 1


def coerce_seconds(value: Any, default: int) -> int:
    """时长字段的容错：在 `coerce_int` 之外还认单位（「1分30秒」「1.5分钟」「2min」「1:30」）。

    `coerce_int("1分30秒")` 只取第一个数 = 1 秒；时长字段要按单位换算成秒。
    范围（「10-15秒」）取前一个数，和 `coerce_int` 一致。
    """
    if not isinstance(value, str):
        return coerce_int(value, default)
    text = value.strip()
    clock = _CLOCK_RE.search(text)
    if clock:
        hours, minutes, seconds = clock.groups()
        return round(int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds))
    parts = list(_UNIT_RE.finditer(text))
    if parts and any(_unit_seconds(m.group(2)) > 1 for m in parts):
        total = 0.0
        last_scale = None
        for match in parts:
            scale = _unit_seconds(match.group(2))
            if last_scale is not None and scale >= last_scale:
                break  # 「1-2分钟」「5秒或10秒」这类：只取第一段
            total += float(match.group(1)) * scale
            last_scale = scale
        return round(total)
    return coerce_int(value, default)


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
