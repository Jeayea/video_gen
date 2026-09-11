from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

from videogen.models.video import MediaKind

__all__ = [
    "GeneratedAudio",
    "TTSProvider",
    "TTSProviderError",
    "TTSRequest",
]


class TTSProviderError(RuntimeError):
    """配音失败（消息必须已脱敏）。"""


class TTSRequest(BaseModel):
    """一句台词的配音请求。"""

    clip_id: str
    text: str
    voice: str = ""
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"
    output_path: Path

    model_config = {"arbitrary_types_allowed": True}


class GeneratedAudio(BaseModel):
    """provider 的返回：只描述产物，不掺项目状态。"""

    path: Path
    media_kind: MediaKind = MediaKind.AUDIO
    #: 语音时长（秒）。provider 必须尽力给出真实值，拿不到就给 0 让上层告警
    duration_sec: float = 0.0
    voice: str = ""


class TTSProvider(ABC):
    """配音适配器。

    约定：
    - `synthesize()` 把一句台词写成 `request.output_path` 指向的音频文件
    - 失败必须抛 `TTSProviderError`，不要产出空文件假装成功
    - 没有 ffmpeg 的实现应显式返回 `media_kind=placeholder`，让上层拒绝合成
    """

    name: str = "base"
    requires_ffmpeg: bool = False
    #: 是否能在不联网的情况下工作（mock 是 True）
    offline: bool = False

    @abstractmethod
    async def synthesize(self, request: TTSRequest) -> GeneratedAudio:
        """合成一句台词。"""

    async def aclose(self) -> None:
        """释放资源，默认什么都不做。"""
        return None
