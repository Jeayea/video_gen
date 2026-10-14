from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from videogen.config import get_settings
from videogen.models.audio import AudioManifest
from videogen.models.bible import StoryBible
from videogen.models.compose import ComposeReport
from videogen.models.consistency import ConsistencyReport
from videogen.models.cost import CostReport
from videogen.models.novel import NovelSource
from videogen.models.project import (
    LEGACY_STATUS_MAP,
    LEGACY_VIDEO_READY,
    LEGACY_VIDEO_READY_TARGET,
    Project,
    ProjectStatus,
)
from videogen.models.script import Script, ScriptOutline, StoryOutline
from videogen.models.storyboard import Storyboard
from videogen.models.video import VideoManifest
from videogen.models.world_state import WorldState
from videogen.utils import media_history
from videogen.utils.naming import derive_title, project_slug

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: 项目 id = 目录名：只允许字母 / 数字（含中文）/ `_` / `-`，没有 `.` 和 `/`，避免路径穿越
_ID_RE = re.compile(r"\w[\w-]{0,63}")


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
        return bool(project_id and _ID_RE.fullmatch(project_id))

    def project_dir(self, project_id: str) -> Path:
        return self.base / project_id

    def allocate_id(self, base: str) -> str:
        """按名字占一个新目录：`base`、`base-2`、`base-3`…（mkdir 独占创建，并发建项不撞名）。"""
        base = base[:60].rstrip("-_") or "project"
        for n in range(1, 1000):
            candidate = base if n == 1 else f"{base}-{n}"
            if not self.is_valid_id(candidate):
                raise ValueError(f"非法项目 id: {candidate!r}")
            try:
                self.project_dir(candidate).mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            return candidate
        raise RuntimeError(f"同名项目太多：{base!r}")

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
        """读一个产物 JSON；坏文件**不抛异常**，隔离成 `.bad` 后返回 None。

        真实风险（review 复现过）：一个手改坏 / 版本不兼容的 `meta.json` 会让
        `list_projects()` 直接抛 ValidationError —— 整个 `/api/projects` 变成 500，
        一个坏项目把全部项目都带下水。这里改成「坏的那个当作不存在」，
        并且**重命名而不是删除**（坏文件本身是排查线索，改好名字就能恢复）。
        """
        if not self.is_valid_id(project_id):
            return None
        path = self._json_path(project_id, name)
        if not path.exists():
            return None
        try:
            return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # pydantic 的 ValidationError 是 ValueError 的子类，一起兜住
            self._quarantine(path, exc)
            return None

    def _quarantine(self, path: Path, exc: Exception) -> None:
        """把损坏的产物改名成 `<名字>.bad`（不删除），并留下可操作的告警。"""
        target = path.with_name(f"{path.name}.bad")
        try:
            path.replace(target)
        except OSError:
            logger.warning("产物损坏且无法隔离：%s（%s）", path, exc)
            return
        logger.warning(
            "产物损坏，已隔离为 %s（原因：%s）——修好后改回 %s 即可恢复",
            target.name,
            exc,
            path.name,
        )

    # --- Project ---
    def create_project(self, project: Project) -> Project:
        """新建项目：标题没填就从原文里取，目录名按「日期-标题」起（见 utils/naming.py）。"""
        project.title = derive_title(project.title, project.input_text, project.input_mode.value)
        project.id = self.allocate_id(
            project_slug(project.title, project.created_at, project.input_mode.value)
        )
        self.save_project(project)
        return project

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

    # --- Story Outline（长篇剧集大纲，20+ 章）---
    def save_story_outline(self, project_id: str, outline: StoryOutline) -> None:
        self._save(project_id, "story_outline", outline)

    def load_story_outline(self, project_id: str) -> StoryOutline | None:
        return self._load(project_id, "story_outline", StoryOutline)

    # --- Novel（小说原文切章 + 逐章抽取）---
    def save_novel(self, project_id: str, novel: NovelSource) -> None:
        self._save(project_id, "novel", novel)

    def load_novel(self, project_id: str) -> NovelSource | None:
        return self._load(project_id, "novel", NovelSource)

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

    # --- Audio（配音）---
    def save_audio_manifest(self, project_id: str, manifest: AudioManifest) -> None:
        self._save(project_id, "audio_manifest", manifest)

    def load_audio_manifest(self, project_id: str) -> AudioManifest | None:
        return self._load(project_id, "audio_manifest", AudioManifest)

    # --- Cost（成本账本）---
    def save_cost_report(self, project_id: str, report: CostReport) -> None:
        self._save(project_id, "cost_report", report)

    def load_cost_report(self, project_id: str) -> CostReport | None:
        return self._load(project_id, "cost_report", CostReport)

    # --- Compose（成片）---
    def save_compose_report(self, project_id: str, report: ComposeReport) -> None:
        self._save(project_id, "compose_report", report)

    def load_compose_report(self, project_id: str) -> ComposeReport | None:
        return self._load(project_id, "compose_report", ComposeReport)

    def purge_media(
        self,
        project_id: str,
        *,
        video: bool = False,
        audio: bool = False,
        compose: bool = False,
        reason: str = "上游内容变了",
        keep_history: bool = True,
    ) -> None:
        """上游产物变了就作废下游媒体，避免拿着过期分镜的片子去合成。

        只动清单 + 媒体目录，不动 meta/剧本/分镜这些文本产物。片段 / 配音 / 成片与它们的清单
        先挪进 `history/` 留一份（`utils/media_history.py`），剩下的缓存（抽帧、合成中间段、
        口型产物）照删。`keep_history=False` 用于「这份产物本身就是坏的」
        （例如合成失败的半截成片）。
        """
        if not self.is_valid_id(project_id):
            return
        directory = self.project_dir(project_id)
        if not directory.is_dir():
            return
        if keep_history:
            self.archive_media(project_id, video=video, audio=audio, compose=compose, reason=reason)
        if video:
            self.remove_artifact(project_id, "video_manifest")
            shutil.rmtree(directory / "clips", ignore_errors=True)
            # 抽帧是从片段里来的，片段没了帧也必须一起走
            shutil.rmtree(directory / "frames", ignore_errors=True)
        if audio:
            self.remove_artifact(project_id, "audio_manifest")
            shutil.rmtree(directory / "audio", ignore_errors=True)
        if compose:
            self.remove_artifact(project_id, "compose_report")
            shutil.rmtree(directory / "output", ignore_errors=True)

    def archive_media(
        self,
        project_id: str,
        *,
        video: bool = False,
        audio: bool = False,
        compose: bool = False,
        reason: str,
    ) -> Path | None:
        """把片段 / 配音 / 成片（连同清单）挪进 `history/` 的一批新版本，返回这批的目录。

        成片只留 `output/` 根目录下的交付物（final.mp4、字幕、报告），子目录是合成缓存。
        """
        if not self.is_valid_id(project_id):
            return None
        directory = self.project_dir(project_id)
        if not directory.is_dir():
            return None
        keep: list[Path] = []
        if video:
            keep += _files_under(directory / "clips", recursive=True)
            keep.append(self._json_path(project_id, "video_manifest"))
        if audio:
            keep += _files_under(directory / "audio", recursive=True)
            keep.append(self._json_path(project_id, "audio_manifest"))
        if compose:
            keep += _files_under(directory / "output", recursive=False)
            keep.append(self._json_path(project_id, "compose_report"))
        return media_history.archive(directory, keep, reason=reason)

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

    def refs_dir(self, project_id: str) -> Path:
        """角色参考图目录（角色一致性用）。"""
        d = self.ensure_project_dir(project_id) / "refs"
        d.mkdir(exist_ok=True)
        return d

    def frames_dir(self, project_id: str) -> Path:
        """视频抽帧目录（配音文案让 LLM 看画面用）。"""
        d = self.ensure_project_dir(project_id) / "frames"
        d.mkdir(exist_ok=True)
        return d

    def segments_dir(self, project_id: str) -> Path:
        """合成中间产物（每个镜头一段），失败可复用已渲染好的段。"""
        d = self.output_dir(project_id) / "segments"
        d.mkdir(exist_ok=True)
        return d


def _files_under(directory: Path, *, recursive: bool) -> list[Path]:
    if not directory.is_dir():
        return []
    items = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(p for p in items if p.is_file())

