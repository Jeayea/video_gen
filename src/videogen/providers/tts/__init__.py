from __future__ import annotations

from videogen.config import get_settings
from videogen.providers.tts.base import (
    GeneratedAudio,
    TTSProvider,
    TTSProviderError,
    TTSRequest,
)
from videogen.providers.tts.edge import EdgeTTSProvider
from videogen.providers.tts.mock import MockTTSProvider

__all__ = [
    "EdgeTTSProvider",
    "GeneratedAudio",
    "MockTTSProvider",
    "TTSProvider",
    "TTSProviderError",
    "TTSRequest",
    "get_tts_provider",
]


def get_tts_provider() -> TTSProvider:
    """按 `TTS_PROVIDER` 配置构造配音 provider（service 只依赖工厂）。"""
    settings = get_settings()
    name = (settings.tts_provider or "edge").strip().lower()
    if name == "edge":
        return EdgeTTSProvider(timeout_sec=settings.tts_timeout_sec)
    if name == "mock":
        return MockTTSProvider()
    raise TTSProviderError(f"未知的 TTS_PROVIDER：{name}")
