"""黑场上的画面文字（黑底白字的字幕卡，如冷开场后的「八个月前」）。Pillow 本机渲染，$0。"""

from __future__ import annotations

from pathlib import Path

#: 字幕卡用的字体：优先宋体（叙事感），没有就黑体；都找不到时用 Pillow 自带字体（中文会缺字）
_FONTS = (
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
TEXT_RGB = (232, 232, 228)


def render(text: str, width: int, height: int, path: Path) -> Path:
    """在 `width×height` 的黑底中央写一行（或几行）字，存成 PNG。"""
    from PIL import Image, ImageDraw, ImageFont

    size = max(18, round(min(width, height) * 0.045))
    font = None
    for candidate in _FONTS:
        if Path(candidate).is_file():
            try:
                font = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default(size)
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    lines = [line.strip() for line in text.splitlines() if line.strip()] or [""]
    spacing = round(size * 0.6)
    boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    heights = [b[3] - b[1] for b in boxes]
    y = (height - (sum(heights) + spacing * (len(lines) - 1))) / 2
    for line, box, h in zip(lines, boxes, heights, strict=True):
        x = (width - (box[2] - box[0])) / 2 - box[0]
        draw.text((x, y - box[1]), line, font=font, fill=TEXT_RGB)
        y += h + spacing
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path
