from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from videogen.config import get_settings
from videogen.models.video import MediaKind
from videogen.providers.video.base import (
    GeneratedClip,
    VideoArtifactError,
    VideoJobPending,
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

#: 运行中从失败原因里学到的提示词上限（模型 slug → 字符数，进程内）。
#: Kling 超过 2500 直接 FAIL（`ret:1201, prompt: size must be between 0 and 2500`），
#: 失败发生在轮询阶段；记住上限后，上层的重试就会按它压缩，而不是原样再失败
_LEARNED_PROMPT_LIMITS: dict[str, int] = {}

#: 失败原因里「提示词太长」的说法：提到 prompt + 长度类字眼，上限取 prompt 之后最大的那个数
_PROMPT_TOO_LONG = re.compile(
    r"too\s*long|exceed|at\s*most|max|between|length|size", re.IGNORECASE
)


def clear_video_capability_cache() -> None:
    """测试用：清掉能力缓存（含学到的提示词上限）。"""
    _CAP_CACHE.clear()
    _LEARNED_PROMPT_LIMITS.clear()


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
    paid = True
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

    def _own_host(self, url: str) -> bool:
        """这个地址是不是 OpenRouter 自己的（同协议，同主机或它的子域名）。

        `polling_url` / `unsigned_urls` 都来自接口响应：指向别的域名时不能带 key 过去
        （key 会被第三方拿到）。实测 `unsigned_urls` 就是 OpenRouter 自己的 content 端点。
        """
        mine, theirs = urlsplit(self.base_url), urlsplit(url)
        host, base = (theirs.hostname or "").lower(), (mine.hostname or "").lower()
        same_host = host == base or host.endswith(f".{base}")
        return bool(base) and theirs.scheme == mine.scheme and same_host

    def _headers_for(self, url: str) -> dict[str, str]:
        return self._headers() if self._own_host(url) else {}

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
                response = await client.get(
                    url, headers=self._headers_for(url), timeout=timeout_sec
                )
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
                error = VideoProviderError(_http_error_message(response, url))
                error.status_code = response.status_code  # type: ignore[attr-defined]
                raise error
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
        """从 `pricing_skus` 读单价（USD/秒），读不到就返回 None 让上层用配置值。

        三种价目写法都认：按秒（`duration_seconds*`）、按美分每秒（`cents_per_*second*`，
        Grok / Runway）、按视频 token（`video_tokens*`，Seedance，按分辨率换算成每秒）。
        """
        caps = await self.capabilities()
        skus = caps.get("pricing_skus") or {}
        if not isinstance(skus, dict) or not skus:
            return None

        wanted = (resolution or self.resolution).strip().lower()
        if not wanted:
            # 不指定分辨率时提交用的是模型列表里的第一档，单价也按它算
            wanted = _pick_from("", caps.get("supported_resolutions")).lower()
        # 带音频 / 不带音频两档价（Kling、Veo、Seedance 1.5）：按是否真的生成音轨取对应那档，
        # 否则开了音轨却按低价预估（Kling 少算 1/3）
        with_audio = bool(self.generate_audio and caps.get("generate_audio"))
        price = _per_second_price(skus, wanted, with_audio)
        if price is None:
            price = _token_price(skus, wanted, with_audio)
        return price

    async def frame_mode(self) -> str:
        if not self.use_reference_images:
            return "none"
        return _frame_mode(await self.capabilities(), get_settings().video_frame_mode)

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

        status: dict[str, Any] | None = None
        job_id = request.resume_job_id
        if job_id:
            # 上一次已经提交（取消 / 超时 / 下载失败）：接着轮询同一个任务，不重新买
            logger.info("接着轮询已提交的视频任务 shot=%s job=%s", request.shot_id, job_id)
            try:
                status = await self._poll(
                    f"{self.base_url}/videos/{job_id}",
                    job_id=job_id,
                    shot_id=request.shot_id,
                    payload=payload,
                )
            except VideoReferenceRejected:
                raise
            except VideoJobPending:
                raise
            except VideoProviderError as exc:
                # 原任务在服务端已失败（失败不计费）：重新提交一次
                logger.warning(
                    "已提交的任务 %s 在服务端失败，重新提交：%s",
                    job_id,
                    scrub_secrets(str(exc))[:200],
                )
                status = None

        if status is None:
            job = await self._submit(payload)
            job_id = str(job.get("id") or "")
            polling_url = str(job.get("polling_url") or f"{self.base_url}/videos/{job_id}")
            if not job_id:
                raise VideoProviderError(
                    f"OpenRouter 没有返回 job id：{scrub_secrets(str(job))[:200]}"
                )
            if request.on_submitted is not None:
                request.on_submitted(job_id)

            logger.info(
                "视频任务已提交 shot=%s model=%s job=%s 时长=%ss 分辨率=%s",
                request.shot_id,
                self.model,
                job_id,
                payload.get("duration", "?"),
                payload.get("resolution", "默认"),
            )

            status = await self._poll(
                polling_url, job_id=job_id, shot_id=request.shot_id, payload=payload, fresh=True
            )
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
        mode = _frame_mode(caps, get_settings().video_frame_mode)

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
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                # 连接都没建立：请求肯定没到服务端，重发是安全的
                if attempt < self.max_http_retries:
                    await asyncio.sleep(min(10.0, 2.0 * attempt))
                    continue
                raise VideoProviderError(
                    scrub_secrets(f"提交视频任务失败：{type(exc).__name__}: {exc}")
                ) from exc
            except httpx.HTTPError as exc:
                # 请求可能已经到了服务端、任务可能已经建好（只是响应丢了）：重发 = 可能买两次
                raise VideoJobPending(
                    scrub_secrets(
                        f"提交视频任务后没收到响应（{type(exc).__name__}: {exc}），"
                        "任务可能已在服务端创建并计费；请先到 OpenRouter 后台核对再重试"
                    )
                ) from exc

            if response.status_code == 504:
                # 网关等上游超时：上游可能已经建好任务（和读超时一样），重发 = 可能买两次
                raise VideoJobPending(
                    "提交视频任务返回 504（网关超时），任务可能已在服务端创建并计费；"
                    "请先到 OpenRouter 后台核对再重试"
                )

            if response.status_code in _RETRY_STATUS and attempt < self.max_http_retries:
                logger.warning(
                    "提交视频任务返回 %d，第 %d/%d 次重试",
                    response.status_code,
                    attempt,
                    self.max_http_retries,
                )
                await asyncio.sleep(min(20.0, 3.0 * attempt))
                continue

            if (
                response.status_code == 400
                and ("input_references" in attempt_payload or "frame_images" in attempt_payload)
                and _IMAGE_PROBLEM.search(response.text or "")
            ):
                # 带的图被拒：不降级成「不带图」悄悄出片（那样一致性就没了），交给上层问用户。
                # 只认「错误在说图」的 400：参数错（时长 / 分辨率）不该让用户去重画图
                raise _reference_rejected(response, attempt_payload)

            if response.status_code >= 400:
                error = VideoProviderError(_http_error_message(response, url))
                # 4xx 是请求本身的问题，原样重交只会再错一次
                error.retryable = response.status_code >= 500
                raise error

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

    async def _poll(
        self,
        polling_url: str,
        *,
        job_id: str,
        shot_id: str,
        payload: dict[str, Any] | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """轮询到 completed / failed / 超时（`fresh` = 这个任务是这一轮刚提交的）。

        有的模型不是提交时 400，而是任务跑起来才报图片被拒（MiniMax H3：
        `input new_sensitive, input image sensitive (1026)`）—— 同样按「带的图被拒」中断，交给用户。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_sec
        last_status = ""

        while True:
            try:
                status = await self._get_json(polling_url)
            except VideoProviderError as exc:
                if getattr(exc, "status_code", 0) in (404, 410) and not fresh:
                    # 续查的老任务服务端说没有：过期了，按「生成失败」处理（可重新提交）。
                    # 刚提交的任务查不到更可能是还没同步过去 —— 走下面的 pending，
                    # 下次接着查；当场重交就可能买两份
                    raise VideoProviderError(
                        f"视频任务不存在（job={job_id}）：{scrub_secrets(str(exc))[:200]}"
                    ) from exc
                # 任务已经提交：查询失败不等于任务失败，绝不能让上层重新提交
                raise VideoJobPending(
                    f"查询视频任务失败（job={job_id}），任务在服务端可能仍在跑；"
                    f"下次生成会接着查这个任务：{scrub_secrets(str(exc))[:200]}",
                    job_id=job_id,
                ) from exc
            state = str(status.get("status") or "")
            if state != last_status:
                logger.info("视频任务 %s（shot=%s）状态：%s", job_id, shot_id, state or "?")
                last_status = state

            if state == "completed":
                return status
            if state in {"failed", "error", "cancelled"}:
                reason = status.get("error") or status.get("message") or state
                rejected = _image_rejected_in_job(str(reason), payload or {}, job_id)
                if rejected is not None:
                    raise rejected
                learn_prompt_limit(self.model, str(reason))
                raise VideoProviderError(
                    f"视频生成失败（job={job_id}）：{scrub_secrets(str(reason))[:300]}"
                )
            if loop.time() >= deadline:
                raise VideoJobPending(
                    f"视频生成超时（job={job_id}，已等 {self.timeout_sec:.0f}s，"
                    f"最后状态 {last_status or '未知'}）。任务在服务端可能仍在跑，"
                    "下次生成会接着查这个任务，不会重新提交。",
                    job_id=job_id,
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
        # 别的主机上的地址只匿名下载：key 不出 OpenRouter
        candidates += [(url, True) for url in urls if self._own_host(url)]
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
        headers = self._headers_for(url) if authorized else {}
        try:
            async with client.stream(
                "GET", url, headers=headers, timeout=httpx.Timeout(60.0, read=300.0)
            ) as resp:
                # 只认 2xx：httpx 默认不跟随跳转，3xx 的响应体会被当成「片段」写盘
                if not 200 <= resp.status_code < 300:
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



#: 价目 key 里可能出现的分辨率标记（小写）
_RESOLUTION_TAGS = ("480p", "720p", "768p", "1080p", "2k", "4k")
#: 不是「输出每秒单价」的 key：参考图 / 续写 / 起步价 / 图片输入 / 按像素计
_NOT_OUTPUT_SECOND = (
    "reference", "continuation", "minimum", "image_input", "megapixel", "video_input",
)


def _per_second_price(skus: dict[str, Any], wanted: str, with_audio: bool) -> float | None:
    audio_tags = ["with_audio", ""] if with_audio else ["without_audio", ""]
    candidates: list[str] = []
    for key in ("duration_seconds", "per-video-second", "per_video_second"):
        for tag in audio_tags:
            base = f"{key}_{tag}" if tag else key
            if wanted:
                candidates += [f"{base}_{wanted}", f"{base}-{wanted}"]
            candidates.append(base)
    for key in candidates:
        value = _as_float(skus.get(key))
        if value is not None:
            return value
    # 兜底：其它带 second 的 key。分辨率对得上的优先、不带分辨率的其次、别的分辨率不要；
    # 跳过另一档音频价；`cents_` 开头的是美分
    other_audio = "without_audio" if with_audio else "with_audio"
    exact: list[float] = []
    generic: list[float] = []
    for key, raw in skus.items():
        lowered = str(key).lower()
        if "second" not in lowered or other_audio in lowered:
            continue
        if any(word in lowered for word in _NOT_OUTPUT_SECOND):
            continue
        value = _as_float(raw)
        if value is None:
            continue
        if lowered.startswith("cents"):
            value /= 100.0
        tags = [tag for tag in _RESOLUTION_TAGS if tag in lowered]
        if not tags:
            generic.append(value)
        elif wanted in tags:
            exact.append(value)
    if exact:
        return exact[0]
    return generic[0] if generic else None


#: 按 token 计费时每秒的像素量：各家同一档分辨率换比例时像素量基本不变（720p 竖屏 720×1280、
#: 1:1 是 960×960），所以按 16:9 的像素数算
_PIXELS_PER_FRAME = {
    "480p": 864 * 480,
    "720p": 1280 * 720,
    "1080p": 1920 * 1080,
    "2k": 2560 * 1440,
    "4k": 3840 * 2160,
}
#: 视频 token = 宽 × 高 × 帧率 × 秒数 / 1024（Seedance 实收与此吻合：2.0 720p 10 秒 $1.518）
_TOKEN_FPS = 24
_PIXELS_PER_TOKEN = 1024


def _token_price(skus: dict[str, Any], wanted: str, with_audio: bool) -> float | None:
    """`video_tokens*` 价目换算成每秒单价；分辨率不认识就算不出来。"""
    pixels = _PIXELS_PER_FRAME.get(wanted)
    if not pixels:
        return None
    candidates: list[str] = []
    if wanted:
        if not with_audio:
            candidates.append(f"video_tokens_{wanted}_without_audio")
        candidates.append(f"video_tokens_{wanted}")
    if not with_audio:
        candidates.append("video_tokens_without_audio")
    candidates.append("video_tokens")
    for key in candidates:
        value = _as_float(skus.get(key))
        if value is not None:
            return value * pixels * _TOKEN_FPS / _PIXELS_PER_TOKEN
    return None


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


#: 提交时 400 的错误信息在说「带的图有问题」（审核 / 隐私 / 格式 / 尺寸）
_IMAGE_PROBLEM = re.compile(
    r"image|frame|reference|picture|photo|sensitive|privacy|real\s*person", re.IGNORECASE
)

#: 任务失败原因里「图被判敏感」的说法（H3：`input image sensitive (1026)`）
_IMAGE_SENSITIVE = re.compile(r"image\s*sensitive|InputImageSensitive", re.IGNORECASE)


def _image_rejected_in_job(
    reason: str, payload: dict[str, Any], job_id: str
) -> VideoReferenceRejected | None:
    """轮询到的失败原因是「带的图被拒」时，转成 `VideoReferenceRejected`；否则 None。"""
    if not _IMAGE_SENSITIVE.search(reason):
        return None
    if not (payload.get("input_references") or payload.get("frame_images")):
        return None
    kind = "first_frame" if payload.get("frame_images") else "references"
    count = len(payload.get("frame_images") or payload.get("input_references") or [])
    label = "首帧" if kind == "first_frame" else f"{count} 张参考图"
    match = re.search(r"\((\d+)\)", reason) or _ERROR_CODE.search(reason)
    code = match.group(1) if match else ""
    text = scrub_secrets(reason)[:240]
    logger.warning("视频模型拒收了%s（job=%s，%s），中断等用户决定", label, job_id, text)
    return VideoReferenceRejected(
        f"视频模型拒收了{label}（{code or '图片审核'}，job={job_id}）：{text}", kind=kind, code=code
    )


def _frame_mode(caps: dict[str, Any], override: str = "auto") -> str:
    """能力表 → `references` / `first_frame` / `none`（价目里有 reference 计费项才算收参考图）。

    能力表没有「收不收参考图」这一项，只能从价目推断；推断错时用 `VIDEO_FRAME_MODE` 覆盖。
    """
    override = (override or "auto").strip().lower()
    if override in ("references", "first_frame", "none"):
        return override
    skus = caps.get("pricing_skus") or {}
    if isinstance(skus, dict) and any("reference" in str(key).lower() for key in skus):
        return "references"
    if "first_frame" in (caps.get("supported_frame_images") or []):
        return "first_frame"
    return "none"


def _configured_limits(spec: str) -> list[tuple[str, int]]:
    limits: list[tuple[str, int]] = []
    for item in (spec or "").split(","):
        prefix, _, value = item.strip().rpartition("=")
        if prefix and value.strip().isdigit():
            limits.append((prefix.strip(), int(value)))
    return sorted(limits, key=lambda item: -len(item[0]))


def prompt_limit(model: str) -> int:
    """该模型的提示词字符上限（0 = 未知 / 不限）：先看运行中学到的，再看 `VIDEO_PROMPT_LIMITS`。"""
    if model in _LEARNED_PROMPT_LIMITS:
        return _LEARNED_PROMPT_LIMITS[model]
    for prefix, limit in _configured_limits(get_settings().video_prompt_limits):
        if model.startswith(prefix):
            return limit
    return 0


def learn_prompt_limit(model: str, reason: str) -> int:
    """失败原因在说「提示词太长」时记住上限（留 5% 余量），返回记住的值（0 = 不是这类失败）。"""
    lowered = (reason or "").lower()
    at = lowered.find("prompt")
    if at < 0 or not model or not _PROMPT_TOO_LONG.search(lowered[at:]):
        return 0
    numbers = [int(n) for n in re.findall(r"\d{3,6}", lowered[at:])]
    if not numbers:
        return 0
    limit = int(max(numbers) * 0.95)
    _LEARNED_PROMPT_LIMITS[model] = limit
    logger.warning("模型 %s 报提示词超长：记住上限 %d 字符，重试时按它压缩", model, limit)
    return limit


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


