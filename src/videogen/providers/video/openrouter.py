from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from videogen.config import get_settings
from videogen.models.video import MediaKind
from videogen.providers.video.base import (
    GeneratedClip,
    VideoArtifactError,
    VideoProvider,
    VideoProviderError,
    VideoReferenceRejected,
    VideoRequest,
)
from videogen.utils.ffmpeg import probe_duration
from videogen.utils.redact import scrub_secrets

logger = logging.getLogger(__name__)

__all__ = ["OpenRouterVideoProvider", "clear_video_capability_cache"]

#: `/videos/models` 的能力缓存（进程级）。能力/单价一天内基本不变，没必要每次都拉。
_CAP_CACHE: dict[str, dict[str, Any]] = {}

#: 这些状态码值得退避重试
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: 单次下载图片/视频的分块大小
_CHUNK = 1024 * 256

#: 提示词长度上限（字符）。能力表里没有这一项，只能按模型前缀写死：
#: Kling 超过 2500 直接 FAIL（`ret:1201, prompt: size must be between 0 and 2500`），
#: 而且失败发生在轮询阶段，重试 3 次照样失败。前缀越长越优先
_PROMPT_LIMITS: tuple[tuple[str, int], ...] = (("kwaivgi/", 2500),)


def clear_video_capability_cache() -> None:
    """测试用：清掉能力缓存。"""
    _CAP_CACHE.clear()


class OpenRouterVideoProvider(VideoProvider):
    """通过 OpenRouter 的统一视频接口调用真实视频模型（默认 minimax/hailuo-3 = MiniMax H3）。

    流程与官方文档一致（异步 job）：

    1. `POST /api/v1/videos` 提交 → 拿到 `id` / `polling_url`
    2. `GET {polling_url}` 轮询到 `completed`
    3. 下载 `unsigned_urls[0]` 到 `request.output_path`

    几个必须知道的模型约束（都从 `/videos/models` 读，不写死）：

    - H3 时长只支持 **5~15 秒整数**，比镜头短的请求会被抬到 5 秒；
      多出来的部分由合成阶段裁掉（`compose` 会 `trim` 到镜头时长），所以不会串味
    - H3 分辨率只支持 **2K**；配错会自动挑一个支持的并告警
    - H3 **不支持 seed**，所以参数里不会带 `seed`（带上会被 400）
    - 角色一致性用 `input_references`（reference-to-video）；不支持时退回
      `frame_images.first_frame`，都不支持就忽略参考图并告警
    - 带的图被模型拒收（内容审核 / 格式）时抛 `VideoReferenceRejected`，**不**悄悄去掉图重试
    """

    name = "openrouter"
    supports_reference_image = True  # 通过参考图（不是 seed）保持角色外观
    requires_ffmpeg = False  # 只下载，不转码

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        resolution: str = "",
        generate_audio: bool = False,
        use_reference_images: bool = True,
        poll_interval_sec: float | None = None,
        timeout_sec: float | None = None,
        max_http_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = (api_key or settings.openrouter_api_key).strip()
        if not self.api_key:
            raise VideoProviderError(
                "未配置 OPENROUTER_API_KEY，无法使用 openrouter 视频 provider"
            )
        self.model = model or settings.openrouter_video_model
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.resolution = resolution or settings.video_resolution
        self.generate_audio = generate_audio
        self.use_reference_images = use_reference_images
        self.poll_interval_sec = max(2.0, poll_interval_sec or settings.video_poll_interval_sec)
        self.timeout_sec = max(60.0, timeout_sec or settings.video_timeout_sec)
        self.max_http_retries = max(1, max_http_retries)
        self._client = client
        self._owns_client = client is None

    # ── HTTP 基础设施 ─────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0))
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is None or not self._owns_client:
            return
        try:
            await client.aclose()
        except RuntimeError:  # 事件循环已经关了（测试里常见），不影响正确性
            logger.debug("关闭 httpx 客户端时事件循环已结束", exc_info=True)

    async def _get_json(self, url: str, *, timeout_sec: float = 60.0) -> dict[str, Any]:
        """带退避重试的 GET（429/5xx 才重试）。"""
        client = await self._http()
        last_error = ""
        for attempt in range(1, self.max_http_retries + 1):
            try:
                response = await client.get(url, headers=self._headers(), timeout=timeout_sec)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_http_retries:
                    await asyncio.sleep(min(8.0, 1.5 * attempt))
                    continue
                raise VideoProviderError(
                    scrub_secrets(f"OpenRouter 请求失败（{url}）：{last_error}")
                ) from exc

            if response.status_code in _RETRY_STATUS and attempt < self.max_http_retries:
                logger.warning(
                    "OpenRouter 返回 %d，第 %d/%d 次重试",
                    response.status_code,
                    attempt,
                    self.max_http_retries,
                )
                await asyncio.sleep(min(15.0, 2.0 * attempt))
                continue
            if response.status_code >= 400:
                raise VideoProviderError(_http_error_message(response, url))
            try:
                return response.json()
            except ValueError as exc:
                raise VideoProviderError(
                    f"OpenRouter 返回的不是 JSON（{url}）：{scrub_secrets(response.text[:200])}"
                ) from exc
        raise VideoProviderError(f"OpenRouter 请求失败（{url}）：{scrub_secrets(last_error)}")

    # ── 能力 / 单价 ───────────────────────────────────────

    async def capabilities(self, *, refresh: bool = False) -> dict[str, Any]:
        """该模型的能力条目（`/videos/models`），带进程级缓存。"""
        if not refresh and self.model in _CAP_CACHE:
            return _CAP_CACHE[self.model]
        data = await self._get_json(f"{self.base_url}/videos/models")
        entries = data.get("data") or []
        found: dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            _CAP_CACHE[str(entry.get("id"))] = entry
        found = _CAP_CACHE.get(self.model, {})
        if not found:
            # 能力查不到不该直接失败：可能是新模型还没进列表，按「不限制」提交
            logger.warning("OpenRouter 视频模型列表里没有 %s，将不做参数校验", self.model)
        return found

    async def price_per_second(self, *, resolution: str = "") -> float | None:
        """从 `pricing_skus` 读单价（USD/秒），读不到就返回 None 让上层用配置值。"""
        caps = await self.capabilities()
        skus = caps.get("pricing_skus") or {}
        if not isinstance(skus, dict) or not skus:
            return None

        wanted = (resolution or self.resolution).strip().lower()
        if not wanted:
            # 不指定分辨率时提交用的是模型列表里的第一档，单价也按它算
            wanted = _pick_from("", caps.get("supported_resolutions")).lower()
        # 带音频 / 不带音频两档价（Kling、Veo）：按是否真的生成音轨取对应那档，
        # 否则开了音轨却按低价预估（Kling 少算 1/3）
        audio = "with_audio" if self.generate_audio and caps.get("generate_audio") else ""
        audio_tags = [audio, ""] if audio else ["without_audio", ""]
        candidates: list[str] = []
        for key in ("duration_seconds", "per-video-second", "per_video_second"):
            for tag in audio_tags:
                base = f"{key}_{tag}" if tag else key
                if wanted:
                    candidates += [f"{base}_{wanted}", f"{base}-{wanted}"]
                candidates.append(base)
        for key in candidates:
            if key in skus:
                value = _as_float(skus[key])
                if value is not None:
                    return value
        # 兜底：任何带 second 的 key（跳过另一档音频价）
        other = "without_audio" if audio else "with_audio"
        for key, raw in skus.items():
            lowered = str(key).lower()
            if "second" in lowered and other not in lowered:
                value = _as_float(raw)
                if value is not None:
                    return value
        return None

    async def frame_mode(self) -> str:
        if not self.use_reference_images:
            return "none"
        return _frame_mode(await self.capabilities())

    async def planned_seconds(self, requested_sec: float) -> float:
        """按模型支持的时长档位，算出**实际会被计费**的秒数。"""
        caps = await self.capabilities()
        return _clamp_duration(float(requested_sec), caps.get("supported_durations"))

    async def duration_options(self) -> list[float]:
        """模型支持的时长档位（升序）。H3 是 5~15 秒，所以 3 秒的镜头也按 5 秒计费。"""
        caps = await self.capabilities()
        raw = caps.get("supported_durations")
        if not isinstance(raw, list):
            return []
        values: list[float] = []
        for item in raw:
            value = _as_float(item)
            if value is not None and value > 0:
                values.append(value)
        return sorted(set(values))

    # ── 生成 ──────────────────────────────────────────────

    async def generate(self, request: VideoRequest) -> GeneratedClip:
        caps = await self.capabilities()
        payload = self._build_payload(request, caps)

        job = await self._submit(payload)
        job_id = str(job.get("id") or "")
        polling_url = str(job.get("polling_url") or f"{self.base_url}/videos/{job_id}")
        if not job_id:
            raise VideoProviderError(f"OpenRouter 没有返回 job id：{scrub_secrets(str(job))[:200]}")

        logger.info(
            "视频任务已提交 shot=%s model=%s job=%s 时长=%ss 分辨率=%s",
            request.shot_id,
            self.model,
            job_id,
            payload.get("duration", "?"),
            payload.get("resolution", "默认"),
        )

        status = await self._poll(polling_url, job_id=job_id, shot_id=request.shot_id)
        urls = [str(u) for u in (status.get("unsigned_urls") or [])]

        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        # 注意：到这一步片段已经生成并扣费了，任何失败都必须抛不可重试的错误
        await self._download(job_id, urls, output)

        duration = await probe_duration(output) or 0.0
        if duration <= 0:
            duration = await self.planned_seconds(float(request.duration_sec))
            logger.info("拿不到真实时长（没有 ffprobe），按计费档位记为 %.1fs", duration)
        if duration <= 0:
            raise VideoProviderError(f"任务 {job_id} 的产物时长为 0，无法使用")

        actual_cost = _usage_cost(status)
        logger.info(
            "视频片段完成 shot=%s job=%s 时长=%.2fs 大小=%d bytes 扣费=%s",
            request.shot_id,
            job_id,
            duration,
            output.stat().st_size,
            f"${actual_cost:.4f}" if actual_cost else "未返回",
        )
        return GeneratedClip(
            path=output,
            media_kind=MediaKind.VIDEO,
            duration_sec=round(duration),
            task_id=job_id,
            actual_cost_usd=actual_cost,
        )

    def _build_payload(self, request: VideoRequest, caps: dict[str, Any]) -> dict[str, Any]:
        duration = _clamp_duration(float(request.duration_sec), caps.get("supported_durations"))
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": fit_prompt(request.prompt, prompt_limit(self.model)),
            "duration": round(duration),
        }
        if duration > request.duration_sec:
            logger.info(
                "shot=%s 请求 %ss，模型最短支持 %ss：会生成 %.0fs 再由合成裁到镜头时长",
                request.shot_id,
                request.duration_sec,
                duration,
                duration,
            )

        resolution = _pick_from(self.resolution, caps.get("supported_resolutions"))
        if resolution:
            if resolution != self.resolution and self.resolution:
                logger.warning(
                    "模型 %s 不支持分辨率 %s，改用 %s",
                    self.model,
                    self.resolution,
                    resolution,
                )
            payload["resolution"] = resolution

        aspect = _pick_from(request.aspect_ratio, caps.get("supported_aspect_ratios"))
        if aspect:
            payload["aspect_ratio"] = aspect
        elif request.aspect_ratio:
            logger.warning(
                "模型 %s 不支持比例 %s，交给服务端默认", self.model, request.aspect_ratio
            )

        # H3 支持自带音轨，但我们用 TTS 配音，不需要多花钱生成
        if caps.get("generate_audio"):
            payload["generate_audio"] = bool(self.generate_audio)

        # seed 只有模型声明支持时才带（H3 是 false，带上会 400）
        if request.seed and caps.get("seed") is True:
            payload["seed"] = int(request.seed)

        reference = self._reference_payload(request, caps)
        if reference:
            payload.update(reference)
        return payload

    def _reference_payload(self, request: VideoRequest, caps: dict[str, Any]) -> dict[str, Any]:
        """角色一致性：优先 reference-to-video（可多张），其次 image-to-video（首帧）。

        被拒时不降级（见 `VideoReferenceRejected`）。
        """
        if not self.use_reference_images:
            return {}
        paths = [Path(p) for p in (request.reference_images or [])]
        paths = [p for p in paths if p.is_file()]
        mode = _frame_mode(caps)

        if mode == "references":
            if not paths:
                return {}
            return {"input_references": [_image_url_payload(path) for path in paths]}
        if mode == "first_frame":
            first = Path(request.first_frame) if request.first_frame else None
            if first is not None and first.is_file():
                frame = {**_image_url_payload(first), "frame_type": "first_frame"}
                return {"frame_images": [frame]}
            if paths:
                # 定妆照（正面半身、干净背景）当首帧 = 每段都从一张证件照开场，宁可不带
                logger.warning(
                    "模型 %s 只认首帧、不收参考图：没有关键帧，%d 张参考图不作为首帧发送"
                    "（VIDEO_KEYFRAME=auto 会先画一张开场关键帧）",
                    self.model,
                    len(paths),
                )
            return {}

        if paths:
            logger.warning(
                "模型 %s 不支持参考图/首帧图，已忽略 %d 张参考图", self.model, len(paths)
            )
        return {}

    async def _submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._http()
        url = f"{self.base_url}/videos"
        attempt_payload = dict(payload)

        for attempt in range(1, self.max_http_retries + 1):
            try:
                response = await client.post(url, headers=self._headers(), json=attempt_payload)
            except httpx.HTTPError as exc:
                if attempt < self.max_http_retries:
                    await asyncio.sleep(min(10.0, 2.0 * attempt))
                    continue
                raise VideoProviderError(
                    scrub_secrets(f"提交视频任务失败：{type(exc).__name__}: {exc}")
                ) from exc

            if response.status_code in _RETRY_STATUS and attempt < self.max_http_retries:
                logger.warning(
                    "提交视频任务返回 %d，第 %d/%d 次重试",
                    response.status_code,
                    attempt,
                    self.max_http_retries,
                )
                await asyncio.sleep(min(20.0, 3.0 * attempt))
                continue

            if response.status_code == 400 and (
                "input_references" in attempt_payload or "frame_images" in attempt_payload
            ):
                # 带的图被拒：不降级成「不带图」悄悄出片（那样一致性就没了），交给上层问用户
                raise _reference_rejected(response, attempt_payload)

            if response.status_code >= 400:
                raise VideoProviderError(_http_error_message(response, url))

            try:
                data = response.json()
            except ValueError as exc:
                raise VideoProviderError(
                    f"提交视频任务的响应不是 JSON：{scrub_secrets(response.text[:200])}"
                ) from exc
            if not isinstance(data, dict):
                raise VideoProviderError("提交视频任务的响应结构异常")
            return data

        raise VideoProviderError("提交视频任务失败：重试次数用尽")

    async def _poll(self, polling_url: str, *, job_id: str, shot_id: str) -> dict[str, Any]:
        """轮询到 completed / failed / 超时。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_sec
        last_status = ""

        while True:
            status = await self._get_json(polling_url)
            state = str(status.get("status") or "")
            if state != last_status:
                logger.info("视频任务 %s（shot=%s）状态：%s", job_id, shot_id, state or "?")
                last_status = state

            if state == "completed":
                return status
            if state in {"failed", "error", "cancelled"}:
                reason = status.get("error") or status.get("message") or state
                raise VideoProviderError(
                    f"视频生成失败（job={job_id}）：{scrub_secrets(str(reason))[:300]}"
                )
            if loop.time() >= deadline:
                raise VideoProviderError(
                    f"视频生成超时（job={job_id}，已等 {self.timeout_sec:.0f}s，"
                    f"最后状态 {last_status or '未知'}）。任务在服务端可能仍在跑。"
                )
            await asyncio.sleep(self.poll_interval_sec)

    async def _download(self, job_id: str, urls: list[str], output: Path) -> None:
        """按「官方 content 端点（带鉴权）→ unsigned_urls（带鉴权）→ 无鉴权」依次尝试。

        `unsigned_urls` 是 `.../videos/{id}/content?index=0` 这种地址，**需要
        Authorization 头**（官方 curl 示例里也是带的），裸下载会 401。
        """
        candidates: list[tuple[str, bool]] = [
            (f"{self.base_url}/videos/{job_id}/content?index=0", True)
        ]
        candidates += [(url, True) for url in urls]
        candidates += [(url, False) for url in urls]

        failures: list[str] = []
        tried: list[str] = []
        for url, authorized in candidates:
            tried.append(url)
            ok, reason = await self._try_download(url, output, authorized=authorized)
            if ok:
                logger.info(
                    "视频下载成功（%s）：%s",
                    "带鉴权" if authorized else "无鉴权",
                    url.split("?")[0],
                )
                return
            failures.append(f"{url.split('?')[0]}({'带鉴权' if authorized else '匿名'}) {reason}")
            output.unlink(missing_ok=True)

        raise VideoArtifactError(
            f"视频已生成但下载失败（job={job_id}，已尝试 {len(tried)} 个地址）："
            + "；".join(failures[:3]),
            job_id=job_id,
            urls=tried,
        )

    async def _try_download(self, url: str, output: Path, *, authorized: bool) -> tuple[bool, str]:
        """下载一次，返回 (是否成功, 失败原因)。"""
        client = await self._http()
        headers = self._headers() if authorized else {}
        try:
            async with client.stream(
                "GET", url, headers=headers, timeout=httpx.Timeout(60.0, read=300.0)
            ) as resp:
                if resp.status_code >= 400:
                    return False, f"HTTP {resp.status_code}"
                with output.open("wb") as handle:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"

        if not output.is_file() or output.stat().st_size == 0:
            return False, "空文件"
        return True, ""


# ── 工具 ──────────────────────────────────────────────────


def _usage_cost(status: dict[str, Any]) -> float | None:
    """从轮询响应里读 OpenRouter 报的实际扣费（USD）。

    这比按价目表推算更准（例如 2K 档位实际单价比 `pricing_skus` 里的基础档更高）。
    """
    usage = status.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("cost", "total_cost", "total_price"):
        value = _as_float(usage.get(key))
        if value is not None:
            return value
    return None


#: 错误体里的错误码，例如 `InputImageSensitiveContentDetected.PrivacyInformation`
#: （OpenRouter 把上游的 JSON 转义后包在 message 里，所以引号前可能有反斜杠）
_ERROR_CODE = re.compile(r'\\?"code\\?"\s*:\s*\\?"([A-Za-z][\w.]*)')


def _reference_rejected(
    response: httpx.Response, payload: dict[str, Any]
) -> VideoReferenceRejected:
    text = scrub_secrets(response.text or "")
    match = _ERROR_CODE.search(text)
    code = match.group(1) if match else ""
    kind = "first_frame" if "frame_images" in payload else "references"
    count = len(payload.get("frame_images") or payload.get("input_references") or [])
    label = "首帧" if kind == "first_frame" else f"{count} 张参考图"
    logger.warning(
        "视频模型拒收了%s（%s），中断等用户决定：%s", label, code or "无错误码", text[:200]
    )
    return VideoReferenceRejected(
        f"视频模型拒收了{label}（{code or 'HTTP 400'}）：{text[:240]}", kind=kind, code=code
    )


def _frame_mode(caps: dict[str, Any]) -> str:
    """能力表 → `references` / `first_frame` / `none`（价目里有 reference 计费项才算收参考图）。"""
    skus = caps.get("pricing_skus") or {}
    if isinstance(skus, dict) and any("reference" in str(key).lower() for key in skus):
        return "references"
    if "first_frame" in (caps.get("supported_frame_images") or []):
        return "first_frame"
    return "none"


def prompt_limit(model: str) -> int:
    """该模型的提示词字符上限（0 = 未知 / 不限）。"""
    for prefix, limit in sorted(_PROMPT_LIMITS, key=lambda item: -len(item[0])):
        if model.startswith(prefix):
            return limit
    return 0


def fit_prompt(prompt: str, limit: int) -> str:
    """把提示词压进模型上限：先去掉全局通用要求（背景 / 表演 / 风格），再按句截断。

    通用要求是给所有镜头的「加分项」，逐镜头的画面与台词才是这一段的内容，所以先砍前者。
    """
    if limit <= 0 or len(prompt) <= limit:
        return prompt
    settings = get_settings()
    fitted = prompt
    for directive in (
        settings.video_style_prompt,
        settings.video_performance_directive,
        settings.video_background_directive,
    ):
        directive = (directive or "").strip()
        if not directive:
            continue
        # 整句对不上时（段落 prompt 会删掉其中「不许切」那条）按逗号分句删
        parts = [directive] if directive in fitted else directive.split(", ")
        for part in parts:
            part = part.strip(" ,.;")
            if part and part in fitted:
                fitted = fitted.replace(part, "")
        fitted = re.sub(r"(\s*,)+\s*(?=[,.]|$)", "", fitted).strip(" ,.;")
        if len(fitted) <= limit:
            break
    truncated = len(fitted) > limit
    if truncated:
        cut = fitted[:limit]
        end = max(cut.rfind(". "), cut.rfind('" '))
        fitted = cut[: end + 1] if end > limit // 2 else cut
    logger.warning(
        "提示词 %d 字符超过模型上限 %d：已去掉通用要求%s → %d 字符",
        len(prompt),
        limit,
        "并按句截断" if truncated else "",
        len(fitted),
    )
    return fitted


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _clamp_duration(requested: float, supported: Any) -> float:
    """把请求时长夹到模型支持的档位（向上取整到最近的一档）。"""
    want = max(1.0, float(requested))
    if not isinstance(supported, list) or not supported:
        return want
    options = sorted(float(x) for x in supported if isinstance(x, (int, float)))
    if not options:
        return want
    if want <= options[0]:
        return options[0]
    if want >= options[-1]:
        return options[-1]
    for option in options:
        if option >= want:
            return option
    return options[-1]


def _pick_from(requested: str, supported: Any) -> str:
    """在支持列表里挑一个：优先用户配置，否则第一个，列表为空则不传该参数。"""
    want = (requested or "").strip()
    if not isinstance(supported, list) or not supported:
        return want
    options = [str(x) for x in supported]
    if want in options:
        return want
    return options[0]


def _image_url_payload(path: Path) -> dict[str, Any]:
    """本地参考图转 data URL（OpenRouter 的 image_url 接受 https 或 data URL）。"""
    suffix = path.suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/{suffix};base64,{encoded}"}}


def _http_error_message(response: httpx.Response, url: str) -> str:
    """把 OpenRouter 的错误体转成一句人话（绝不带密钥）。"""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error)
            elif error:
                detail = str(error)
            else:
                detail = str(body.get("message") or "")
    except ValueError:
        detail = response.text[:200]

    hints = {
        400: "参数不被支持（时长/分辨率/比例/参考图）",
        401: "API Key 无效",
        402: "OpenRouter 余额不足",
        403: "该 Key 没有视频生成权限",
        404: "模型不存在（检查 OPENROUTER_VIDEO_MODEL）",
        429: "触发限流，稍后重试",
    }
    hint = hints.get(response.status_code, "")
    label = f"HTTP {response.status_code}"
    if hint:
        label += f"（{hint}）"
    return scrub_secrets(f"OpenRouter {label}：{detail}")[:400]


