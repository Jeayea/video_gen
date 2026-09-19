#!/usr/bin/env python
"""把一张**照片**转成漫画风（图生图），并可直接装成项目里的角色定妆照。

    # 只转风格，落到指定文件
    .venv/bin/python tools/image/restyle.py --input 剧照/曹操.jpg --out .e2e/anime/cao_anime.jpg

    # 转完直接装成项目的角色定妆照（会覆盖 refs/char_N.jpg）
    .venv/bin/python tools/image/restyle.py --input 剧照/曹操.jpg \\
        --project 541bf0406fac --character char_1 --base-url http://127.0.0.1:8097

为什么单独做成工具：角色的 `visual_prompt` 只能「描述长相」，做不到「照着这张脸画」。
图片 provider 本身支持参考图输入（`input_references`），所以拿照片当参考图 + 风格化提示词，
就是一次标准的图生图。**注意**：输入的必须是**你有权使用的图**（自己的截图/素材），
把别人的剧照转成二次元再发布，涉及摄影作品版权与演员肖像权。

风格预设：`anime`（日式赛璐璐）/ `comic`（国漫厚涂）/ `ink`（水墨）—— 都强制保留五官特征。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from videogen.config import get_settings  # noqa: E402
from videogen.providers.image import get_image_provider  # noqa: E402
from videogen.providers.image.base import ImageRequest  # noqa: E402
from videogen.services import cost_service  # noqa: E402
from videogen.utils.file_store import FileStore  # noqa: E402

#: 统一写死「必须保留的特征」，否则模型会顺手换脸
KEEP = (
    "STRICTLY preserve the person's identity: face shape, cheekbones, nose, "
    "eye shape, eyebrows, hairstyle, facial hair, apparent age and the costume. "
    "Front-facing head-and-shoulders portrait on a plain background. "
    "No text, no watermark, no lettering."
)

#: 风格预设：`--preset` 选一个，或用 `--prompt` 自己写
PRESETS = {
    "anime": (
        "Convert this photo into a Japanese cel-shaded 2D anime character design. "
        "Flat cel shading, clean bold line art, simplified stylised shapes, "
        "expressive anime eyes. " + KEEP
    ),
    "comic": (
        "Convert this photo into a Chinese comic (manhua) illustration. "
        "Bold ink outlines, halftone shading, saturated flat colours, dramatic lighting. " + KEEP
    ),
    "ink": (
        "Convert this photo into a traditional Chinese ink-wash painting. "
        "Black inkwork, rice-paper texture, restrained muted colours. " + KEEP
    ),
}


def record_cost(project_id: str, provider: str, model: str, usd: float) -> None:
    """把这张图记进项目账本。

    图片是**真花钱**的，而这个工具跑在流水线外 —— 不记的话「用工具转几版定妆照」
    这些钱就永远不在账本里（和「取消任务不记账」是同一类问题）。
    """
    settings = get_settings()
    store = FileStore(settings.projects_path)
    if not store.project_dir(project_id).is_dir():
        print(f"  （账本跳过：项目 {project_id} 不存在）")
        return
    cost_service.record(
        store,
        project_id,
        cost_service.image_entry(provider, model, 1, actual_usd=usd if usd > 0 else None),
    )
    print(f"  记账：${usd:.4f} → 项目 {project_id}")


def upload(base_url: str, project_id: str, character_id: str, path: Path) -> dict:
    """把转好的图装成角色定妆照（走 base64 上传接口，避免额外依赖）。"""
    payload = {
        "filename": path.name,
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/projects/{project_id}/characters/{character_id}/reference",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": exc.code, "detail": exc.read().decode()[:300]}


async def restyle(source: Path, out: Path, *, prompt: str, resolution: str, aspect: str) -> Path:
    get_settings()  # 触发 .env 加载（provider 工厂自己会读配置）
    provider = get_image_provider()
    try:
        result = await provider.generate(
            ImageRequest(
                subject_id=source.stem,
                prompt=prompt,
                output_path=out,
                aspect_ratio=aspect,
                resolution=resolution,
                reference_image=source,
            )
        )
    finally:
        await provider.aclose()
    return Path(result.path)


def main() -> int:
    parser = argparse.ArgumentParser(description="照片 → 漫画风（图生图）")
    parser.add_argument("--input", required=True, help="源图（必须是你可以使用的图）")
    parser.add_argument("--out", default="", help="输出文件；留空则用 <input>_<preset>.png")
    parser.add_argument("--preset", default="anime", choices=sorted(PRESETS))
    parser.add_argument("--prompt", default="", help="自定义提示词（会覆盖 preset）")
    parser.add_argument("--resolution", default="2K", help="512 / 1K / 2K / 4K")
    parser.add_argument("--aspect", default="1:1")
    parser.add_argument("--project", default="", help="转完装成这个项目的角色定妆照")
    parser.add_argument("--character", default="", help="角色 id（如 char_1）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()

    source = Path(args.input).expanduser()
    if not source.is_file():
        raise SystemExit(f"找不到源图：{source}")
    out = Path(args.out) if args.out else source.with_name(f"{source.stem}_{args.preset}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    prompt = args.prompt.strip() or PRESETS[args.preset]
    print(f"→ 图生图：{source.name} → {out.name}（preset={args.preset}, {args.resolution}）")
    result = asyncio.run(
        restyle(source, out, prompt=prompt, resolution=args.resolution, aspect=args.aspect)
    )
    print(f"  产物：{result}（{result.stat().st_size / 1e6:.2f} MB）")

    if args.project:
        # 图生图也是真花钱：记进账本（单价用配置值，$0.04/张）
        record_cost(
            args.project,
            "openrouter",
            get_settings().openrouter_image_model,
            float(get_settings().image_price_usd),
        )

    if args.project and args.character:
        reply = upload(args.base_url, args.project, args.character, result)
        detail = json.dumps(reply, ensure_ascii=False)[:200]
        print(f"  装入 {args.project}/{args.character}：{detail}")
    elif args.project or args.character:
        print("  （要装成定妆照请同时给 --project 和 --character）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
