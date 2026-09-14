"""LLM provider 的 HTTP 层（`providers/llm/openrouter.py`）。

全部用 `httpx.MockTransport` 打桩：不联网、不花钱。
这一批用例是 review 提的两个问题的回归：

1. **每次 chat 都新建 `AsyncClient`** —— 分镜/配音文案是成批调用（一场一次），
   每次都重新握手 TLS。现在实例持有 client 并复用，用完由编排层 `aclose()`。
2. **中文标点替换会改写台词** —— `_repair_json` 见 `tests/test_json_extract.py`。
"""

from __future__ import annotations

import json

import httpx
import pytest

from videogen.providers.llm.base import LLMError, LLMMessage
from videogen.providers.llm.openrouter import OpenRouterProvider


def make_provider(handler, **kwargs) -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key="test-key",
        model="google/gemini-2.5-flash",
        base_url="https://openrouter.test/api/v1",
        timeout_sec=5.0,
        max_retries=kwargs.pop("max_retries", 1),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def ok(body: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": body}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )


def ask(provider: OpenRouterProvider) -> str:
    import asyncio

    return asyncio.run(provider.chat([LLMMessage(role="user", content="hi")]))


# ── 连接复用与生命周期 ─────────────────────────────────


def make_owned_provider(monkeypatch, handler, **kwargs) -> tuple[OpenRouterProvider, list]:
    """构造一个「自己持有 client」的 provider（不注入），并数它建了几个 client。

    真实路径就是这条：`get_llm()` → `OpenRouterProvider()` → 第一次 chat 时懒建 client。
    """
    created: list[httpx.AsyncClient] = []
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs_):
        client = real_client(transport=transport)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    provider = OpenRouterProvider(
        api_key="test-key",
        model="google/gemini-2.5-flash",
        base_url="https://openrouter.test/api/v1",
        timeout_sec=5.0,
        max_retries=kwargs.pop("max_retries", 0),
        **kwargs,
    )
    return provider, created


def test_client_is_reused_across_calls(monkeypatch):
    """同一个 provider 连打两次只应该建一个 client（连接池复用）。"""
    import asyncio

    async def handler(request):
        return ok()

    async def run():
        provider, created = make_owned_provider(monkeypatch, handler)
        await provider.chat([LLMMessage(role="user", content="1")])
        first = provider._client
        await provider.chat([LLMMessage(role="user", content="2")])
        assert provider._client is first
        assert len(created) == 1, "第二次 chat 不该再建 client"
        await provider.aclose()
        assert provider._client is None
        assert first is not None and first.is_closed

        # 关掉之后再调用会重建一个（aclose 之后仍可用）
        await provider.chat([LLMMessage(role="user", content="3")])
        assert len(created) == 2
        await provider.aclose()

    asyncio.run(run())


def test_injected_client_is_not_closed_by_provider():
    """外部注入的 client（测试/宿主应用）不归 provider 关闭。"""
    import asyncio

    async def handler(request):
        return ok()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = make_provider(handler)
    provider._client = client
    provider._owns_client = False
    asyncio.run(provider.aclose())
    assert not client.is_closed, "注入的 client 不该被 provider 关掉"
    asyncio.run(client.aclose())


def test_owned_client_is_closed(monkeypatch):
    import asyncio

    async def handler(request):
        return ok()

    async def run():
        provider, created = make_owned_provider(monkeypatch, handler)
        await provider.chat([LLMMessage(role="user", content="hi")])
        assert provider._owns_client is True
        await provider.aclose()
        assert provider._client is None
        assert created and created[0].is_closed

    asyncio.run(run())


def test_with_model_shares_client_without_owning_it():
    import asyncio

    async def handler(request):
        return ok()

    async def run():
        provider = make_provider(handler)
        await provider.chat([LLMMessage(role="user", content="hi")])
        switched = provider.with_model("anthropic/claude-3.5-haiku")
        assert switched.model == "anthropic/claude-3.5-haiku"
        assert switched._client is provider._client
        assert switched._owns_client is False
        # 换模型不影响原实例
        assert provider.with_model("") is provider
        await switched.aclose()
        assert provider._client is not None, "子实例不该把父实例的连接关掉"
        await provider.aclose()

    asyncio.run(run())


# ── 重试与降级 ─────────────────────────────────────────


def test_retries_on_429_then_succeeds(monkeypatch):
    import asyncio

    calls: list[dict] = []

    async def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(429, text="rate limited")
        return ok("第二次成功")

    async def run():
        provider = make_provider(handler, max_retries=2)
        provider._backoff = staticmethod(lambda attempt: asyncio.sleep(0))  # type: ignore[method-assign]
        content = await provider.chat([LLMMessage(role="user", content="hi")])
        assert content == "第二次成功"
        assert len(calls) == 2

    asyncio.run(run())


def test_response_format_fallback():
    import asyncio

    seen: list[dict] = []

    async def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, text="response_format is not supported")
        return ok('{"ok": true}')

    async def run():
        provider = make_provider(handler)
        content = await provider.chat(
            [LLMMessage(role="user", content="hi")], json_mode=True
        )
        assert content == '{"ok": true}'
        assert "response_format" in seen[0]
        assert "response_format" not in seen[1], "应该去掉 response_format 再试一次"

    asyncio.run(run())


def test_error_message_is_redacted():
    import asyncio

    key = "sk-or-v1-" + "f" * 64

    async def handler(request):
        return httpx.Response(401, text=f"invalid api key: {key}")

    async def run():
        provider = make_provider(handler)
        with pytest.raises(LLMError) as excinfo:
            await provider.chat([LLMMessage(role="user", content="hi")])
        assert key not in str(excinfo.value)
        assert "401" in str(excinfo.value)

    asyncio.run(run())


def test_network_error_is_wrapped():
    import asyncio

    async def handler(request):
        raise httpx.ConnectError("boom")

    async def run():
        provider = make_provider(handler, max_retries=0)
        with pytest.raises(LLMError, match="网络请求失败"):
            await provider.chat([LLMMessage(role="user", content="hi")])

    asyncio.run(run())
