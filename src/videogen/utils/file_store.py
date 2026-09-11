from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from videogen.config import get_settings
from videogen.models.bible import StoryBible
from videogen.models.consistency import ConsistencyReport
from videogen.models.project import (
    LEGACY_STATUS_MAP,
    LEGACY_VIDEO_READY,
    LEGACY_VIDEO_READY_TARGET,
    Project,
    ProjectStatus,
)
from videogen.models.script import Script, ScriptOutline
from videogen.models.storyboard import Storyboard
from videogen.models.video import VideoManifest
from videogen.models.world_state import WorldState

T = TypeVar("T", bound=BaseModel)

#: 项目 id 只允许这几种字符，避免 `..` 之类的路径穿越
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class FileStore:
    """项目产物的文件系统存储。每个项目一个目录，JSON 文件落盘。

    - 写入是原子的（临时文件 + os.replace），不会留下半截 JSON
    - 读取路径不做 mkdir，不存在就是不存在的
    """

    def __init__(self, base_dir: Path | None = None):
        self.base = base_dir or get_settings().projects_path
        self.base.mkdir(parents=True, exist_ok=True)

    # ── 路径 ──────────────────────────────────────────

    @staticmethod
    def is_valid_id(project_id: str) -> bool:
        return bool(project_id and _ID_RE.match(project_id))

    def project_dir(self, project_id: str) -> Path:
        return self.base / project_id

    def ensure_project_dir(self, project_id: str) -> Path:
        if not self.is_valid_id(project_id):
            raise ValueError(f"非法项目 id: {project_id!r}")
        d = self.project_dir(project_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _json_path(self, project_id: str, name: str) -> Path:
        return self.project_dir(project_id) / f"{name}.json"

    # ── 读写 ──────────────────────────────────────────

    def _save(self, project_id: str, name: str, data: BaseModel) -> None:
        directory = self.ensure_project_dir(project_id)
        path = directory / f"{name}.json"
        payload = data.model_dump_json(indent=2)
        # 原子写：先写临时文件再 replace，避免并发/中断留下损坏 JSON
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _load(self, project_id: str, name: str, model_cls: type[T]) -> T | None:
        if not self.is_valid_id(project_id):
            return None
        path = self._json_path(project_id, name)
        if not path.exists():
            return None
        return model_cls.model_validate_json(path.read_text(encoding="utf-8"))

    # --- Project ---
    def save_project(self, project: Project) -> None:
        self._save(project.id, "meta", project)

    def remove_artifact(self, project_id: str, name: str) -> None:
        """删除某个产物文件（例如重新生成剧本时清掉过期分镜）。"""
        if not self.is_valid_id(project_id):
            return
        self._json_path(project_id, name).unlink(missing_ok=True)

    def load_project(self, project_id: str) -> Project | None:
        project = self._load(project_id, "meta", Project)
        if project is None:
            return None
        return self._migrate(project)

    def _migrate(self, project: Project) -> Project:
        """把旧状态值迁移到新状态机，避免老项目被卡在无效状态。"""
        current = project.status.value
        target = LEGACY_STATUS_MAP.get(current)
        if (
            target is None
            and current == LEGACY_VIDEO_READY
            # Phase 2 落地前，旧 VIDEO_READY 实际等价于「分镜已确认」
            and not (self.project_dir(project.id) / "video_manifest.json").exists()
        ):
            target = LEGACY_VIDEO_READY_TARGET
        if target:
            project.status = ProjectStatus(target)
        if project.target_duration_sec <= 0:
            project.target_duration_sec = 45
        return project

    def list_projects(self) -> list[Project]:
        projects = []
        for d in sorted(self.base.iterdir()):
            if d.is_dir() and (d / "meta.json").exists():
                p = self.load_project(d.name)
                if p:
                    projects.append(p)
        return projects

    def delete_project(self, project_id: str) -> bool:
        if not self.is_valid_id(project_id):
            return False
        directory = self.project_dir(project_id)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory)
        return True

    # --- Bible ---
    def save_bible(self, project_id: str, bible: StoryBible) -> None:
        self._save(project_id, "story_bible", bible)

    def load_bible(self, project_id: str) -> StoryBible | None:
        return self._load(project_id, "story_bible", StoryBible)

    # --- Outline（断点续跑用）---
    def save_outline(self, project_id: str, outline: ScriptOutline) -> None:
        self._save(project_id, "outline", outline)

    def load_outline(self, project_id: str) -> ScriptOutline | None:
        return self._load(project_id, "outline", ScriptOutline)

    # --- Script ---
    def save_script(self, project_id: str, script: Script) -> None:
        self._save(project_id, "script", script)

    def load_script(self, project_id: str) -> Script | None:
        return self._load(project_id, "script", Script)

    # --- World State ---
    def save_world_state(self, project_id: str, ws: WorldState) -> None:
        self._save(project_id, "world_state", ws)

    def load_world_state(self, project_id: str) -> WorldState | None:
        return self._load(project_id, "world_state", WorldState)

    # --- Storyboard ---
    def save_storyboard(self, project_id: str, sb: Storyboard) -> None:
        self._save(project_id, "storyboard", sb)

    def load_storyboard(self, project_id: str) -> Storyboard | None:
        return self._load(project_id, "storyboard", Storyboard)

    # --- Consistency ---
    def save_consistency(self, project_id: str, report: ConsistencyReport) -> None:
        self._save(project_id, "consistency_report", report)

    def load_consistency(self, project_id: str) -> ConsistencyReport | None:
        return self._load(project_id, "consistency_report", ConsistencyReport)

    # --- Video ---
    def save_video_manifest(self, project_id: str, manifest: VideoManifest) -> None:
        self._save(project_id, "video_manifest", manifest)

    def load_video_manifest(self, project_id: str) -> VideoManifest | None:
        return self._load(project_id, "video_manifest", VideoManifest)

    def resolve_artifact(self, project_id: str, relative: str) -> Path | None:
        """把产物相对路径安全地解析成绝对路径（禁止越出项目目录）。"""
        if not self.is_valid_id(project_id) or not relative:
            return None
        root = self.project_dir(project_id).resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    # --- Clips / Audio / Output dirs（Phase 2+）---
    def clips_dir(self, project_id: str) -> Path:
        d = self.ensure_project_dir(project_id) / "clips"
        d.mkdir(exist_ok=True)
        return d

    def audio_dir(self, project_id: str) -> Path:
        d = self.ensure_project_dir(project_id) / "audio"
        d.mkdir(exist_ok=True)
        return d

    def output_dir(self, project_id: str) -> Path:
        d = self.ensure_project_dir(project_id) / "output"
        d.mkdir(exist_ok=True)
        return d
