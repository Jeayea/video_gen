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
    reference_image: Path | None = None

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

    async def aclose(self) -> None:
        """释放资源（HTTP 客户端等），默认什么都不做。"""
        return None
