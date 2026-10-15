"""作废的媒体不直接删，先挪进 `history/` 留一份。

片段是按秒花真钱买的，成片是一轮完整的产出：「重做这段」「改分镜」「换模型重跑」
「重新合成」以前都是**先删旧的**，新的再失败 / 被拒 / 更差，旧的就找不回来了
（真实场景：《重回2000》重跑时首帧全被拒，没有手动备份的话原来 13 镜和成片已经没了）。

    projects/<id>/history/<时间戳>-<原因>/
        clips/scene_1_shot_1.mp4        ← 保持在项目里的相对路径
        clips/segments/scene_1_seg_1.mp4
        output/final.mp4
        history.json                     ← 原因、时间、文件清单、片段当时的 prompt / 扣费

- **挪，不复制**：同一块磁盘上 `os.replace` 不占额外空间
- **有上限**：最多留 `MEDIA_HISTORY_KEEP` 批、合计不超过 `MEDIA_HISTORY_MAX_MB`，超了从最旧的删；
  最新的一批永远留着。`MEDIA_HISTORY_KEEP=0` = 关掉留存（回到直接删除）
- 只留**花钱或费时的产物**（片段 / 配音 / 成片 / 字幕）；抽帧、合成中间缓存照删
- **能恢复片段**：`video_service.restore_clips` 把一批里的片段拷回 `clips/`（拷贝，历史里那份还在）
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from videogen.config import Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = ["HISTORY_DIR", "archive", "list_entries", "load_entry", "prune", "resolve_file"]

HISTORY_DIR = "history"
_META = "history.json"
_SLUG_UNSAFE = re.compile(r"[^\w一-鿿-]+")
_ENTRY_NAME = re.compile(r"^[\w一-鿿-]+$")


def archive(
    project_dir: Path,
    files: Iterable[Path | str],
    *,
    reason: str,
    extra: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> Path | None:
    """把这些文件（项目目录下的绝对或相对路径）挪进一批新的历史版本，返回这批的目录。

    留存关掉时直接删除（老行为）。没有任何文件存在时什么都不做、返回 None。
    """
    settings = settings or get_settings()
    root = Path(project_dir)
    history = root / HISTORY_DIR
    found: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for item in files:
        path = Path(item)
        path = path if path.is_absolute() else root / path
        try:
            relative = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue  # 不在项目目录里的不碰
        if relative.parts and relative.parts[0] == HISTORY_DIR:
            continue
        if relative in seen or not path.is_file():
            continue
        seen.add(relative)
        found.append((path, relative))
    if not found:
        return None

    if settings.media_history_keep <= 0:
        for path, _ in found:
            path.unlink(missing_ok=True)
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    slug = _SLUG_UNSAFE.sub("-", reason).strip("-")[:40] or "replaced"
    entry = history / f"{stamp}-{slug}"
    entry.mkdir(parents=True, exist_ok=True)
    listed = []
    for path, relative in found:
        target = entry / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        size = path.stat().st_size
        try:
            os.replace(path, target)
        except OSError:  # 跨盘时退回拷贝 + 删除
            shutil.move(str(path), str(target))
        listed.append({"path": relative.as_posix(), "bytes": size})
    meta = {
        "reason": reason,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": listed,
        **(extra or {}),
    }
    (entry / _META).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("旧媒体已留存 %d 个文件 → %s（%s）", len(listed), entry.name, reason)
    prune(root, settings=settings)
    return entry


def prune(project_dir: Path, *, settings: Settings | None = None) -> list[str]:
    """超出批数 / 总大小上限时从最旧的删（最新一批永远留着），返回删掉的批名。"""
    settings = settings or get_settings()
    entries = _entries(Path(project_dir))
    keep = max(1, settings.media_history_keep)
    limit = max(0.0, settings.media_history_max_mb) * 1024 * 1024
    sizes = {entry: _size(entry) for entry in entries}
    removed: list[str] = []
    while len(entries) > 1 and (
        len(entries) > keep or (limit and sum(sizes[e] for e in entries) > limit)
    ):
        oldest = entries.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)
        removed.append(oldest.name)
    if removed:
        logger.info("历史版本超出上限，已删除最旧的 %d 批：%s", len(removed), ", ".join(removed))
    return removed


def list_entries(project_dir: Path) -> list[dict[str, Any]]:
    """最新的在前：批名、原因、时间、文件清单、总大小。"""
    result = []
    for entry in reversed(_entries(Path(project_dir))):
        try:
            meta = json.loads((entry / _META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        files = meta.get("files") or [
            {"path": p.relative_to(entry).as_posix(), "bytes": p.stat().st_size}
            for p in sorted(entry.rglob("*"))
            if p.is_file() and p.name != _META
        ]
        files = [f for f in files if (entry / f["path"]).is_file()]
        result.append(
            {
                "id": entry.name,
                "reason": meta.get("reason", ""),
                "at": meta.get("at", ""),
                "files": files,
                "bytes": sum(int(f.get("bytes") or 0) for f in files),
                "clips": meta.get("clips") or [],
            }
        )
    return result


def load_entry(project_dir: Path, entry_id: str) -> tuple[Path, dict[str, Any]] | None:
    """一批历史版本的目录和 history.json（批名不合法 / 不存在返回 None）。"""
    if not _ENTRY_NAME.match(entry_id or ""):
        return None
    entry = Path(project_dir) / HISTORY_DIR / entry_id
    if not entry.is_dir():
        return None
    try:
        meta = json.loads((entry / _META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return entry, meta if isinstance(meta, dict) else {}


def resolve_file(project_dir: Path, entry_id: str, relative: str) -> Path | None:
    """历史版本里的一个文件（禁止越出这一批的目录）。"""
    if not _ENTRY_NAME.match(entry_id or ""):
        return None
    entry = (Path(project_dir) / HISTORY_DIR / entry_id).resolve()
    if not entry.is_dir():
        return None
    candidate = (entry / relative).resolve()
    try:
        candidate.relative_to(entry)
    except ValueError:
        return None
    if candidate.name == _META or not candidate.is_file():
        return None
    return candidate


def _entries(project_dir: Path) -> list[Path]:
    history = project_dir / HISTORY_DIR
    if not history.is_dir():
        return []
    return sorted(p for p in history.iterdir() if p.is_dir())


def _size(entry: Path) -> int:
    return sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
