from __future__ import annotations

import hashlib
import logging
import struct
import zlib
from pathlib import Path

from videogen.providers.image.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
)

logger = logging.getLogger(__name__)

#: 占位配色（按 subject_id 哈希稳定选取）
_PALETTE = (
    (43, 58, 103),
    (92, 46, 58),
    (31, 77, 61),
    (74, 59, 107),
    (107, 68, 35),
    (47, 79, 95),
    (81, 58, 92),
    (61, 92, 47),
)

_SIZE = 96

__all__ = ["MockImageProvider"]


class MockImageProvider(ImageProvider):
    """占位图片 provider：不花钱、不依赖 ffmpeg，纯 Python 写一张真 PNG。

    用途是「把链路跑通 + 让测试能断言参考图真的传进来了」：
    - 颜色由 `subject_id`（有参考图时再加上参考图字节）哈希决定
    - 固定 96x96 纯色 PNG，肉眼可辨认、体积几百字节
    """

    name = "mock"
    model = "mock"
    supports_reference_image = True

    async def generate(self, request: ImageRequest) -> GeneratedImage:
        key = request.subject_id
        for reference in request.all_references():
            key = f"{key}:{reference.read_bytes()!r}"
        color = _PALETTE[_stable_index(key, len(_PALETTE))]

        output = Path(request.output_path)
        if output.suffix.lower() != ".png":
            output = output.with_suffix(".png")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_solid_png(_SIZE, _SIZE, color))
        return GeneratedImage(path=output, media_type="image/png", actual_cost_usd=None)


def _stable_index(key: str, modulo: int) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


def _solid_png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """手写一张纯色 PNG（不引第三方图像库）。"""
    row = b"\x00" + bytes(color) * width  # 每行前面是 filter 类型 0
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )
