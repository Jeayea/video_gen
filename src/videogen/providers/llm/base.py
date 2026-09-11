from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class TokenUsage(BaseModel):
    """一次（或一段）调用的 token 统计，用于成本记账。"""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: TokenUsage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


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
    #: 累计使用量（由实现方在每次请求后调用 record_usage 累加）
    usage: TokenUsage

    # ── 使用量记账 ────────────────────────────────────

    def _usage_ref(self) -> TokenUsage:
        current = getattr(self, "usage", None)
        if not isinstance(current, TokenUsage):
            current = TokenUsage()
            self.usage = current
        return current

    def record_usage(
        self, *, input_tokens: int = 0, output_tokens: int = 0, calls: int = 1
    ) -> None:
        usage = self._usage_ref()
        usage.calls += max(0, calls)
        usage.input_tokens += max(0, input_tokens)
        usage.output_tokens += max(0, output_tokens)

    def take_usage(self) -> TokenUsage:
        """取走并清零累计量：按阶段记账用。"""
        usage = self._usage_ref()
        snapshot = usage.model_copy()
        self.usage = TokenUsage()
        return snapshot

    #: provider 写进成本账本的标识
    @property
    def name(self) -> str:
        return type(self).__name__

    def with_model(self, model: str) -> LLMProvider:
        """换模型（不支持切换的实现原样返回）。"""
        return self

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
