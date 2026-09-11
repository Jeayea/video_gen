from __future__ import annotations

from videogen.config import get_settings
from videogen.providers.video.base import (
    GeneratedClip,
    VideoProvider,
    VideoProviderError,
    VideoRequest,
)
from videogen.providers.video.mock import MockVideoProvider
from videogen.providers.video.openrouter import (
    OpenRouterVideoProvider,
    clear_video_capability_cache,
)

__all__ = [
    "GeneratedClip",
    "MockVideoProvider",
    "OpenRouterVideoProvider",
    "VideoProvider",
    "VideoProviderError",
    "VideoRequest",
    "clear_video_capability_cache",
    "get_video_provider",
]


def get_video_provider() -> VideoProvider:
    """按 `VIDEO_PROVIDER` 配置构造视频 provider（service 只依赖工厂）。

    - `mock`：不花钱的占位片段（默认，先把链路跑通）
    - `openrouter`：真实视频模型（默认 `minimax/hailuo-3` = MiniMax H3），按秒计费
    """
    settings = get_settings()
    name = (settings.video_provider or "mock").strip().lower()
    if name == "mock":
        return MockVideoProvider(fps=settings.video_fps, crf=settings.video_crf)
    if name == "openrouter":
        return OpenRouterVideoProvider(
            model=settings.openrouter_video_model,
            resolution=settings.video_resolution,
            generate_audio=settings.video_generate_audio,
            use_reference_images=settings.video_use_reference_images,
        )
    raise VideoProviderError(
        f"未知的 VIDEO_PROVIDER：{name}（可选：mock | openrouter）"
    )
