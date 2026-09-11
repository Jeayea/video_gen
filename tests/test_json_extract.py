from __future__ import annotations

import pytest

from tests.fakes import FlakyJSONProvider
from videogen.models.llm_io import SceneResponse
from videogen.providers.llm.base import (
    LLMJSONError,
    image_message,
    message_image_count,
    message_text,
    text_message,
)
from videogen.providers.llm.openrouter import OpenRouterProvider, _extract_json, scrub_secrets

VALID = {"a": 1, "b": [1, 2, 3]}


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("plain", '{"a": 1}'),
        ("fenced", '```json\n{"a": 1}\n```'),
        ("fenced_no_lang", '```\n{"a": 1}\n```'),
        ("prose_prefix", 'Here is the JSON you asked for:\n{"a": 1}'),
        ("prose_suffix", '{"a": 1}\nHope this helps!'),
        ("prose_both", 'Sure!\n```json\n{"a": 1}\n```\nLet me know.'),
        ("trailing_comma", '{"a": 1,}'),
        ("smart_quotes", '{\u201ca\u201d: 1}'),
        ("full_width_comma", '{"a": 1，}'),
        ("nested_with_brace_in_string", '{"a": "x{y}z", "b": 1}'),
        ("array_toplevel", "[1, 2, 3]"),
    ],
)
def test_extract_json_handles_real_world_noise(name: str, text: str) -> None:
    parsed = _extract_json(text)
    assert parsed is not None, name


def test_extract_json_exact_values() -> None:
    assert _extract_json('前言\n{"a": 1}\n后记') == {"a": 1}
    assert _extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    assert _extract_json('{"a": "x{y}z"}') == {"a": "x{y}z"}


def test_extract_json_raises_on_garbage() -> None:
    with pytest.raises(LLMJSONError):
        _extract_json("完全不是 JSON")


def test_extract_json_raises_on_empty() -> None:
    with pytest.raises(LLMJSONError):
        _extract_json("   ")


@pytest.mark.asyncio
async def test_chat_json_retries_with_error_feedback() -> None:
    """坏 JSON → 回灌错误重试 → 第二次拿到合法 JSON。"""
    provider = FlakyJSONProvider(
        bad_responses=["这不是 JSON", '{"dialogue": "这一行不是数组"}'],
        good_payload={"heading": "", "summary": "ok", "dialogue": [], "duration_sec": 7},
    )
    result = await provider.chat_json([], schema=SceneResponse)
    assert isinstance(result, SceneResponse)
    assert result.duration_sec == 7
    assert provider.attempts == 3  # 两次坏 + 一次好


@pytest.mark.asyncio
async def test_chat_json_gives_up_after_retries() -> None:
    provider = FlakyJSONProvider(bad_responses=["x", "y", "z"], good_payload={})
    with pytest.raises(LLMJSONError):
        await provider.chat_json([], schema=SceneResponse, retries=2)
    assert provider.attempts == 3


def test_scrub_secrets_removes_keys() -> None:
    # 用明显是假货的字符串，确保任何扫描器都不会把它当成真密钥
    dummy_a = "sk-or-v1-DUMMYKEYFORTEST0000000000"
    dummy_b = "sk-or-v1-DUMMYSHORT"
    text = f"failed with Authorization: Bearer {dummy_a} and api_key={dummy_b}"
    cleaned = scrub_secrets(text)
    assert dummy_a not in cleaned
    assert dummy_b not in cleaned
    assert "***" in cleaned


# ── 多模态消息（配音文案「看着画面写」靠它）─────────────


def test_image_message_orders_labels_and_images() -> None:
    message = image_message(
        "user",
        "正文",
        [("镜头 s1", "data:image/jpeg;base64,AAA"), ("镜头 s2", "data:image/jpeg;base64,BBB")],
    )
    parts = message.content
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "正文"}
    assert [p["text"] for p in parts if p["type"] == "text"] == ["正文", "镜头 s1", "镜头 s2"]
    assert message_image_count(message) == 2
    # 文字部分包含图片说明（记账/日志要看得到），顺序与图片一致
    assert message_text(message).splitlines() == ["正文", "镜头 s1", "镜头 s2"]


def test_image_message_skips_missing_urls() -> None:
    message = image_message(
        "user", "正文", [("镜头 s1", ""), ("镜头 s2", "data:image/jpeg;base64,BBB")]
    )
    assert message_image_count(message) == 1
    # 没有 URL 的说明文字仍然保留（模型要知道少了哪张图）
    assert message_text(message).splitlines() == ["正文", "镜头 s1", "镜头 s2"]


def test_message_text_passes_through_plain_string() -> None:
    message = text_message("system", "纯文本")
    assert message.content == "纯文本"
    assert message_text(message) == "纯文本"
    assert message_image_count(message) == 0


def test_openrouter_payload_serializes_vision_parts() -> None:
    """发出去的请求体必须是标准的 OpenAI 多模态结构（不然真实调用才炸）。"""
    provider = object.__new__(OpenRouterProvider)  # 只测 payload，不碰 key/网络
    provider.model = "test/model"
    message = image_message("user", "看看画面", [("镜头 s1", "data:image/jpeg;base64,AAA")])

    payload = provider._payload([text_message("system", "sys"), message], 0.5, 1024, True)

    assert payload["model"] == "test/model"
    assert payload["response_format"] == {"type": "json_object"}
    user = payload["messages"][1]
    assert user["role"] == "user"
    assert user["content"][0] == {"type": "text", "text": "看看画面"}
    assert user["content"][1] == {"type": "text", "text": "镜头 s1"}
    assert user["content"][2]["type"] == "image_url"
    assert user["content"][2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
