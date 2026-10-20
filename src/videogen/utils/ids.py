"""会被拼进文件名的 id（项目 / 角色 / 场景 / 道具 / 场次 / 镜头 / 段落）的格式。

镜头 id 会变成 `clips/<id>.mp4`，角色 id 会变成 `refs/<id>.png` 并被 `glob("<id>.*")` 清理。
id 里带 `../` 就能写到项目目录外，带 `*` 就能一次删掉一整个目录的图。
代码分配的 id（`char_1`、`scene_1_shot_2`）天然合规；需要拦的是用户通过 PUT 接口手写的 id。

只在写入边界（PUT 圣经 / 剧本 / 分镜）校验，不放进模型校验器：读盘时校验失败的文件会被隔离成
`.bad`，老项目里万一有不合规的 id，整个项目就打不开了。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["SAFE_ID_RE", "allocate_id", "is_safe_id", "unsafe_ids"]

#: 字母 / 数字 / 下划线开头，后面可以有连字符，最长 64 个字符（`\w` 含中文）
SAFE_ID_RE = re.compile(r"\w[\w-]{0,63}")


def is_safe_id(value: str) -> bool:
    return bool(value) and SAFE_ID_RE.fullmatch(value) is not None


def unsafe_ids(values: Iterable[str]) -> list[str]:
    """挑出不合规的 id（空串不算：可选字段留空是正常的）。"""
    return sorted({v for v in values if v and not is_safe_id(v)})


def allocate_id(prefix: str, existing: Iterable[str]) -> str:
    """下一个编号：已有最大号 + 1，**不复用空号**。

    复用空号的坑：删掉 `char_2` 之后它的定妆照 `refs/char_2.png` 还在盘上，新角色分到 `char_2`，
    「补登记孤儿图」就把被删角色的脸免费登记给了它。
    """
    pattern = re.compile(rf"{re.escape(prefix)}_(\d+)")
    top = max(
        (int(match.group(1)) for value in existing if (match := pattern.fullmatch(value))),
        default=0,
    )
    return f"{prefix}_{top + 1}"
