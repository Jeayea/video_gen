from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from videogen.models.video import MediaKind
from videogen.providers.tts.base import (
    GeneratedAudio,
    TTSProvider,
    TTSProviderError,
    TTSRequest,
)
from videogen.utils.ffmpeg import FFmpegError, ffmpeg_available, run_ffmpeg
from videogen.utils.text import clean_spoken_text, estimate_speech_sec

logger = logging.getLogger(__name__)


class MockTTSProvider(TTSProvider):
    """占位配音：不联网、不花钱，用来把合成链路跑通。

    - 有 ffmpeg：按字数估算时长，生成一段**真实可播放**的单音 mp3
    - 没有 ffmpeg：写 `.placeholder.json` 并标 `media_kind=placeholder`，
      上层（compose）会因为这些占位产物直接拒绝合成
    """

    name = "mock"
    requires_ffmpeg = False
    offline = True

    async def synthesize(self, request: TTSRequest) -> GeneratedAudio:
        text = clean_spoken_text(request.text)
        duration = estimate_speech_sec(text)
        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        if not ffmpeg_available():
            return self._write_placeholder(request, output, duration)

        frequency = 190 + _stable_index(request.clip_id, 8) * 34
        args = [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=24000:duration={duration}",
            "-af",
            "volume=0.25",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-ar",
            "24000",
            "-ac",
            "1",
            str(output),
        ]
        try:
            await run_ffmpeg(args, timeout_sec=max(20.0, duration * 4))
        except FFmpegError as exc:
            raise TTSProviderError(f"占位配音生成失败（clip={request.clip_id}）：{exc}") from exc

        if not output.exists() or output.stat().st_size == 0:
            raise TTSProviderError(f"ffmpeg 未产出有效音频（clip={request.clip_id}）")

        logger.info("mock 配音完成 clip=%s 时长=%.2fs（占位音）", request.clip_id, duration)
        return GeneratedAudio(path=output, duration_sec=duration, voice=request.voice or "mock")

    def _write_placeholder(
        self, request: TTSRequest, output: Path, duration: float
    ) -> GeneratedAudio:
        stub = output.with_suffix(".placeholder.json")
        stub.write_text(
            json.dumps(
                {
                    "kind": "videogen.audio_placeholder",
                    "clip_id": request.clip_id,
                    "text": request.text,
                    "duration_sec": duration,
                    "note": "未检测到 ffmpeg，这是占位文件而不是音频",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.warning("未检测到 ffmpeg，clip=%s 只写了配音占位文件", request.clip_id)
        return GeneratedAudio(
            path=stub,
            media_kind=MediaKind.PLACEHOLDER,
            duration_sec=duration,
            voice=request.voice or "mock",
        )


def _stable_index(key: str, modulo: int) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo
