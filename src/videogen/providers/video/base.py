from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field

from videogen.models.video import MediaKind

__all__ = [
    "GeneratedClip",
    "VideoArtifactError",
    "VideoProvider",
    "VideoProviderError",
    "VideoRequest",
]


class VideoProviderError(RuntimeError):
    """视频生成失败（消息必须已脱敏）。

    `retryable=True` 表示「重试一次可能成功」，上层会按配置重试；
    重试是要花钱的，所以只有**还没生成成功**的失败才允许重试。
    """

    retryable: bool = True


class VideoArtifactError(VideoProviderError):
    """生成成功、但产物下载/落盘失败。

    这类错误**绝对不能自动重试** —— 重新提交等于把已经生成好的片段再买一遍。
    异常里带上 job id 与候选地址，好让人工把片子捞回来。
    """

    retryable = False

    def __init__(self, message: str, *, job_id: str = "", urls: list[str] | None = None):
        super().__init__(message)
        self.job_id = job_id
        self.urls = list(urls or [])


class VideoRequest(BaseModel):
    """一次单镜头视频生成请求。"""

    shot_id: str
    scene_id: str = ""
    prompt: str
    duration_sec: int
    aspect_ratio: str = "9:16"
    output_path: Path
    #: 角色一致性：画面里每个配了参考图的角色各一张（provider 不支持时为空）。
    #: 双人镜头要同时锁住两个人，所以这里是**列表**而不是单张。
    reference_images: list[Path] = Field(default_factory=list)
    #: 固定 seed（provider 支持时才有用；H3 是 `seed: false`，带上会 400）
    seed: int | None = None

    model_config = {"arbitrary_types_allowed": True}

    @property
    def reference_image(self) -> Path | None:
        """第一张参考图（老调用方/只支持单图的 provider 用这个）。"""
        return self.reference_images[0] if self.reference_images else None


class GeneratedClip(BaseModel):
    """provider 的返回：只描述产物，不掺项目状态。"""

    path: Path
    media_kind: MediaKind = MediaKind.VIDEO
    duration_sec: int = 0
    task_id: str | None = None
    #: provider 报的实际扣费（USD）。真实 API 会给，mock 为 None —— 记账以它为准
    actual_cost_usd: float | None = None


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

    async def duration_options(self) -> list[float]:
        """模型支持的时长档位（秒，升序）；返回空列表 = 没有档位限制。

        成本前置（preflight）用它算「最少要买多少秒」和「合并成几个镜头最省」。
        """
        return []

    async def price_per_second(self, *, resolution: str = "") -> float | None:
        """向 provider 问单价（USD/秒）；问不到返回 None，让上层用配置的单价。"""
        return None

    async def aclose(self) -> None:
        """释放资源（HTTP 客户端等），默认什么都不做。"""
        return None
