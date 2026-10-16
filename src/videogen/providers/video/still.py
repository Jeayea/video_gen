"""插图 provider：每个镜头画一张图，本机 ffmpeg 做成推拉 / 平移的片段（科普 `visual_mode=still`）。

实现的是 `VideoProvider` 接口，所以片段清单、记账、preflight、配音、合成全部复用视频那一套。
和视频模型的区别：

- **按张计费**：`planned_seconds()` 对任何时长都返回 1、`price_per_second()` 返回一张图的单价，
  preflight / 预算门控 / 账本因此按「张数 × 单价」算（`unit_kind = "image"`，界面据此显示「张」）
- **图只买一次**：同一个任务里渲染失败重试时复用已经画好的图（画图要钱，渲染不要）
- 片段比镜头长 `slack_sec`：配音比估计的长时，合成不用冻结末帧，推拉还在动
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from videogen.providers.image.base import ImageProvider, ImageProviderError, ImageRequest
from videogen.providers.video.base import (
    GeneratedClip,
    VideoProvider,
    VideoProviderError,
    VideoRequest,
)
from videogen.utils.ffmpeg import FFmpegError, ffmpeg_available, run_ffmpeg

logger = logging.getLogger(__name__)

__all__ = ["StillVideoProvider", "ken_burns_filter"]

#: 输出分辨率（合成时再统一缩放到项目分辨率）
_RESOLUTIONS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}
#: 推拉幅度：从 1.0 推到 1.12（太大像在晃，太小看不出在动）
_ZOOM = 0.12
#: 四种运动，按镜头 id 哈希挑一种（同一个镜头重渲染还是同一种）
MOTIONS = ("zoom_in", "zoom_out", "pan_right", "pan_left")
RENDER_TIMEOUT_SEC = 180.0


class _NotRetryable(VideoProviderError):
    retryable = False


def ken_burns_filter(width: int, height: int, frames: int, fps: int, motion: str) -> str:
    """一张图 → `frames` 帧的推拉 / 平移。先放大到两倍再 zoompan，减轻取整造成的抖动。"""
    last = max(1, frames - 1)
    progress = f"(on/{last})"
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    if motion == "zoom_out":
        zoom, x, y = f"{1 + _ZOOM}-{_ZOOM}*{progress}", center_x, center_y
    elif motion == "pan_right":
        zoom, x, y = f"{1 + _ZOOM}", f"(iw-iw/zoom)*{progress}", center_y
    elif motion == "pan_left":
        zoom, x, y = f"{1 + _ZOOM}", f"(iw-iw/zoom)*(1-{progress})", center_y
    else:  # zoom_in
        zoom, x, y = f"1+{_ZOOM}*{progress}", center_x, center_y
    big_w, big_h = width * 2, height * 2
    return (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps},"
        "setsar=1,format=yuv420p"
    )


def motion_for(shot_id: str) -> str:
    digest = hashlib.sha256(shot_id.encode("utf-8")).digest()
    return MOTIONS[digest[0] % len(MOTIONS)]


class StillVideoProvider(VideoProvider):
    name = "still"
    requires_ffmpeg = True
    supports_reference_image = True
    #: video_service 看到它就改用插图专用的 prompt（`VideoRequest.still_prompt`）
    draws_stills = True
    unit_kind = "image"

    def __init__(
        self,
        image: ImageProvider,
        *,
        price_usd: float,
        fps: int = 24,
        crf: int = 20,
        slack_sec: float = 1.0,
        owns_image: bool = True,
    ) -> None:
        self.image = image
        self.model = f"still:{image.model or image.name}"
        self.paid = image.name != "mock"
        self.price_usd = max(0.0, float(price_usd)) if self.paid else 0.0
        self.fps = max(1, int(fps))
        self.crf = crf
        self.slack_sec = max(0.0, float(slack_sec))
        self._owns_image = owns_image
        #: 输出路径 → (prompt, 图片路径, 花费)：渲染失败重试时不重新买图
        self._drawn: dict[Path, tuple[str, Path, float]] = {}

    async def generate(self, request: VideoRequest) -> GeneratedClip:
        if not ffmpeg_available():
            raise _NotRetryable("插图模式要用 ffmpeg 把图做成片段，但找不到 ffmpeg")
        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        prompt = request.still_prompt or request.prompt
        image_path, cost = await self._draw(request, output, prompt)

        width, height = _RESOLUTIONS.get(request.aspect_ratio, _RESOLUTIONS["9:16"])
        seconds = max(1.0, float(request.duration_sec)) + self.slack_sec
        frames = max(2, round(seconds * self.fps))
        motion = motion_for(request.shot_id)
        try:
            await run_ffmpeg(
                [
                    "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(image_path),
                    "-vf", ken_burns_filter(width, height, frames, self.fps, motion),
                    "-frames:v", str(frames),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.crf),
                    "-pix_fmt", "yuv420p", "-r", str(self.fps),
                    "-movflags", "+faststart",
                    str(output),
                ],
                timeout_sec=RENDER_TIMEOUT_SEC,
            )
        except FFmpegError as exc:
            # 图已经买到了（缓存在 self._drawn），重试只重新渲染
            raise VideoProviderError(f"插图片段渲染失败：{exc}") from exc
        if not output.is_file() or output.stat().st_size == 0:
            raise VideoProviderError("插图片段渲染没有产出文件")
        logger.info(
            "插图片段 %s：%s，%.1fs，%s", request.shot_id, image_path.name, seconds, motion
        )
        # 花费只报一次：同一张图重渲染不再计费
        self._drawn[output] = (prompt, image_path, 0.0)
        return GeneratedClip(
            path=output, duration_sec=int(request.duration_sec), actual_cost_usd=cost
        )

    async def _draw(
        self, request: VideoRequest, output: Path, prompt: str
    ) -> tuple[Path, float]:
        cached = self._drawn.get(output)
        if cached is not None and cached[0] == prompt and cached[1].is_file():
            return cached[1], cached[2]
        try:
            generated = await self.image.generate(
                ImageRequest(
                    subject_id=request.shot_id,
                    prompt=prompt,
                    output_path=output.with_suffix(".png"),
                    aspect_ratio=request.aspect_ratio,
                    reference_images=list(request.reference_images),
                )
            )
        except ImageProviderError as exc:
            error_type = VideoProviderError if exc.retryable else _NotRetryable
            raise error_type(f"插图没画出来：{exc}") from exc
        path = Path(generated.path)
        if not path.is_file() or path.stat().st_size == 0:
            raise _NotRetryable("图片模型没有产出有效文件")
        if not self.paid:
            cost = 0.0
        elif generated.actual_cost_usd is not None:
            cost = max(0.0, generated.actual_cost_usd)
        else:
            cost = self.price_usd
        self._drawn[output] = (prompt, path, cost)
        return path, cost

    async def planned_seconds(self, requested_sec: float) -> float:
        """按张计费：一个镜头一张图，计费单位恒为 1。"""
        return 1.0

    async def price_per_second(self, *, resolution: str = "") -> float | None:
        """一张图的单价（mock 图片 provider 为 0）。"""
        return self.price_usd

    async def aclose(self) -> None:
        if self._owns_image:
            await self.image.aclose()
