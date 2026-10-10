from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from pathlib import Path
from typing import Any

import httpx

from videogen.config import get_settings
from videogen.providers.image.base import (
    GeneratedImage,
    ImageArtifactError,
    ImageProvider,
    ImageProviderError,
    ImageRequest,
)
from videogen.utils.redact import scrub_secrets

logger = logging.getLogger(__name__)

#: 出现这些状态码时做退避重试（图片接口失败不计费，重试是安全的）
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: media_type → 文件后缀（provider 不报就按 png 处理）
_SUFFIX_BY_MEDIA_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}

__all__ = ["OpenRouterImageProvider"]


class OpenRouterImageProvider(ImageProvider):
    """OpenRouter 专用图片接口（`POST /api/v1/images`）。

    - 请求：`{model, prompt, n:1, aspect_ratio?, resolution?, input_references?}`
    - 响应：`data[0].b64_json`（base64 图片）+ `usage.cost`（USD，已含全部费用）
    - 计费是**全有或全无**：失败/取消不计费，所以失败重试不会重复扣钱
    """

    name = "openrouter"
    supports_reference_image = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_sec: float | None = None,
        max_http_retries: int = 3,
        retry_backoff_sec: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = (api_key or settings.openrouter_api_key).strip()
        if not self.api_key:
            raise ImageProviderError(
                "未配置 OPENROUTER_API_KEY，无法生成角色图"
            )
        self.model = model or settings.openrouter_image_model
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.timeout_sec = max(30.0, timeout_sec or settings.image_timeout_sec)
        self.max_http_retries = max(1, max_http_retries)
        self.retry_backoff_sec = max(
            0.0,
            settings.image_retry_backoff_sec
            if retry_backoff_sec is None
            else retry_backoff_sec,
        )
        self._client = client
        self._owns_client = client is None

    # ── HTTP 基础设施 ─────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_sec))
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is None or not self._owns_client:
            return
        try:
            await client.aclose()
        except RuntimeError:  # 事件循环已经关了（测试里常见）
            logger.debug("关闭图片 HTTP 客户端时事件循环已结束", exc_info=True)

    # ── 对外接口 ──────────────────────────────────────────

    async def generate(self, request: ImageRequest) -> GeneratedImage:
        payload = self._payload(request)
        data = await self._post(payload)
        return self._save(data, request)

    # ── 内部实现 ──────────────────────────────────────────

    def _payload(self, request: ImageRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "n": 1,
        }
        if request.aspect_ratio:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.resolution:
            payload["resolution"] = request.resolution
        references = request.all_references() if self.supports_reference_image else []
        if references:
            payload["input_references"] = [
                {"type": "image_url", "image_url": {"url": _data_url(path)}}
                for path in references
            ]
        return payload

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._http()
        url = f"{self.base_url}/images"
        last_error = ""
        for attempt in range(1, self.max_http_retries + 1):
            try:
                response = await client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                last_error = scrub_secrets(f"{type(exc).__name__}: {exc}")
                if attempt < self.max_http_retries:
                    await asyncio.sleep(min(15.0, self.retry_backoff_sec * attempt))
                    continue
                raise ImageProviderError(f"图片请求失败：{last_error}") from exc

            if response.status_code in _RETRY_STATUS and attempt < self.max_http_retries:
                logger.warning(
                    "图片接口 HTTP %d，第 %d/%d 次重试", response.status_code, attempt,
                    self.max_http_retries,
                )
                await asyncio.sleep(min(15.0, self.retry_backoff_sec * attempt))
                continue

            if response.status_code >= 400:
                body = scrub_secrets(response.text or "")[:300]
                raise ImageProviderError(
                    f"图片请求失败 HTTP {response.status_code}：{body}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise ImageProviderError(
                    f"图片接口响应不是 JSON：{scrub_secrets(response.text or '')[:200]}"
                ) from exc
            if not isinstance(data, dict):
                raise ImageProviderError("图片接口响应结构异常")
            return data

        raise ImageProviderError(f"图片请求重试次数用尽：{last_error}")

    def _save(self, data: dict[str, Any], request: ImageRequest) -> GeneratedImage:
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise ImageProviderError("图片接口没有返回任何图片")
        first = items[0] if isinstance(items[0], dict) else {}
        encoded = str(first.get("b64_json") or "")
        if not encoded:
            raise ImageProviderError("图片接口返回里没有 b64_json 字段")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageArtifactError("图片 base64 解码失败（已生成但拿不到图）") from exc

        media_type = str(first.get("media_type") or "image/png").lower()
        suffix = _SUFFIX_BY_MEDIA_TYPE.get(media_type, "")

        output = Path(request.output_path)
        if suffix and output.suffix.lower() != suffix:
            output = output.with_suffix(suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            output.write_bytes(raw)
        except OSError as exc:
            raise ImageArtifactError(f"图片落盘失败（已生成但存不下）：{exc}") from exc

        return GeneratedImage(
            path=output,
            media_type=media_type,
            actual_cost_usd=_usage_cost(data),
        )


def _usage_cost(data: dict[str, Any]) -> float | None:
    """读 `usage.cost`（USD）。接口没报就返回 None，由上层按单价估算。"""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        value = float(usage.get("cost"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix, "image/png")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
