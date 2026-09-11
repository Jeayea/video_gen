from __future__ import annotations

import pytest

from tests.fakes import FlakyJSONProvider
from videogen.models.llm_io import SceneResponse
from videogen.providers.llm.base import LLMJSONError
from videogen.providers.llm.openrouter import _extract_json, scrub_secrets

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
