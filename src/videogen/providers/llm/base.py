from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class LLMError(RuntimeError):
    """LLM 调用失败的统一异常。

    约定：消息必须经过脱敏，绝不包含 API key / Authorization 头。
    """


class LLMJSONError(LLMError):
    """LLM 输出无法解析为合法 JSON 或不符合 schema。"""


class LLMMessage(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        """发送对话请求，返回文本回复。"""

    @abstractmethod
    async def chat_json(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel] | None = None,
        temperature: float = 0.5,
        max_tokens: int = 8192,
        retries: int = 2,
    ) -> Any:
        """发送对话请求并解析 JSON。

        - 解析失败或不符合 `schema` 时，把错误回灌给模型重试（最多 retries 次）
        - `schema` 为空时返回 dict；否则返回校验通过的 schema 实例
        - 最终仍失败时抛 `LLMJSONError`
        """
