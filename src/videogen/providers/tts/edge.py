from __future__ import annotations

import logging
from pathlib import Path

from videogen.providers.tts.base import (
    GeneratedAudio,
    TTSProvider,
    TTSProviderError,
    TTSRequest,
)
from videogen.utils.ffmpeg import probe_duration
from videogen.utils.redact import scrub_secrets
from videogen.utils.text import clean_spoken_text, estimate_speech_sec

logger = logging.getLogger(__name__)

#: edge-tts 输出格式固定为 24kHz/48kbps 单声道 mp3
_BYTES_PER_SEC = 6000.0
#: 边界事件通常不覆盖最后几个音素，补一点尾巴
_TAIL_PAD_SEC = 0.12


class EdgeTTSProvider(TTSProvider):
    """微软 Edge TTS（`edge-tts` 包，免费、无需 key，但要联网）。

    时长获取顺序：
    1. `ffprobe` 读真实时长（最准，但 imageio-ffmpeg 不带 ffprobe）
    2. 服务返回的边界事件（WordBoundary / SentenceBoundary）末位 + 尾巴补偿
    3. 按码率从字节数估算（48kbps）

    中文语音只会返回 `SentenceBoundary`（不是 WordBoundary），所以两者都要认。
    """

    name = "edge"
    requires_ffmpeg = False
    offline = False

    def __init__(self, *, timeout_sec: float = 60.0) -> None:
        self.timeout_sec = timeout_sec

    async def synthesize(self, request: TTSRequest) -> GeneratedAudio:
        text = clean_spoken_text(request.text)
        if not text:
            raise TTSProviderError("配音文本为空（清洗舞台提示后没有内容）")

        try:
            import edge_tts
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise TTSProviderError("未安装 edge-tts，请执行 `pip install edge-tts`") from exc

        voice = request.voice or "zh-CN-XiaoxiaoNeural"
        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=request.rate or "+0%",
            volume=request.volume or "+0%",
            pitch=request.pitch or "+0Hz",
        )

        chunks = bytearray()
        boundary_sec = 0.0
        try:
            async for chunk in communicate.stream():
                kind = chunk.get("type")
                if kind == "audio":
                    chunks.extend(chunk.get("data", b""))
                elif kind in {"WordBoundary", "SentenceBoundary"}:
                    # 时间单位是 100ns
                    end = (int(chunk.get("offset", 0)) + int(chunk.get("duration", 0))) / 1e7
                    boundary_sec = max(boundary_sec, end)
        except Exception as exc:
            raise TTSProviderError(
                scrub_secrets(f"edge-tts 合成失败（voice={voice}）：{type(exc).__name__}: {exc}")
            ) from exc

        if not chunks:
            raise TTSProviderError("edge-tts 没有返回音频数据（检查网络或音色名）")

        output.write_bytes(bytes(chunks))

        duration = await probe_duration(output) or 0.0
        if duration <= 0:
            duration = boundary_sec + _TAIL_PAD_SEC if boundary_sec > 0 else 0.0
        if duration <= 0:
            duration = len(chunks) / _BYTES_PER_SEC
            logger.debug("edge-tts 时长按码率估算：%.2fs", duration)

        logger.info(
            "edge 配音完成 clip=%s 音色=%s 时长=%.2fs 字节=%d",
            request.clip_id,
            voice,
            duration,
            len(chunks),
        )
        return GeneratedAudio(
            path=output,
            duration_sec=round(duration, 3),
            voice=voice,
        )


__all__ = ["EdgeTTSProvider", "estimate_speech_sec"]
