from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from videogen.config import get_settings
from videogen.providers.llm.base import LLMError, LLMJSONError, LLMMessage, LLMProvider
from videogen.utils.redact import scrub_secrets

logger = logging.getLogger(__name__)

#: 出现这些状态码时做指数退避重试
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

__all__ = ["OpenRouterProvider", "scrub_secrets"]


class OpenRouterProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        settings = get_settings()
        self.api_key = (api_key or settings.openrouter_api_key).strip()
        self.model = model or settings.openrouter_model
        self.base_url = settings.openrouter_base_url.rstrip("/")
        self.timeout_sec = settings.llm_timeout_sec
        self.max_retries = settings.llm_max_retries
        if not self.api_key:
            raise LLMError("未配置 LLM API Key（请检查 .env 中的 OPENROUTER_API_KEY）")

    # ── 对外接口 ──────────────────────────────────────────

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = self._payload(messages, temperature, max_tokens, json_mode)
        data = await self._post_with_retry(payload, json_requested=json_mode)
        self._record_usage(data)
        content = self._content_of(data)
        logger.debug("LLM ok model=%s chars=%d json=%s", self.model, len(content), json_mode)
        return content

    async def chat_json(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel] | None = None,
        temperature: float = 0.5,
        max_tokens: int = 8192,
        retries: int = 2,
    ) -> Any:
        convo = list(messages)
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            raw = await self.chat(
                convo, temperature=temperature, max_tokens=max_tokens, json_mode=True
            )
            try:
                parsed = _extract_json(raw)
                if schema is None:
                    return parsed
                return schema.model_validate(parsed)
            except (LLMJSONError, ValidationError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                logger.warning(
                    "LLM JSON/schema 校验失败（第 %d/%d 次尝试）：%s",
                    attempt + 1,
                    retries + 1,
                    scrub_secrets(str(exc))[:300],
                )
                convo = [
                    *messages,
                    LLMMessage(role="assistant", content=raw[:6000]),
                    LLMMessage(
                        role="user",
                        content=(
                            "上面的输出无法解析或不满足要求，错误信息：\n"
                            f"{scrub_secrets(str(exc))[:1500]}\n\n"
                            "请重新输出，要求：只输出一个合法的 JSON 对象，"
                            "不要 markdown 代码块、不要解释文字、不要注释、不要尾随逗号。"
                        ),
                    ),
                ]

        raise LLMJSONError(
            f"LLM 输出无法解析为合法 JSON/schema：{scrub_secrets(str(last_error))[:300]}"
        )

    # ── 内部实现 ──────────────────────────────────────────

    def _payload(
        self,
        messages: list[LLMMessage],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _post_with_retry(
        self, payload: dict[str, Any], *, json_requested: bool
    ) -> dict[str, Any]:
        attempt = 0
        json_fallback_used = False
        while True:
            try:
                async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=self._headers(),
                    )
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    await self._backoff(attempt)
                    attempt += 1
                    continue
                raise LLMError(
                    f"LLM 网络请求失败（{type(exc).__name__}）：{scrub_secrets(str(exc))[:200]}"
                ) from exc

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise LLMError("LLM 返回了非 JSON 的 HTTP 响应体") from exc

            body = scrub_secrets(resp.text or "")[:500]

            # 部分模型不支持 response_format，去掉后重试一次
            if (
                resp.status_code == 400
                and json_requested
                and not json_fallback_used
                and "response_format" in payload
            ):
                json_fallback_used = True
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                logger.warning("模型不支持 response_format，降级为纯 prompt 约束 JSON")
                continue

            if resp.status_code in _RETRY_STATUS and attempt < self.max_retries:
                logger.warning(
                    "LLM HTTP %d，%.1fs 后重试（第 %d/%d 次）",
                    resp.status_code,
                    2**attempt,
                    attempt + 1,
                    self.max_retries,
                )
                await self._backoff(attempt)
                attempt += 1
                continue

            raise LLMError(f"LLM 请求失败 HTTP {resp.status_code}：{body}")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(min(2**attempt, 8))

    @property
    def name(self) -> str:
        return "openrouter"

    def with_model(self, model: str) -> OpenRouterProvider:
        """同一个 provider 配置下换模型（用于多模型对比）。"""
        if not model or model == self.model:
            return self
        return OpenRouterProvider(api_key=self.api_key, model=model)

    def _record_usage(self, data: dict[str, Any]) -> None:
        raw = data.get("usage") or {}
        if not isinstance(raw, dict):
            return
        try:
            prompt = int(raw.get("prompt_tokens") or 0)
            completion = int(raw.get("completion_tokens") or 0)
            total = int(raw.get("total_tokens") or 0)
        except (TypeError, ValueError):
            return
        if completion == 0 and total > prompt:
            completion = total - prompt
        self.record_usage(input_tokens=prompt, output_tokens=completion)

    @staticmethod
    def _content_of(data: dict[str, Any]) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常：{scrub_secrets(json.dumps(data)[:300])}") from exc
        if isinstance(content, list):
            # 少数模型返回 content 分片数组
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            raise LLMError("LLM 响应 content 字段不是字符串")
        usage = (data.get("usage") or {}).get("total_tokens")
        if usage:
            logger.info("LLM tokens=%s model=%s", usage, data.get("model") or "?")
        return content


# ── JSON 提取 ─────────────────────────────────────────────


def _extract_json(text: str) -> Any:
    """从 LLM 输出中尽力提取 JSON。

    依次尝试：原文 → 剥掉 ``` 围栏 → 括号配平截取 → 修复（尾逗号/中文标点/智能引号）。
    """
    text = (text or "").strip()
    if not text:
        raise LLMJSONError("LLM 返回空内容")

    candidates: list[str] = []
    for base in (text, _strip_fence(text)):
        if not base:
            continue
        candidates.append(base)
        balanced = _balanced_json_slice(base)
        if balanced and balanced != base:
            candidates.append(balanced)

    last_error: Exception | None = None
    seen: set[str] = set()
    for candidate in candidates:
        for variant in (candidate, _repair_json(candidate)):
            if variant in seen:
                continue
            seen.add(variant)
            try:
                return json.loads(variant)
            except json.JSONDecodeError as exc:
                last_error = exc

    raise LLMJSONError(
        f"无法解析 LLM 输出为 JSON（{last_error}）；原始输出片段：{text[:200]!r}"
    )


def _strip_fence(text: str) -> str:
    """去掉 ```json ... ``` / ``` ... ``` 围栏，保留中间内容。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.split("\n")
    start = 1
    end = len(lines)
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].strip().startswith("```"):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def _balanced_json_slice(text: str) -> str | None:
    """从第一个 { 或 [ 开始，做括号配平扫描，返回完整的 JSON 片段。

    能正确处理字符串内的括号与转义，跳过模型前后的客套话。
    """
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None

    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"{": "}", "[": "]"}

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in pairs:
            stack.append(ch)
        elif ch in "}]":
            if not stack or pairs[stack.pop()] != ch:
                return None
            if not stack:
                return text[start : i + 1]
    return None


def _repair_json(text: str) -> str:
    """最后一搏的修复：中文标点、智能引号、尾逗号。"""
    repaired = text.strip()
    repaired = repaired.replace("“", '"').replace("”", '"')
    repaired = repaired.replace("‘", "'").replace("’", "'")
    # 中文逗号/冒号只可能出现在非字符串语境，作为兜底替换（必须先于尾逗号清理）
    repaired = repaired.replace("，", ",").replace("：", ":")
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    return repaired
