from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from videogen.models.video import MediaKind

__all__ = [
    "GeneratedClip",
    "VideoArtifactError",
    "VideoJobPending",
    "VideoProvider",
    "VideoProviderError",
    "VideoReferenceRejected",
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


class VideoJobPending(VideoProviderError):
    """任务已经提交到服务端，但这边没等到结果（轮询超时 / 轮询网络失败 / 提交响应丢了）。

    服务端可能仍在跑、仍会扣费，所以**绝不能重新提交**（那等于同一段买两次）。
    上层把 `job_id` 记下来，下次生成时带着 `VideoRequest.resume_job_id` 接着轮询同一个任务。
    `job_id` 为空表示「连任务有没有建成都不知道」（提交请求发出后响应丢了），只能人工核对。
    """

    retryable = False

    def __init__(self, message: str, *, job_id: str = ""):
        super().__init__(message)
        self.job_id = job_id


class VideoReferenceRejected(VideoProviderError):
    """视频模型拒收了这次带的图（参考图 / 首帧），例如 Seedance 的
    `InputImageSensitiveContentDetected.PrivacyInformation`（写实人脸被判成隐私信息）。

    **不自动降级成「不带图」重试**：那样片子照样出、钱照样花，但角色 / 场景一致性悄悄没了
    （真实事故：《重回2000》6 段里 5 段的首帧被拒，静默降级后每段人物各穿一套衣服）。
    上层要中断生成、交给用户决定：重画首帧 / 换图重试 / 明确同意这一段不带图。
    同样的图重试也是被拒，所以不重试。
    """

    retryable = False

    def __init__(self, message: str, *, kind: str, code: str = ""):
        super().__init__(message)
        #: 被拒的是哪种图：`first_frame`（首帧）/ `references`（参考图）
        self.kind = kind
        #: provider 的错误码（拿不到时为空）
        self.code = code


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
    #: 首帧图（只认首帧、不收参考图的模型用，例如 Kling / Wan）。必须是**一帧画面**
    #: （关键帧），不能是定妆照 —— 否则每段都从一张证件照开场
    first_frame: Path | None = None
    #: 固定 seed（provider 支持时才有用；H3 是 `seed: false`，带上会 400）
    seed: int | None = None
    #: 接着轮询上一次已经提交的任务（不重新提交、不重新计费）。
    #: 任务在服务端已失败时 provider 会重新提交
    resume_job_id: str = ""
    #: 任务一提交成功就回调（参数是 job id）：上层要立刻把 job id 落盘，
    #: 之后不管是取消、超时还是进程重启，都能接着轮询而不是再买一次
    on_submitted: Callable[[str], None] | None = Field(default=None, exclude=True)

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

    async def frame_mode(self) -> str:
        """模型怎么吃图：`references`（全程参考图，如 H3）/ `first_frame`（只认首帧，
        如 Kling / Wan）/ `none`。`first_frame` 时上层可以先画一张关键帧当首帧。"""
        return "references" if self.supports_reference_image else "none"

    async def aclose(self) -> None:
        """释放资源（HTTP 客户端等），默认什么都不做。"""
        return None
