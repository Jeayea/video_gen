"""OpenRouter 真实图片 provider（角色参考图）。

全部用 `httpx.MockTransport` 打桩：**不发真实请求、不花一分钱**，
但请求体、base64 落盘、`usage.cost` 记账口径、错误映射都按真实 API 形状验证。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from videogen.providers.image.base import (
    ImageArtifactError,
    ImageProviderError,
    ImageRequest,
)
from videogen.providers.image.openrouter import OpenRouterImageProvider

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"real-looking-bytes"
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


def make_provider(handler, **kwargs) -> OpenRouterImageProvider:
    return OpenRouterImageProvider(
        api_key="test-key",
        model="bytedance-seed/seedream-4.5",
        base_url="https://openrouter.test/api/v1",
        max_http_retries=kwargs.pop("max_http_retries", 1),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def ok_response(cost: float | None = 0.04, media_type: str = "image/png") -> httpx.Response:
    body: dict[str, object] = {
        "created": 1748372400,
        "data": [{"b64_json": PNG_B64, "media_type": media_type}],
    }
    if cost is not None:
        body["usage"] = {"prompt_tokens": 0, "completion_tokens": 4175, "cost": cost}
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_generate_sends_expected_payload_and_writes_file(tmp_path):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return ok_response()

    provider = make_provider(handler)
    output = tmp_path / "char_1.png"
    result = await provider.generate(
        ImageRequest(
            subject_id="char_1",
            prompt="Character reference portrait of 林悦",
            output_path=output,
            aspect_ratio="1:1",
            resolution="2K",
        )
    )
    await provider.aclose()

    assert seen["url"] == "https://openrouter.test/api/v1/images"
    assert seen["auth"] == "Bearer test-key"
    payload = seen["json"]
    assert payload["model"] == "bytedance-seed/seedream-4.5"
    assert payload["n"] == 1
    assert payload["aspect_ratio"] == "1:1"
    assert payload["resolution"] == "2K"
    assert payload["prompt"].startswith("Character reference portrait")
    assert "input_references" not in payload  # 没给参考图就别带

    assert result.path.read_bytes() == PNG_BYTES
    assert result.actual_cost_usd == 0.04


@pytest.mark.asyncio
async def test_reference_image_is_sent_as_data_url(tmp_path):
    reference = tmp_path / "ref.png"
    reference.write_bytes(PNG_BYTES)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return ok_response()

    provider = make_provider(handler)
    await provider.generate(
        ImageRequest(
            subject_id="char_1",
            prompt="another angle",
            output_path=tmp_path / "out.png",
            reference_image=reference,
        )
    )
    await provider.aclose()

    refs = seen["json"]["input_references"]
    assert len(refs) == 1
    assert refs[0]["type"] == "image_url"
    assert refs[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_missing_reference_file_is_ignored(tmp_path):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return ok_response()

    provider = make_provider(handler)
    await provider.generate(
        ImageRequest(
            subject_id="char_1",
            prompt="x",
            output_path=tmp_path / "out.png",
            reference_image=tmp_path / "nope.png",
        )
    )
    await provider.aclose()
    assert "input_references" not in seen["json"]


@pytest.mark.asyncio
async def test_media_type_decides_suffix(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response(media_type="image/jpeg")

    provider = make_provider(handler)
    result = await provider.generate(
        ImageRequest(subject_id="c", prompt="x", output_path=tmp_path / "char.png")
    )
    await provider.aclose()
    assert result.path.suffix == ".jpg"
    assert result.path.read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_missing_usage_cost_returns_none(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response(cost=None)

    provider = make_provider(handler)
    result = await provider.generate(
        ImageRequest(subject_id="c", prompt="x", output_path=tmp_path / "c.png")
    )
    await provider.aclose()
    assert result.actual_cost_usd is None  # 由上层按单价估算


@pytest.mark.asyncio
async def test_retryable_status_is_retried_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="upstream failed")
        return ok_response()

    provider = make_provider(handler, max_http_retries=2)
    result = await provider.generate(
        ImageRequest(subject_id="c", prompt="x", output_path=tmp_path / "c.png")
    )
    await provider.aclose()
    assert calls["n"] == 2
    assert result.path.is_file()


@pytest.mark.asyncio
async def test_client_error_is_not_retried_and_message_is_scrubbed(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request: model not found")

    provider = make_provider(handler, max_http_retries=3)
    with pytest.raises(ImageProviderError, match="HTTP 400"):
        await provider.generate(
            ImageRequest(subject_id="c", prompt="x", output_path=tmp_path / "c.png")
        )
    await provider.aclose()
    assert calls["n"] == 1  # 4xx 不重试（花不了钱也不该反复打）


@pytest.mark.asyncio
async def test_empty_data_is_rejected(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    provider = make_provider(handler)
    with pytest.raises(ImageProviderError, match="没有返回任何图片"):
        await provider.generate(
            ImageRequest(subject_id="c", prompt="x", output_path=tmp_path / "c.png")
        )
    await provider.aclose()


@pytest.mark.asyncio
async def test_broken_base64_is_artifact_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": "!!!not-base64!!!"}]})

    provider = make_provider(handler)
    with pytest.raises(ImageArtifactError) as excinfo:
        await provider.generate(
            ImageRequest(subject_id="c", prompt="x", output_path=tmp_path / "c.png")
        )
    await provider.aclose()
    assert excinfo.value.retryable is False  # 已经生成过了，重试等于再买一张


@pytest.mark.asyncio
async def test_network_error_is_wrapped_and_retried(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    provider = make_provider(handler, max_http_retries=2)
    with pytest.raises(ImageProviderError, match="图片请求失败"):
        await provider.generate(
            ImageRequest(subject_id="c", prompt="x", output_path=tmp_path / "c.png")
        )
    await provider.aclose()
    assert calls["n"] == 2


def test_provider_requires_api_key(monkeypatch):
    from videogen.config import get_settings

    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")
    with pytest.raises(ImageProviderError, match="OPENROUTER_API_KEY"):
        OpenRouterImageProvider()


@pytest.mark.asyncio
async def test_read_timeout_after_sending_is_not_resent(tmp_path):
    """请求发出去后读超时：图可能已经画完、扣了费 —— 不重发，也不让上层重试（以前会再买一张）。"""
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    provider = make_provider(handler, max_http_retries=3)
    with pytest.raises(ImageProviderError) as exc:
        await provider.generate(
            ImageRequest(subject_id="c", prompt="p", output_path=tmp_path / "c.png")
        )
    await provider.aclose()
    assert len(posts) == 1
    assert exc.value.retryable is False
