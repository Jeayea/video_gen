from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

__all__ = [
    "GeneratedImage",
    "ImageArtifactError",
    "ImageProvider",
    "ImageProviderError",
    "ImageRequest",
]


class ImageProviderError(RuntimeError):
    """图片生成失败（消息必须已脱敏）。

    `retryable=True` 表示「重试一次可能成功」；OpenRouter 的图片接口是
    **全有或全无**计费（失败不计费），所以真正的失败重试是安全的。
    """

    retryable: bool = True


class ImageArtifactError(ImageProviderError):
    """生成成功、但产物解码/落盘失败。

    这类错误不重试：重新提交等于把已经生成好的图再买一遍。
    """

    retryable = False


class ImageRequest(BaseModel):
    """一次角色图生成请求。"""

    #: 生成这张图是为了谁（写进文件名 / 日志）
    subject_id: str
    prompt: str
    output_path: Path
    aspect_ratio: str = "1:1"
    #: 分辨率档位（`512` / `1K` / `2K` / `4K`），留空 = 模型默认
    resolution: str = ""
    #: 参考图（图生图）：用于「基于已有角色图再出一个角度」
    reference_image: Path | None = None
    model_config = {"arbitrary_types_allowed": True}


class GeneratedImage(BaseModel):
    """provider 的返回：只描述产物与花费，不掺项目状态。"""

    path: Path
    media_type: str = "image/png"
    #: provider 报的实际扣费（USD）；没报就是 None，由上层按单价估算
    actual_cost_usd: float | None = None


class ImageProvider(ABC):
    """图片生成适配器（目前用于「AI 生成角色参考图」）。"""

    name: str = "base"
    model: str = ""
    #: 是否支持参考图（图生图）
    supports_reference_image: bool = False

    @abstractmethod
    async def generate(self, request: ImageRequest) -> GeneratedImage:
        """生成一张图并写到 `request.output_path`。"""

    async def aclose(self) -> None:
        """释放底层资源（默认什么都不做，子类按需覆盖）。"""
        return None
