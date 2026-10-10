#!/usr/bin/env python
"""把老项目的随机目录名（`2fabf66fbb35`）改成「日期-标题」（`20261010-孔乙己-第一场试拍-wan-3-0`）。

    # 只看会怎么改（默认，不动任何文件）
    .venv/bin/python tools/projects/rename_legacy.py
    # 真的改（先停服务：内存里的任务还认旧 id）
    .venv/bin/python tools/projects/rename_legacy.py --yes

- 只改 12 位十六进制的老 id；已经是新规则的目录不动
- 目录改名后，项目里 JSON / Markdown 中出现的旧 id（`meta.json` 的 `id`、
  写成绝对路径的 `projects/<旧 id>/…`）一并替换
- 标题是「未命名项目」的，按原文补一个标题（与新建项目同一规则）
- 生成中（`*_generating` / `composing`）的项目跳过
- 新旧对照追加写进 `projects/renamed.json`，想改回去照着它改即可
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from videogen.utils.file_store import FileStore  # noqa: E402
from videogen.utils.naming import derive_title, project_slug  # noqa: E402

LEGACY_ID = re.compile(r"[0-9a-f]{12}")
BUSY = ("_generating", "composing")
TEXT_SUFFIXES = {".json", ".md"}


def plan(store: FileStore) -> list[dict]:
    taken = {d.name for d in store.base.iterdir() if d.is_dir()}
    rows = []
    for directory in sorted(store.base.iterdir()):
        meta_path = directory / "meta.json"
        if not directory.is_dir() or not LEGACY_ID.fullmatch(directory.name):
            continue
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        status = str(meta.get("status") or "")
        mode = str(meta.get("input_mode") or "")
        title = derive_title(str(meta.get("title") or ""), str(meta.get("input_text") or ""), mode)
        base = project_slug(title, str(meta.get("created_at") or ""), mode)[:60].rstrip("-_")
        new_id, n = base, 1
        while new_id in taken:
            n += 1
            new_id = f"{base}-{n}"
        taken.add(new_id)
        rows.append(
            {
                "old": directory.name,
                "new": new_id,
                "title": title,
                "title_changed": title != meta.get("title"),
                "skip": status.endswith(BUSY),
                "status": status,
            }
        )
    return rows


def apply(store: FileStore, row: dict) -> None:
    old_dir = store.base / row["old"]
    new_dir = store.base / row["new"]
    old_dir.rename(new_dir)  # 同一个文件系统内是原子的；目标已存在会直接报错
    for path in new_dir.rglob("*"):
        if path.suffix not in TEXT_SUFFIXES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if row["old"] in text:
            path.write_text(text.replace(row["old"], row["new"]), encoding="utf-8")
    meta_path = new_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["id"] = row["new"]
    meta["title"] = row["title"]
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="真的改名（默认只打印计划）")
    args = parser.parse_args()

    store = FileStore()
    rows = plan(store)
    if not rows:
        print("没有需要改名的老项目")
        return 0
    for row in rows:
        mark = "跳过（生成中）" if row["skip"] else "→"
        note = f"  [补标题：{row['title']}]" if row["title_changed"] else ""
        print(f"{row['old']}  {mark}  {row['new']}{note}")
    if not args.yes:
        print("\n只是预览；确认无误后加 --yes 执行（先停服务）")
        return 0

    log_path = store.base / "renamed.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.is_file() else []
    done = 0
    for row in rows:
        if row["skip"]:
            continue
        apply(store, row)
        log.append(
            {"old": row["old"], "new": row["new"], "at": datetime.now(UTC).isoformat()}
        )
        done += 1
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已改名 {done} 个项目，对照表：{log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
