from __future__ import annotations

import re

__all__ = ["scrub_secrets"]

#: 常见的密钥形态。所有对外抛出的异常、日志、接口响应都过这里。
_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
)


def scrub_secrets(text: str) -> str:
    """把任何疑似密钥的内容替换掉。

    用于日志、异常消息、任务错误、前端响应，避免密钥外泄。
    """
    out = text or ""
    for pattern in _PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", out)
    return out
