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
    """一条对话消息。

    `content` 既可以是纯文本，也可以是 OpenAI 风格的多模态分片数组
    （`{"type": "text"}` / `{"type": "image_url"}`）—— 配音阶段要让模型
    「看着画面写台词」，就必须能把视频抽帧当图片发出去。
    """

    role: str  # "system" | "user" | "assistant"
    content: str | list[dict[str, Any]]


def text_message(role: str, text: str) -> LLMMessage:
    return LLMMessage(role=role, content=text)


def image_message(role: str, text: str, frames: list[tuple[str, str]]) -> LLMMessage:
    """把「一段正文 + 若干（说明, 图片 URL）」拼成一条多模态消息。

    图片按顺序插在正文后面，每张图前面可以带一句自己的说明
    （例如「镜头 scene_1_shot_2 的画面」）—— 不标清楚模型就会张冠李戴。
    """
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for label, url in frames:
        if label:
            parts.append({"type": "text", "text": label})
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return LLMMessage(role=role, content=parts)


def message_text(message: LLMMessage) -> str:
    """取出消息里的文字部分（忽略图片）—— 记账 / 假 provider / 日志用。"""
    content = message.content
    if isinstance(content, str):
        return content
    chunks = [
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(chunk for chunk in chunks if chunk)


def message_image_count(message: LLMMessage) -> int:
    """消息里带了几张图（测试与成本估算用）。"""
    content = message.content
    if isinstance(content, str):
        return 0
    return sum(
        1 for part in content if isinstance(part, dict) and part.get("type") == "image_url"
    )


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

    async def aclose(self) -> None:
        """释放 provider 持有的连接资源（默认没有，子类按需实现）。

        编排层在每个阶段结束后调用它 —— 与 video/tts provider 同一套生命周期，
        这样「复用连接」不会变成「泄漏连接」。
        """
        return None

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
