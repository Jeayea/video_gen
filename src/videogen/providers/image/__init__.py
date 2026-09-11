from __future__ import annotations

from videogen.config import get_settings
from videogen.providers.image.base import (
    GeneratedImage,
    ImageProvider,
    ImageProviderError,
    ImageRequest,
)
from videogen.providers.image.mock import MockImageProvider
from videogen.providers.image.openrouter import OpenRouterImageProvider

__all__ = [
    "GeneratedImage",
    "ImageProvider",
    "ImageProviderError",
    "ImageRequest",
    "MockImageProvider",
    "OpenRouterImageProvider",
    "get_image_provider",
]


def get_image_provider() -> ImageProvider:
    """按 `IMAGE_PROVIDER` 构造图片 provider（默认 openrouter）。

    - `mock`：纯 Python 写一张纯色 PNG，不花钱，用来跑通链路 / 跑测试
    - `openrouter`：真实图片模型（默认 `bytedance-seed/seedream-4.5`，$0.04/张）
    """
    settings = get_settings()
    name = (settings.image_provider or "openrouter").strip().lower()
    if name == "mock":
        return MockImageProvider()
    if name == "openrouter":
        return OpenRouterImageProvider(model=settings.openrouter_image_model)
    raise ImageProviderError(f"未知的 IMAGE_PROVIDER：{name}（可选：mock | openrouter）")
