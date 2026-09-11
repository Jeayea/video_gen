from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

from videogen.models.video import MediaKind

__all__ = [
    "GeneratedClip",
    "VideoProvider",
    "VideoProviderError",
    "VideoRequest",
]


class VideoProviderError(RuntimeError):
    """视频生成失败（消息必须已脱敏）。"""


class VideoRequest(BaseModel):
    """一次单镜头视频生成请求。"""

    shot_id: str
    scene_id: str = ""
    prompt: str
    duration_sec: int
    aspect_ratio: str = "9:16"
    output_path: Path
    #: 角色一致性：参考图 + 固定 seed（provider 不支持时为空）
    reference_image: Path | None = None
    seed: int | None = None

    model_config = {"arbitrary_types_allowed": True}


class GeneratedClip(BaseModel):
    """provider 的返回：只描述产物，不掺项目状态。"""

    path: Path
    media_kind: MediaKind = MediaKind.VIDEO
    duration_sec: int = 0
    task_id: str | None = None


class VideoProvider(ABC):
    """视频生成适配器。

    约定：
    - `generate()` 负责把单个镜头写成 `request.output_path` 指向的文件
    - 失败必须抛 `VideoProviderError`（或子类），不要返回空文件假装成功
    - 远程异步 API（可灵 / Runway 等）在 `generate()` 内部自行轮询，
      或用 `check_status()` + `task_id` 暴露给上层
    """

    #: 写进 manifest 的 provider 标识
    name: str = "base"
    #: 当前使用的模型（写进 manifest / 成本账本；mock 就是 "mock"）
    model: str = ""
    #: 是否支持参考图（角色一致性用）
    supports_reference_image: bool = False
    #: 是否需要 ffmpeg
    requires_ffmpeg: bool = False

    @abstractmethod
    async def generate(self, request: VideoRequest) -> GeneratedClip:
        """生成一个镜头的视频片段。"""

    async def check_status(self, task_id: str) -> str:
        """查询远程任务状态（本地 provider 不需要，默认不支持）。"""
        raise VideoProviderError(f"{self.name} 不支持 check_status")

    async def planned_seconds(self, requested_sec: float) -> float:
        """按模型支持的档位，算出这个镜头**实际会被计费**的秒数。

        真实模型常有最小时长（例如 MiniMax H3 最短 5 秒），估预算时必须按它算，
        否则 3 秒的镜头会被低估成 3 秒。默认实现返回原样（mock 就是精确的）。
        """
        return max(0.0, float(requested_sec))

    async def price_per_second(self, *, resolution: str = "") -> float | None:
        """向 provider 问单价（USD/秒）；问不到返回 None，让上层用配置的单价。"""
        return None

    async def aclose(self) -> None:
        """释放资源（HTTP 客户端等），默认什么都不做。"""
        return None
