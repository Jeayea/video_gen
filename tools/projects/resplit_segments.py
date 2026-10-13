#!/usr/bin/env python
"""用已经买到的整段视频，按新的切点规则重新切出单镜片段（$0，不调任何 API）。

    # 只看会怎么切（默认，不动任何文件）
    .venv/bin/python tools/projects/resplit_segments.py <project_id>
    # 真的切（先停服务，或至少别在这个项目上跑任务）
    .venv/bin/python tools/projects/resplit_segments.py <project_id> --yes

切完之后重新合成一次即可（合成按片段实际长度进成片）。抽帧缓存 `frames/<shot>.jpg`
会一起删掉，下次需要时重抽。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from videogen.services import video_service  # noqa: E402
from videogen.utils.file_store import FileStore  # noqa: E402

BUSY = ("_generating", "composing")


async def run(project_id: str, *, yes: bool) -> int:
    store = FileStore()
    project = store.load_project(project_id)
    storyboard = store.load_storyboard(project_id)
    manifest = store.load_video_manifest(project_id)
    if project is None or storyboard is None or manifest is None:
        print(f"项目不存在或还没有视频清单：{project_id}")
        return 1
    if any(project.status.value.endswith(tag) for tag in BUSY):
        print(f"项目正在 {project.status.value}，先等任务结束")
        return 1

    rows = await video_service.resplit_segments(
        storyboard,
        manifest,
        store.project_dir(project_id),
        store.clips_dir(project_id),
        dry_run=not yes,
    )
    if not rows:
        print("没有可重切的段落（不是段落分镜生成的，或整段文件已经不在了）")
        return 0
    print(f"{'镜头':18s}{'旧起点':>8s}{'新起点':>8s}{'长度':>8s}")
    for row in rows:
        moved = "  ←" if abs(row["new_offset"] - row["old_offset"]) > 0.05 else ""
        print(
            f"{row['shot_id']:18s}{row['old_offset']:8.3f}{row['new_offset']:8.3f}"
            f"{row['length']:8.3f}{moved}"
        )
    if not yes:
        print("\n（预览，没有改任何文件；加 --yes 真的切）")
        return 0

    store.save_video_manifest(project_id, manifest)
    frames = store.frames_dir(project_id)
    for row in rows:
        for cached in frames.glob(f"{row['shot_id']}.*"):
            cached.unlink(missing_ok=True)
    print(f"\n已重切 {len(rows)} 个镜头并写回 video_manifest.json，重新合成即可")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_id")
    parser.add_argument("--yes", action="store_true", help="真的切（默认只预览）")
    args = parser.parse_args()
    return asyncio.run(run(args.project_id, yes=args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
