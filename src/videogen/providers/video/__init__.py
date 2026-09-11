from __future__ import annotations

from videogen.config import get_settings
from videogen.providers.video.base import (
    GeneratedClip,
    VideoProvider,
    VideoProviderError,
    VideoRequest,
)
from videogen.providers.video.mock import MockVideoProvider

__all__ = [
    "GeneratedClip",
    "MockVideoProvider",
    "VideoProvider",
    "VideoProviderError",
    "VideoRequest",
    "get_video_provider",
]


def get_video_provider() -> VideoProvider:
    """按 `VIDEO_PROVIDER` 配置构造视频 provider（service 只依赖工厂）。"""
    settings = get_settings()
    name = (settings.video_provider or "mock").strip().lower()
    if name == "mock":
        return MockVideoProvider(fps=settings.video_fps, crf=settings.video_crf)
    raise VideoProviderError(f"未知的 VIDEO_PROVIDER：{name}")
