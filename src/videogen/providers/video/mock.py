from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from videogen.models.video import MediaKind
from videogen.providers.video.base import (
    GeneratedClip,
    VideoProvider,
    VideoProviderError,
    VideoRequest,
)
from videogen.utils.ffmpeg import FFmpegError, ffmpeg_available, run_ffmpeg

logger = logging.getLogger(__name__)

#: 占位片段配色（按 shot_id 哈希稳定选取）
_PALETTE = (
    "0x2b3a67",
    "0x5c2e3a",
    "0x1f4d3d",
    "0x4a3b6b",
    "0x6b4423",
    "0x2f4f5f",
    "0x513a5c",
    "0x3d5c2f",
)

#: 目标分辨率（按比例），mock 用半分辨率，够验证流程也够快
_RESOLUTIONS = {
    "9:16": (540, 960),
    "16:9": (960, 540),
    "1:1": (540, 540),
}


class MockVideoProvider(VideoProvider):
    """占位视频 provider：不花钱，用来把整条流水线跑通。

    - 有 ffmpeg 时：生成真正可播放的 H.264 MP4（纯色背景 + 横向扫过的白条，
      用来一眼确认「这是视频不是静帧」）
    - 没有 ffmpeg 时：写一个 `.placeholder.json` 旁挂文件，并把 `media_kind`
      标成 `placeholder` —— 明确标记，绝不当成真视频混进合成
    """

    name = "mock"
    requires_ffmpeg = False  # 没有 ffmpeg 也能降级工作
    #: 支持参考图：mock 用参考图取色，方便肉眼确认「参考图真的传到了 provider」
    supports_reference_image = True

    def __init__(self, fps: int = 24, crf: int = 30) -> None:
        self.fps = fps
        self.crf = crf

    async def generate(self, request: VideoRequest) -> GeneratedClip:
        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        width, height = _RESOLUTIONS.get(request.aspect_ratio, _RESOLUTIONS["9:16"])
        duration = max(1, int(request.duration_sec))
        key = request.shot_id
        reference_used = ""
        if request.reference_image and Path(request.reference_image).is_file():
            # 用参考图内容决定配色：换了参考图，画面颜色一定变
            key = f"{request.shot_id}:{Path(request.reference_image).read_bytes()!r}"
            reference_used = str(request.reference_image)
        color = _PALETTE[_stable_index(key, len(_PALETTE))]

        if not ffmpeg_available():
            return self._write_placeholder(request, output, width, height, color)

        bar_width = max(8, width // 12)
        sweep = (
            f"drawbox=x='mod(t*{width // 2}\\,{width})':y=0:"
            f"w={bar_width}:h={height}:color=white@0.85:t=fill"
        )
        args = [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={width}x{height}:d={duration}:r={self.fps}",
            "-vf",
            sweep,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            str(self.crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-t",
            str(duration),
            str(output),
        ]
        try:
            await run_ffmpeg(args, timeout_sec=max(30.0, duration * 8.0))
        except FFmpegError as exc:
            raise VideoProviderError(f"占位视频生成失败（shot={request.shot_id}）：{exc}") from exc

        if not output.exists() or output.stat().st_size == 0:
            raise VideoProviderError(f"ffmpeg 未产出有效文件（shot={request.shot_id}）")

        logger.info(
            "mock clip ok shot=%s %dx%d %ds seed=%s reference=%s",
            request.shot_id,
            width,
            height,
            duration,
            request.seed or "-",
            reference_used or "-",
        )
        return GeneratedClip(
            path=output,
            media_kind=MediaKind.VIDEO,
            duration_sec=duration,
            task_id=f"mock-{request.shot_id}",
        )

    def _write_placeholder(
        self,
        request: VideoRequest,
        output: Path,
        width: int,
        height: int,
        color: str,
    ) -> GeneratedClip:
        """没有 ffmpeg 时的降级产物：明确标记为占位，不含真实媒体。"""
        stub = output.with_suffix(".placeholder.json")
        stub.write_text(
            json.dumps(
                {
                    "kind": "videogen.placeholder",
                    "shot_id": request.shot_id,
                    "reference_image": str(request.reference_image or ""),
                    "seed": request.seed,
                    "scene_id": request.scene_id,
                    "prompt": request.prompt,
                    "duration_sec": request.duration_sec,
                    "width": width,
                    "height": height,
                    "color": color,
                    "note": (
                        "未检测到 ffmpeg，这是占位文件而不是视频；"
                        "安装 ffmpeg 后重新生成即可得到真实 MP4"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.warning(
            "未检测到 ffmpeg，shot=%s 只写了占位文件（media_kind=placeholder）", request.shot_id
        )
        return GeneratedClip(
            path=stub,
            media_kind=MediaKind.PLACEHOLDER,
            duration_sec=max(1, int(request.duration_sec)),
            task_id=f"placeholder-{request.shot_id}",
        )


def _stable_index(key: str, modulo: int) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo
