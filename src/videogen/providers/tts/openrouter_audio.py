from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import wave
from pathlib import Path
from typing import Any

import httpx

from videogen.config import get_settings
from videogen.providers.tts.base import (
    GeneratedAudio,
    TTSProvider,
    TTSProviderError,
    TTSRequest,
)
from videogen.utils import language
from videogen.utils.ffmpeg import probe_duration
from videogen.utils.redact import scrub_secrets
from videogen.utils.text import clean_spoken_text, estimate_speech_sec

logger = logging.getLogger(__name__)

#: 出现这些状态码时退避重试（语音接口失败不计费，重试安全）
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: 流式音频只支持 pcm16（真实 400 反馈：`audio.format does not support 'mp3' when
#: stream=true. Supported values are: 'pcm16'`）。所以我们要自己封容器。
_STREAM_AUDIO_FORMAT = "pcm16"
#: OpenAI 系音频输出的原始采样参数（pcm16 = 16bit 单声道）
_PCM_SAMPLE_RATE = 24000
_PCM_SAMPLE_WIDTH = 2
_PCM_CHANNELS = 1

#: 把「念法」写进系统提示。云端 TTS 的强项就是能用自然语言描述表演，
#: 但也必须锁死「只朗读、不回答、不改词」。
#:
#: 真实翻车样本（同一段提示词，换个音色就出事）：
#:   输入：薇儿，为什么？
#:   输出转写：薇儿……为什么……难道就这样吗？我……我真的……很……无法理解……
#:            （语气颤抖，声音渐弱）……为什么……你要离开……（深呼吸）
#:   15.15 秒，而且**把舞台提示也念了出来**。
#: 所以规则要写死「一个字都不许加」「括号内容不许念」，并且事后用转写文本校验。
_SYSTEM_TEMPLATE = (
    "你是一个配音引擎（TTS）。唯一任务：把**用户消息里的那一句台词**原样朗读出来。\n"
    "绝对规则（任何情况下都不得违反）：\n"
    "1. 只朗读原文，一个字都不许增加、删除或改写：不许多问一句、不许接话、"
    "不许补充语气词或解释、不许自己编后续台词\n"
    "2. 不要朗读任何括号里的内容（例如「（深呼吸）」「（声音渐弱）」）—— "
    "那些只是给你的表演要求，不是要念出来的字\n"
    "3. 不要念出「台词」「用户」「朗读」「以下是」这类字样\n"
    "4. 念完最后一个字立刻停止，不要有额外的收尾\n"
    "5. 用{language}朗读，像真人演戏一样自然，带真实的气息、停顿与情绪起伏\n"
    "表演要求（只影响怎么念，不影响念什么）：{instruction}"
)

#: 有原声参考时追加的表演要求：照原声的节奏和情绪念，但嗓音是自己的
_GUIDE_TEMPLATE = (
    "\n用户消息里附了一段**参考录音**，是同一句台词的另一个演员版本。"
    "照它的节奏、停顿位置、重音、语速和情绪起伏来念，{length}"
    "但**用你自己的音色**，不要模仿参考录音的嗓音；"
    "参考录音里的字可能念错，**以台词原文为准**。"
)

#: 转写明显超出原文这么多字就算「模型自己加戏」（标点/语气词不算）
_TRANSCRIPT_SLACK_CHARS = 6
#: 转写长度上限 = 原文长度 × 该系数 + 松弛量
_TRANSCRIPT_RATIO = 1.6

#: 没有逐条念法时的兜底（去播音腔）
_DEFAULT_INSTRUCTION = "自然的中文口语，像真人对话，句间有呼吸和停顿，不要播音腔"
_DEFAULT_INSTRUCTION_EN = (
    "natural conversational English, like real people talking, with breaths and pauses, "
    "not a newsreader voice"
)
#: 按台词原文的语言选朗读语言（英文项目的台词是英文，模型就该用英语念）
_LANGUAGE_NAMES = {
    "Mandarin": "中文普通话",
    "English": "地道的英语（English）",
    "Japanese": "日语",
    "Korean": "韩语",
}


class OpenRouterTTSProvider(TTSProvider):
    """OpenRouter 的语音合成（走 `POST /chat/completions` 的音频输出）。

    与 Edge 的区别不只是音质：这里可以给**自然语言的念法**（`instruction`），
    例如「压抑的哭腔，语速慢，句尾发颤」—— 情绪表达力是 Edge 那套
    rate/pitch 参数做不到的。计费按 provider 报的 `usage.cost` 记账。

    实测（2026-09）：音频输出**必须 `stream: true`**，否则直接 400
    `Audio output requires stream: true`。所以这里按 SSE 逐块收 base64 音频，
    拼起来再落盘。
    """

    name = "openrouter"
    requires_ffmpeg = False
    offline = False

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_sec: float | None = None,
        audio_format: str | None = None,
        max_duration_ratio: float = 0.0,
        max_http_retries: int = 3,
        retry_backoff_sec: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = (api_key or settings.openrouter_api_key).strip()
        if not self.api_key:
            raise TTSProviderError(
                "未配置 OPENROUTER_API_KEY，无法使用云端配音（TTS_PROVIDER=openrouter）"
            )
        self.model = model or settings.openrouter_tts_model
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.timeout_sec = max(30.0, timeout_sec or settings.tts_timeout_sec)
        #: 落盘容器。流式接口只给 pcm16，所以这里固定封成 wav（标准库写头，不需要 ffmpeg）
        self.container = (audio_format or settings.tts_audio_format or "wav").lower()
        if self.container != "wav":
            logger.warning(
                "云端 TTS 只能落 wav（流式接口只支持 pcm16），配置的 %s 已忽略", self.container
            )
            self.container = "wav"
        self.max_duration_ratio = max(
            1.0,
            settings.tts_max_duration_ratio if max_duration_ratio <= 0 else max_duration_ratio,
        )
        self.max_http_retries = max(1, max_http_retries)
        self.retry_backoff_sec = max(
            0.0,
            settings.tts_retry_backoff_sec if retry_backoff_sec is None else retry_backoff_sec,
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
        except RuntimeError:  # pragma: no cover - 事件循环已经关了（测试里常见）
            logger.debug("关闭语音 HTTP 客户端时事件循环已结束", exc_info=True)

    # ── 对外接口 ──────────────────────────────────────────

    async def synthesize(self, request: TTSRequest) -> GeneratedAudio:
        text = clean_spoken_text(request.text)
        if not text:
            raise TTSProviderError("配音文本为空（清洗舞台提示后没有内容）")

        # 加戏检测：先按常规提示词合成，转写明显超长就带着更严格的提示重试一次
        result = await self._synthesize_once(request, text, stricter=False)
        if not _transcript_is_verbatim(result.transcript, text):
            logger.warning(
                "配音 %s 转写与原文不符（模型加戏），改用更严格的提示重试：%r",
                request.clip_id,
                result.transcript[:80],
            )
            strict = await self._synthesize_once(request, text, stricter=True)
            result = strict if _transcript_is_verbatim(strict.transcript, text) else result
        return self._guard_duration(result, request, text)

    async def _synthesize_once(
        self, request: TTSRequest, text: str, *, stricter: bool
    ) -> GeneratedAudio:
        data = await self._post(self._payload(request, text, stricter=stricter))
        # 云端接口不返回时长：统一用 ffprobe 读，读不到就交给上层告警（不假装成功）
        return await fill_duration(self._save(data, request))

    def _guard_duration(
        self, result: GeneratedAudio, request: TTSRequest, text: str
    ) -> GeneratedAudio:
        """时长守卫：一句话被念成独角戏时**不能**默默进成片。

        超过容忍倍数直接失败 —— 宁可不配这一句，也不要把 15 秒独白塞进 5 秒的镜头。
        """
        expected = estimate_speech_sec(text)
        if expected <= 0 or result.duration_sec <= 0:
            return result
        limit = max(expected * self.max_duration_ratio, expected + 1.0)
        if result.duration_sec <= limit:
            return result
        raise TTSProviderError(
            f"配音 {request.clip_id} 时长异常：{result.duration_sec:.1f}s，"
            f"原文预计约 {expected:.1f}s（模型自己加了内容）。"
            "换一个音色，或把台词改短后重试"
        )

    # ── 内部实现 ──────────────────────────────────────────

    def _payload(
        self, request: TTSRequest, text: str, *, stricter: bool = False
    ) -> dict[str, Any]:
        instruction = " ".join(str(request.instruction or "").split()) or _DEFAULT_INSTRUCTION
        spoken = language.spoken_language_name(text)
        if spoken == "English" and instruction in {
            _DEFAULT_INSTRUCTION,
            " ".join(get_settings().tts_default_instruction.split()),
        }:
            instruction = _DEFAULT_INSTRUCTION_EN
        if stricter:
            instruction = (
                "这一次只念下面这一句，不要任何多余的字，不要接话，不要念括号里的内容。"
                + instruction
            )
        system = _SYSTEM_TEMPLATE.format(
            instruction=instruction, language=_LANGUAGE_NAMES.get(spoken, "中文普通话")
        )
        user: str | list[dict[str, Any]] = text
        reference = _reference_wav(request.reference_audio)
        if reference:
            length = (
                f"总时长尽量接近 {request.target_sec:.1f} 秒，"
                if request.target_sec > 0
                else ""
            )
            system += _GUIDE_TEMPLATE.format(length=length)
            user = [
                {"type": "text", "text": "参考录音："},
                {"type": "input_audio", "input_audio": {"data": reference, "format": "wav"}},
                {"type": "text", "text": text},
            ]
        return {
            "model": self.model,
            "modalities": ["text", "audio"],
            # 音频输出必须走流式（真实 400 反馈：Audio output requires stream: true）
            "stream": True,
            "audio": {"voice": request.voice or "nova", "format": _STREAM_AUDIO_FORMAT},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # 让 OpenRouter 回传真实花费，账本不靠估算
            "usage": {"include": True},
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发流式请求并拼出完整音频（返回与「非流式响应」同构的字典）。"""
        client = await self._http()
        url = f"{self.base_url}/chat/completions"
        last_error = ""
        for attempt in range(1, self.max_http_retries + 1):
            try:
                return await self._stream_once(client, url, payload)
            except httpx.HTTPError as exc:
                last_error = scrub_secrets(f"{type(exc).__name__}: {exc}")
            except _RetryableStatus as exc:
                last_error = str(exc)
            except TTSProviderError:
                raise  # 已经是可读的业务错误（含 4xx body），不再重试
            if attempt < self.max_http_retries:
                logger.warning(
                    "语音接口第 %d/%d 次失败，准备重试：%s",
                    attempt,
                    self.max_http_retries,
                    last_error,
                )
                await asyncio.sleep(min(15.0, self.retry_backoff_sec * attempt))
        raise TTSProviderError(f"云端配音重试次数用尽：{last_error}")

    async def _stream_once(
        self, client: httpx.AsyncClient, url: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        chunks: list[str] = []
        transcript: list[str] = []
        cost = 0.0
        async with client.stream(
            "POST", url, headers=self._headers(), json=payload
        ) as response:
            if response.status_code in _RETRY_STATUS:
                body = (await response.aread()).decode(errors="replace")[:200]
                raise _RetryableStatus(f"HTTP {response.status_code} {scrub_secrets(body)}")
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")[:300]
                raise TTSProviderError(
                    f"云端配音失败 HTTP {response.status_code}：{scrub_secrets(body)}"
                )
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    logger.debug("语音流里出现非 JSON 行：%s", line[:120])
                    continue
                if isinstance(event, dict) and event.get("error"):
                    raise TTSProviderError(
                        scrub_secrets(f"云端配音返回错误：{event['error']}")[:300]
                    )
                usage = event.get("usage") if isinstance(event, dict) else None
                if isinstance(usage, dict):
                    with contextlib.suppress(TypeError, ValueError):
                        cost = max(cost, float(usage.get("cost") or 0.0))
                choices = event.get("choices") if isinstance(event, dict) else None
                if not isinstance(choices, list) or not choices:
                    continue
                delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                if not isinstance(delta, dict):
                    continue
                audio = delta.get("audio")
                if isinstance(audio, dict):
                    if audio.get("data"):
                        chunks.append(str(audio["data"]))
                    if audio.get("transcript"):
                        transcript.append(str(audio["transcript"]))
        if not chunks:
            raise TTSProviderError("语音接口没有返回音频（模型可能只回了文本）")
        return {
            "audio_b64": "".join(chunks),
            "transcript": "".join(transcript).strip(),
            "cost": cost,
        }

    def _save(self, data: dict[str, Any], request: TTSRequest) -> GeneratedAudio:
        encoded = str(data.get("audio_b64") or "")
        if not encoded:
            raise TTSProviderError("语音接口返回里没有音频数据")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TTSProviderError("语音 base64 解码失败（已生成但拿不到音频）") from exc

        output = Path(request.output_path)
        if output.suffix.lower() != ".wav":
            output = output.with_suffix(".wav")
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write_wav(output, raw)
        except OSError as exc:
            raise TTSProviderError(f"配音落盘失败（已生成但存不下）：{exc}") from exc

        return GeneratedAudio(
            path=output,
            duration_sec=0.0,  # 时长由上层用 ffprobe 补齐（provider 不做 IO）
            voice=str(request.voice or ""),
            usd=float(data.get("cost") or 0.0),
            transcript=str(data.get("transcript") or ""),
        )


#: 比对转写与原文时忽略的标点
_PUNCT_RE = re.compile(r"[\s，。！？…、；：,.!?;:\"'“”‘’（）()《》—-]+")


def _transcript_is_verbatim(transcript: str, text: str) -> bool:
    """转写是否等于原文（没有自己加戏）。

    接口没回转写时按「无法判断」处理 —— 当作通过，绝不误杀。
    """
    got = _PUNCT_RE.sub("", str(transcript or ""))
    want = _PUNCT_RE.sub("", str(text or ""))
    if not got or not want:
        return True
    if len(got) > len(want) * _TRANSCRIPT_RATIO + _TRANSCRIPT_SLACK_CHARS:
        return False
    return want in got or got in want


#: 参考录音的大小上限：一句台词的 16k 单声道 wav 不到 1MB，超了多半是传错了文件
_MAX_REFERENCE_BYTES = 4 * 1024 * 1024


def _reference_wav(path: Path | None) -> str:
    """参考录音 → base64（只收 wav；没有 / 太大 / 读不了都当没有，绝不因此失败）。"""
    if path is None:
        return ""
    reference = Path(path)
    try:
        if reference.suffix.lower() != ".wav" or not reference.is_file():
            return ""
        if reference.stat().st_size > _MAX_REFERENCE_BYTES:
            logger.warning("参考录音过大，已忽略：%s", reference.name)
            return ""
        return base64.b64encode(reference.read_bytes()).decode("ascii")
    except OSError:
        logger.warning("参考录音读不了，已忽略：%s", reference.name, exc_info=True)
        return ""


def _write_wav(output: Path, pcm: bytes) -> None:
    """把 pcm16 裸流封成 WAV（标准库即可，不依赖 ffmpeg）。"""
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(_PCM_CHANNELS)
        handle.setsampwidth(_PCM_SAMPLE_WIDTH)
        handle.setframerate(_PCM_SAMPLE_RATE)
        handle.writeframes(pcm)


class _RetryableStatus(Exception):
    """可重试的 HTTP 状态（音频接口失败不计费，重试安全）。"""


async def fill_duration(result: GeneratedAudio) -> GeneratedAudio:
    """补齐时长（云端接口不给时长，统一用 ffprobe 读）。"""
    if result.duration_sec > 0:
        return result
    duration = await probe_duration(result.path) or 0.0
    return result.model_copy(update={"duration_sec": round(duration, 3)})


__all__ = ["OpenRouterTTSProvider", "fill_duration"]
