from __future__ import annotations

from videogen.config import get_settings
from videogen.providers.llm.base import LLMError, LLMJSONError, LLMMessage, LLMProvider
from videogen.providers.llm.openrouter import OpenRouterProvider, scrub_secrets

__all__ = [
    "LLMError",
    "LLMJSONError",
    "LLMMessage",
    "LLMProvider",
    "OpenRouterProvider",
    "get_llm",
    "scrub_secrets",
]


def get_llm() -> LLMProvider:
    """按配置构造 LLM provider。

    所有 service 都通过这个工厂拿 provider（而不是直接 new），
    这样测试可以注入 FakeLLM，后续换模型/换厂商也不用改业务代码。
    """
    settings = get_settings()
    provider = (settings.llm_provider or "openrouter").strip().lower()
    if provider in {"openrouter", "openai_compatible"}:
        return OpenRouterProvider()
    raise LLMError(f"未知的 LLM_PROVIDER：{provider}")
