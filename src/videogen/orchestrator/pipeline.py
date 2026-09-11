from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from videogen.config import get_settings
from videogen.models.bible import StoryBible
from videogen.models.consistency import ConsistencyReport
from videogen.models.project import Project, ProjectStatus
from videogen.models.script import Script, ScriptOutline
from videogen.models.storyboard import Storyboard
from videogen.models.video import VideoManifest
from videogen.models.world_state import WorldState
from videogen.orchestrator.jobs import Job, JobContext, JobManager
from videogen.providers import llm as llm_providers
from videogen.providers import video as video_providers
from videogen.providers.llm.base import LLMProvider
from videogen.providers.video.base import VideoProvider
from videogen.services import (
    bible_service,
    consistency_service,
    script_service,
    storyboard_service,
    video_service,
)
from videogen.utils.file_store import FileStore

logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """流水线业务错误（状态不允许、前置产物缺失、生成失败等）。"""


class PipelineNotFound(PipelineError):
    """项目或产物不存在。"""


# ── 各操作的入口状态白名单（唯一事实来源）──────────────────

GENERATE_BIBLE_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED}
GENERATE_SCRIPT_STATUSES = {
    ProjectStatus.DRAFT,
    ProjectStatus.FAILED,
    ProjectStatus.SCRIPT_REVIEW,
}
CONFIRM_SCRIPT_STATUSES = {ProjectStatus.SCRIPT_REVIEW}
REOPEN_SCRIPT_STATUSES = {
    ProjectStatus.SCRIPT_CONFIRMED,
    ProjectStatus.STORYBOARD_REVIEW,
    ProjectStatus.STORYBOARD_CONFIRMED,
}
GENERATE_STORYBOARD_STATUSES = {
    ProjectStatus.SCRIPT_CONFIRMED,
    ProjectStatus.STORYBOARD_REVIEW,
    ProjectStatus.FAILED,
}
CONFIRM_STORYBOARD_STATUSES = {ProjectStatus.STORYBOARD_REVIEW}
REOPEN_STORYBOARD_STATUSES = {ProjectStatus.STORYBOARD_CONFIRMED}

EDIT_BIBLE_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED, ProjectStatus.SCRIPT_REVIEW}
EDIT_SCRIPT_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED, ProjectStatus.SCRIPT_REVIEW}
EDIT_STORYBOARD_STATUSES = {ProjectStatus.SCRIPT_CONFIRMED, ProjectStatus.STORYBOARD_REVIEW}

#: 视频生成入口：确认分镜后开跑，或在 VIDEO_READY 下补跑失败/未生成的镜头。
#: 允许 VIDEO_GENERATING 是为了让「任务已死但状态没收拾干净」的项目也能手动重试。
GENERATE_VIDEO_STATUSES = {
    ProjectStatus.STORYBOARD_CONFIRMED,
    ProjectStatus.VIDEO_READY,
    ProjectStatus.VIDEO_GENERATING,
}

#: 进程重启后需要收尾的「进行中」状态
TRANSIENT_STATUSES = {
    ProjectStatus.VIDEO_GENERATING,
    ProjectStatus.AUDIO_GENERATING,
    ProjectStatus.COMPOSING,
}

VIDEO_JOB_KIND = "video"


class Pipeline:
    """项目流水线编排：管理阶段流转、人工确认门控和断点续跑。

    所有会改变项目状态的操作都：
    1. 拿项目级 asyncio 锁（避免并发写同一份 JSON）
    2. 校验入口状态（不允许的操作直接拒绝，不做隐式状态跳跃）
    3. 失败时保留已完成产物（逐场落盘）并把项目标为 FAILED 以便续跑
    """

    def __init__(
        self,
        store: FileStore | None = None,
        llm_factory: Callable[[], LLMProvider] | None = None,
        video_factory: Callable[[], VideoProvider] | None = None,
        jobs: JobManager | None = None,
    ):
        self.store = store or FileStore()
        self._llm_factory = llm_factory or (lambda: llm_providers.get_llm())
        self._video_factory = video_factory or (lambda: video_providers.get_video_provider())
        self.jobs = jobs or JobManager()
        self._locks: dict[str, asyncio.Lock] = {}

    # ── 基础设施 ──────────────────────────────────────────

    def _lock(self, project_id: str) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[project_id] = lock
        return lock

    def _llm(self) -> LLMProvider:
        return self._llm_factory()

    def _video(self) -> VideoProvider:
        return self._video_factory()

    def get_project(self, project_id: str) -> Project:
        project = self.store.load_project(project_id)
        if not project:
            raise PipelineNotFound(f"项目不存在: {project_id}")
        return project

    @staticmethod
    def _require(project: Project, allowed: set[ProjectStatus], action: str) -> None:
        if project.status not in allowed:
            expected = "/".join(sorted(s.value for s in allowed))
            raise PipelineError(
                f"当前状态 '{project.status.value}' 不允许{action}（需要 {expected}）"
            )

    def _fail(self, project: Project, action: str) -> None:
        project.status = ProjectStatus.FAILED
        self.store.save_project(project)
        logger.warning("%s失败，项目 %s 标记 FAILED（已完成产物保留，可续跑）", action, project.id)

    # ── 项目元信息 ────────────────────────────────────────

    async def update_project(self, project_id: str, fields: dict) -> Project:
        """更新标题 / 比例 / 目标时长（不影响已生成产物）。"""
        async with self._lock(project_id):
            project = self.get_project(project_id)
            data = project.model_dump()
            for key, value in fields.items():
                if value is not None and key in data:
                    data[key] = value
            try:
                updated = Project.model_validate(data)
            except Exception as exc:
                raise PipelineError(f"项目更新失败：{exc}") from exc
            self.store.save_project(updated)
            return updated

    # ── 故事圣经 ──────────────────────────────────────────

    async def generate_bible(self, project_id: str) -> StoryBible:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, GENERATE_BIBLE_STATUSES, "生成故事圣经")
            llm = self._llm()

            try:
                bible = await bible_service.generate_bible(project, llm)
            except Exception as exc:
                self._fail(project, "故事圣经生成")
                raise PipelineError(f"故事圣经生成失败：{exc}") from exc

            self.store.save_bible(project_id, bible)
            # 圣经变了，旧的一致性报告作废
            self.store.remove_artifact(project_id, "consistency_report")
            return bible

    async def update_bible(self, project_id: str, data: dict) -> StoryBible:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, EDIT_BIBLE_STATUSES, "修改故事圣经")
            try:
                bible = StoryBible.model_validate(data)
            except Exception as exc:
                raise PipelineError(f"圣经更新失败：{exc}") from exc
            self.store.save_bible(project_id, bible)
            self._revalidate(project, bible, self.store.load_script(project_id))
            return bible

    # ── 剧本 ──────────────────────────────────────────────

    async def generate_script(self, project_id: str) -> Script:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, GENERATE_SCRIPT_STATUSES, "生成剧本（需先退回剧本阶段）")

            bible = self.store.load_bible(project_id)
            if not bible:
                raise PipelineError("故事圣经不存在，请先生成圣经")

            llm = self._llm()
            try:
                script, world_state = await self._run_script_generation(project, bible, llm)
            except Exception as exc:
                self._fail(project, "剧本生成")
                raise PipelineError(f"剧本生成失败：{exc}") from exc

            report = consistency_service.validate_script(
                script, bible, world_state, project.target_duration_sec
            )
            self.store.save_consistency(project_id, report)

            project.status = ProjectStatus.SCRIPT_REVIEW
            self.store.save_project(project)
            logger.info(
                "剧本完成 %s：%d 场 / %ds（目标 %ds），%d 条校验问题",
                project_id,
                len(script.scenes),
                script.total_duration_sec,
                project.target_duration_sec,
                len(report.issues),
            )
            return script

    async def _run_script_generation(
        self, project: Project, bible: StoryBible, llm: LLMProvider
    ) -> tuple[Script, WorldState]:
        """逐场生成剧本，每场生成后立即落盘，失败可续跑。"""
        project_id = project.id
        resuming = project.status == ProjectStatus.FAILED
        outline: ScriptOutline | None = self.store.load_outline(project_id) if resuming else None

        if outline is None:
            outline = await script_service.generate_outline(llm, project, bible)
            self.store.save_outline(project_id, outline)
            script = Script(title=bible.style.genre or project.title, logline="")
            world_state = WorldState()
            # 重新生成剧本会让旧分镜失效
            self.store.remove_artifact(project_id, "storyboard")
            self.store.save_script(project_id, script)
            self.store.save_world_state(project_id, world_state)
        else:
            script = self.store.load_script(project_id) or Script(
                title=bible.style.genre or project.title, logline=""
            )
            world_state = self.store.load_world_state(project_id) or WorldState()
            logger.info(
                "续跑剧本 %s：已完成 %d/%d 场",
                project_id,
                len(script.scenes),
                len(outline.scenes),
            )

        done = script.scene_ids()
        for outline_scene in outline.scenes:
            if outline_scene.id in done:
                continue
            scene, response = await script_service.generate_scene(
                llm,
                project,
                bible,
                outline_scene,
                script.scenes,
                world_state,
                len(outline.scenes),
            )
            script.scenes.append(scene)
            world_state.apply_delta(response.world_state_delta)
            # 逐场落盘：中途失败也不会丢掉已经生成的场次
            self.store.save_script(project_id, script)
            self.store.save_world_state(project_id, world_state)

        script_service.normalize_script_durations(script, project.target_duration_sec)
        script.logline = await script_service.generate_logline(llm, script)
        script.title = script.title or project.title
        self.store.save_script(project_id, script)
        return script, world_state

    async def confirm_script(self, project_id: str) -> Project:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, CONFIRM_SCRIPT_STATUSES, "确认剧本")
            project.status = ProjectStatus.SCRIPT_CONFIRMED
            self.store.save_project(project)
            return project

    async def reopen_script(self, project_id: str) -> Project:
        """把已确认的剧本退回可编辑/可重生成状态。"""
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, REOPEN_SCRIPT_STATUSES, "退回剧本阶段")
            project.status = ProjectStatus.DRAFT
            self.store.save_project(project)
            return project

    async def update_script(self, project_id: str, data: dict) -> Script:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, EDIT_SCRIPT_STATUSES, "修改剧本")
            try:
                script = Script.model_validate(data)
            except Exception as exc:
                raise PipelineError(f"剧本更新失败：{exc}") from exc
            self.store.save_script(project_id, script)
            self._revalidate(project, self.store.load_bible(project_id), script)
            return script

    # ── 分镜 ──────────────────────────────────────────────

    async def generate_storyboard(self, project_id: str) -> Storyboard:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, GENERATE_STORYBOARD_STATUSES, "生成分镜（需先确认剧本）")

            script = self.store.load_script(project_id)
            bible = self.store.load_bible(project_id)
            if not script or not script.scenes:
                raise PipelineError("剧本不存在或为空，无法生成分镜")
            if not bible:
                raise PipelineError("故事圣经不存在，无法生成分镜")

            outline = self.store.load_outline(project_id)
            if outline and len(script.scenes) < len(outline.scenes):
                raise PipelineError(
                    f"剧本不完整（{len(script.scenes)}/{len(outline.scenes)} 场），请先生成完整剧本"
                )

            llm = self._llm()
            try:
                storyboard = await self._run_storyboard_generation(project, script, bible, llm)
            except Exception as exc:
                self._fail(project, "分镜生成")
                raise PipelineError(f"分镜生成失败：{exc}") from exc

            script_report = consistency_service.validate_script(
                script, bible, self.store.load_world_state(project_id), project.target_duration_sec
            )
            issues = consistency_service.validate_storyboard(storyboard, script, bible)
            report = consistency_service.merge_storyboard_issues(script_report, issues)
            self.store.save_consistency(project_id, report)

            project.status = ProjectStatus.STORYBOARD_REVIEW
            self.store.save_project(project)
            logger.info(
                "分镜完成 %s：%d 镜 / %ds，%d 条校验问题",
                project_id,
                len(storyboard.shots),
                storyboard.total_duration_sec,
                len(report.issues),
            )
            return storyboard

    async def _run_storyboard_generation(
        self,
        project: Project,
        script: Script,
        bible: StoryBible,
        llm: LLMProvider,
    ) -> Storyboard:
        project_id = project.id
        resuming = project.status == ProjectStatus.FAILED
        storyboard = self.store.load_storyboard(project_id) if resuming else None
        if storyboard is None or not storyboard.shots:
            storyboard = Storyboard()
        else:
            logger.info(
                "续跑分镜 %s：已覆盖 %d/%d 场",
                project_id,
                len(storyboard.scene_ids()),
                len(script.scenes),
            )

        covered = storyboard.scene_ids() if resuming else set()
        for scene in script.scenes:
            if scene.id in covered:
                continue
            shots = await storyboard_service.generate_shots_for_scene(llm, project, bible, scene)
            storyboard.shots.extend(shots)
            self.store.save_storyboard(project_id, storyboard)

        storyboard_service.normalize_storyboard_durations(storyboard, script)
        self.store.save_storyboard(project_id, storyboard)
        return storyboard

    async def confirm_storyboard(self, project_id: str) -> Project:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, CONFIRM_STORYBOARD_STATUSES, "确认分镜")
            # 只表示「分镜已确认」。视频/配音/合成阶段尚未实现，不虚报 VIDEO_READY
            project.status = ProjectStatus.STORYBOARD_CONFIRMED
            self.store.save_project(project)
            return project

    async def reopen_storyboard(self, project_id: str) -> Project:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, REOPEN_STORYBOARD_STATUSES, "退回分镜阶段")
            project.status = ProjectStatus.SCRIPT_CONFIRMED
            self.store.save_project(project)
            return project

    async def update_storyboard(self, project_id: str, data: dict) -> Storyboard:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, EDIT_STORYBOARD_STATUSES, "修改分镜")
            try:
                storyboard = Storyboard.model_validate(data)
            except Exception as exc:
                raise PipelineError(f"分镜更新失败：{exc}") from exc
            self.store.save_storyboard(project_id, storyboard)
            self._revalidate(
                project, self.store.load_bible(project_id), self.store.load_script(project_id)
            )
            return storyboard

    # ── 视频阶段 ──────────────────────────────────────────

    async def start_video_generation(self, project_id: str) -> Job:
        """提交视频生成后台任务，立即返回 Job（进度用 video_status 查）。"""
        # 快速路径：正在跑的任务会一直持有项目锁，所以必须先看任务表，
        # 否则重复提交会阻塞到上一个任务跑完（真实 provider 可能是几分钟）。
        self._reject_if_video_running(project_id)

        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._reject_if_video_running(project_id)
            self._require(project, GENERATE_VIDEO_STATUSES, "生成视频（需先确认分镜）")

            storyboard = self.store.load_storyboard(project_id)
            if not storyboard or not storyboard.shots:
                raise PipelineError("分镜不存在或为空，无法生成视频")

            project.status = ProjectStatus.VIDEO_GENERATING
            self.store.save_project(project)

        return self.jobs.submit(
            project_id,
            VIDEO_JOB_KIND,
            lambda ctx: self._run_video_job(ctx, project_id),
        )

    def _reject_if_video_running(self, project_id: str) -> None:
        running = self.jobs.active_for(project_id, VIDEO_JOB_KIND)
        if running:
            raise PipelineError(f"该项目已有视频生成任务在运行（job={running.id}）")

    async def _run_video_job(self, ctx: JobContext, project_id: str) -> None:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            storyboard = self.store.load_storyboard(project_id) or Storyboard()
            provider = self._video()
            project_dir = self.store.ensure_project_dir(project_id)
            clips_dir = self.store.clips_dir(project_id)
            settings = get_settings()

            def on_progress(manifest: VideoManifest) -> None:
                self.store.save_video_manifest(project_id, manifest)
                done = len(manifest.ready_clips) + len(manifest.failed_clips)
                ctx.set_progress(
                    done=done,
                    total=len(manifest.clips),
                    message=f"片段 {done}/{len(manifest.clips)}（成功 {len(manifest.ready_clips)}，"
                    f"失败 {len(manifest.failed_clips)}）",
                )

            try:
                manifest = await video_service.generate_clips(
                    project,
                    storyboard,
                    provider,
                    clips_dir,
                    project_dir,
                    concurrency=settings.video_concurrency,
                    max_retries=settings.video_max_retries,
                    retry_backoff_sec=settings.video_retry_backoff_sec,
                    existing=self.store.load_video_manifest(project_id),
                    on_progress=on_progress,
                )
            except asyncio.CancelledError:
                # 取消：保留已生成片段，状态退回可重试的位置
                self._settle_video_status(project, self.store.load_video_manifest(project_id))
                raise
            finally:
                await provider.aclose()

            self.store.save_video_manifest(project_id, manifest)
            self._settle_video_status(project, manifest)
            ctx.set_progress(
                done=len(manifest.clips),
                total=len(manifest.clips),
                message=(
                    f"完成：成功 {len(manifest.ready_clips)}，"
                    f"失败 {len(manifest.failed_clips)}"
                ),
            )
            if not manifest.ready_clips:
                raise PipelineError(
                    f"全部 {len(manifest.clips)} 个片段生成失败，请检查 provider 配置 / ffmpeg"
                )

    def _settle_video_status(self, project: Project, manifest: VideoManifest | None) -> None:
        """有可用片段就进 VIDEO_READY，否则退回 STORYBOARD_CONFIRMED（可重试）。"""
        project.status = (
            ProjectStatus.VIDEO_READY
            if manifest and manifest.ready_clips
            else ProjectStatus.STORYBOARD_CONFIRMED
        )
        self.store.save_project(project)

    def get_video_manifest(self, project_id: str) -> VideoManifest | None:
        return self.store.load_video_manifest(project_id)

    def video_status(self, project_id: str) -> dict:
        project = self.get_project(project_id)
        manifest = self.store.load_video_manifest(project_id)
        active = self.jobs.active_for(project_id, VIDEO_JOB_KIND)
        latest = self.jobs.list(project_id, limit=1)
        job = active or (latest[0] if latest else None)
        return {
            "project_status": project.status.value,
            "summary": manifest.summary() if manifest else None,
            "manifest": manifest.model_dump() if manifest else None,
            "job": job.to_public() if job else None,
        }

    async def cancel_video_generation(self, project_id: str) -> int:
        self.get_project(project_id)
        cancelled = await self.jobs.cancel_project(project_id, VIDEO_JOB_KIND)
        if cancelled:
            async with self._lock(project_id):
                project = self.get_project(project_id)
                self._settle_video_status(project, self.store.load_video_manifest(project_id))
        return cancelled

    async def recover_interrupted_jobs(self) -> list[str]:
        """进程重启后，把卡在「进行中」状态的项目收回到可继续的状态。

        内存里的任务在重启后就没了，但 meta.json 里可能还写着 VIDEO_GENERATING。
        """
        recovered: list[str] = []
        for project in self.store.list_projects():
            if project.status not in TRANSIENT_STATUSES:
                continue
            manifest = self.store.load_video_manifest(project.id)
            if project.status == ProjectStatus.VIDEO_GENERATING:
                self._settle_video_status(project, manifest)
            else:
                # 配音/合成阶段未实现，回退到视频就绪
                project.status = (
                    ProjectStatus.VIDEO_READY
                    if manifest and manifest.ready_clips
                    else ProjectStatus.STORYBOARD_CONFIRMED
                )
                self.store.save_project(project)
            recovered.append(project.id)
            logger.warning(
                "重启恢复：项目 %s 原处于中断状态，已回退为 %s", project.id, project.status.value
            )
        return recovered

    # ── 读取 / 校验 / 删除 ────────────────────────────────

    def get_script(self, project_id: str) -> Script | None:
        return self.store.load_script(project_id)

    def get_bible(self, project_id: str) -> StoryBible | None:
        return self.store.load_bible(project_id)

    def get_storyboard(self, project_id: str) -> Storyboard | None:
        return self.store.load_storyboard(project_id)

    def get_world_state(self, project_id: str) -> WorldState | None:
        return self.store.load_world_state(project_id)

    def get_outline(self, project_id: str) -> ScriptOutline | None:
        return self.store.load_outline(project_id)

    def get_report(self, project_id: str) -> ConsistencyReport | None:
        return self.store.load_consistency(project_id)

    async def revalidate(self, project_id: str) -> ConsistencyReport | None:
        """按当前磁盘产物重算一致性报告。"""
        async with self._lock(project_id):
            project = self.get_project(project_id)
            bible = self.store.load_bible(project_id)
            if not bible:
                return None
            return self._revalidate(project, bible, self.store.load_script(project_id))

    def _revalidate(
        self, project: Project, bible: StoryBible | None, script: Script | None
    ) -> ConsistencyReport | None:
        """不加锁的内部实现（调用方已持锁）。"""
        if not bible:
            return None
        if not script or not script.scenes:
            report = ConsistencyReport(stage="script")
        else:
            report = consistency_service.validate_script(
                script, bible, self.store.load_world_state(project.id), project.target_duration_sec
            )
            storyboard = self.store.load_storyboard(project.id)
            if storyboard and storyboard.shots:
                report = consistency_service.merge_storyboard_issues(
                    report, consistency_service.validate_storyboard(storyboard, script, bible)
                )
        self.store.save_consistency(project.id, report)
        return report

    async def delete_project(self, project_id: str) -> bool:
        async with self._lock(project_id):
            self._locks.pop(project_id, None)
            return self.store.delete_project(project_id)
