from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from videogen.config import get_settings
from videogen.models.audio import AudioManifest
from videogen.models.bible import StoryBible
from videogen.models.compose import ComposeReport
from videogen.models.consistency import ConsistencyReport
from videogen.models.project import Project, ProjectStatus
from videogen.models.script import Script, ScriptOutline
from videogen.models.storyboard import Storyboard
from videogen.models.video import VideoManifest
from videogen.models.world_state import WorldState
from videogen.orchestrator.jobs import Job, JobContext, JobManager
from videogen.providers import bgm as bgm_providers
from videogen.providers import llm as llm_providers
from videogen.providers import tts as tts_providers
from videogen.providers import video as video_providers
from videogen.providers.bgm.base import BgmLibrary
from videogen.providers.llm.base import LLMProvider
from videogen.providers.tts.base import TTSProvider
from videogen.providers.video.base import VideoProvider
from videogen.services import (
    audio_service,
    bible_service,
    compose_service,
    consistency_service,
    script_service,
    storyboard_service,
    video_service,
)
from videogen.utils.ffmpeg import ffmpeg_available
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
    # 已经出片的项目也要能退回改剧本（改文本时才作废媒体）
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}
GENERATE_STORYBOARD_STATUSES = {
    ProjectStatus.SCRIPT_CONFIRMED,
    ProjectStatus.STORYBOARD_REVIEW,
    ProjectStatus.FAILED,
}
CONFIRM_STORYBOARD_STATUSES = {ProjectStatus.STORYBOARD_REVIEW}
REOPEN_STORYBOARD_STATUSES = {
    ProjectStatus.STORYBOARD_CONFIRMED,
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}

EDIT_BIBLE_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED, ProjectStatus.SCRIPT_REVIEW}
EDIT_SCRIPT_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED, ProjectStatus.SCRIPT_REVIEW}
EDIT_STORYBOARD_STATUSES = {ProjectStatus.SCRIPT_CONFIRMED, ProjectStatus.STORYBOARD_REVIEW}

#: 视频生成入口：确认分镜后开跑，或在 VIDEO_READY 下补跑失败/未生成的镜头。
#: 允许 VIDEO_GENERATING 是为了让「任务已死但状态没收拾干净」的项目也能手动重试。
GENERATE_VIDEO_STATUSES = {
    ProjectStatus.STORYBOARD_CONFIRMED,
    ProjectStatus.VIDEO_READY,
    ProjectStatus.VIDEO_GENERATING,
    # 配音/合成之后也允许补跑镜头（配音与文本无关，不会被作废）
    ProjectStatus.AUDIO_READY,
    ProjectStatus.COMPOSING,
    ProjectStatus.DONE,
}

#: 进程重启后需要收尾的「进行中」状态
TRANSIENT_STATUSES = {
    ProjectStatus.VIDEO_GENERATING,
    ProjectStatus.AUDIO_GENERATING,
    ProjectStatus.COMPOSING,
}

#: 配音入口：视频就绪后开跑；AUDIO_READY 下允许补跑失败条目
GENERATE_AUDIO_STATUSES = {
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.AUDIO_GENERATING,
}

#: 合成入口：配音就绪后合成；DONE 下允许重新合成（换 BGM / 换字幕模式）
COMPOSE_STATUSES = {
    ProjectStatus.AUDIO_READY,
    ProjectStatus.COMPOSING,
    ProjectStatus.DONE,
}

VIDEO_JOB_KIND = "video"
AUDIO_JOB_KIND = "audio"
COMPOSE_JOB_KIND = "compose"


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
        tts_factory: Callable[[], TTSProvider] | None = None,
        bgm_factory: Callable[[], BgmLibrary] | None = None,
        jobs: JobManager | None = None,
    ):
        self.store = store or FileStore()
        self._llm_factory = llm_factory or (lambda: llm_providers.get_llm())
        self._video_factory = video_factory or (lambda: video_providers.get_video_provider())
        self._tts_factory = tts_factory or (lambda: tts_providers.get_tts_provider())
        self._bgm_factory = bgm_factory or (lambda: bgm_providers.get_bgm_library())
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

    def _tts(self) -> TTSProvider:
        return self._tts_factory()

    def _bgm(self) -> BgmLibrary:
        return self._bgm_factory()

    def _invalidate_media(self, project_id: str) -> None:
        """文本产物变了，下游媒体全部作废（避免用旧分镜去合成）。"""
        self.store.purge_media(project_id, video=True, audio=True, compose=True)

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
            # 圣经变了，旧的一致性报告和下游媒体全部作废
            self.store.remove_artifact(project_id, "consistency_report")
            self._invalidate_media(project_id)
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
            self._invalidate_media(project_id)
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
            # 重新生成剧本会让旧分镜和所有下游媒体失效
            self.store.remove_artifact(project_id, "storyboard")
            self._invalidate_media(project_id)
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
            self._invalidate_media(project_id)
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

            self._invalidate_media(project_id)
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
            self._invalidate_media(project_id)
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
        self._reject_if_running(project_id, VIDEO_JOB_KIND, "视频生成")

    def _reject_if_running(self, project_id: str, kind: str, label: str) -> None:
        """同一项目同类型任务只允许一个在跑（在拿项目锁之前调用，避免阻塞）。"""
        running = self.jobs.active_for(project_id, kind)
        if running:
            raise PipelineError(f"该项目已有{label}任务在运行（job={running.id}）")

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
                self.store.purge_media(project_id, compose=True)
                self._settle_video_status(
                    project,
                    self.store.load_video_manifest(project_id),
                    self.store.load_audio_manifest(project_id),
                )
                raise
            finally:
                await provider.aclose()

            self.store.save_video_manifest(project_id, manifest)
            # 片段变了，旧的成片一定过期
            self.store.purge_media(project_id, compose=True)
            self._settle_video_status(
                project, manifest, self.store.load_audio_manifest(project_id)
            )
            ctx.set_progress(
                done=len(manifest.clips),
                total=len(manifest.clips),
                message=(
                    f"完成：成功 {len(manifest.ready_clips)}，失败 {len(manifest.failed_clips)}"
                ),
            )
            if not manifest.ready_clips:
                raise PipelineError(
                    f"全部 {len(manifest.clips)} 个片段生成失败，请检查 provider 配置 / ffmpeg"
                )

    def _settle_video_status(
        self,
        project: Project,
        manifest: VideoManifest | None,
        audio: AudioManifest | None = None,
    ) -> None:
        """有可用片段就进 VIDEO_READY（配音仍在则保持 AUDIO_READY），否则退回分镜确认态。"""
        if not manifest or not manifest.ready_clips:
            project.status = ProjectStatus.STORYBOARD_CONFIRMED
        elif audio is not None and audio.is_usable and audio.ready_clips:
            project.status = ProjectStatus.AUDIO_READY
        else:
            project.status = ProjectStatus.VIDEO_READY
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
                self._settle_video_status(
                    project,
                    self.store.load_video_manifest(project_id),
                    self.store.load_audio_manifest(project_id),
                )
        return cancelled

    # ── 配音阶段 ──────────────────────────────────────────

    async def start_audio_generation(
        self, project_id: str, voice_overrides: dict[str, str] | None = None
    ) -> Job:
        """提交配音后台任务（进度用 audio_status 查）。

        `voice_overrides` 可以按角色换音色；换了音色的条目会重新合成（其余续跑）。
        """
        self._reject_if_running(project_id, AUDIO_JOB_KIND, "配音")

        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._reject_if_running(project_id, AUDIO_JOB_KIND, "配音")
            self._require(project, GENERATE_AUDIO_STATUSES, "生成配音（需先有视频片段）")

            storyboard = self.store.load_storyboard(project_id)
            if not storyboard or not storyboard.shots:
                raise PipelineError("分镜不存在或为空，无法生成配音")
            video = self.store.load_video_manifest(project_id)
            if not video or not video.ready_clips:
                raise PipelineError("没有可用的视频片段，请先生成视频")

            project.status = ProjectStatus.AUDIO_GENERATING
            self.store.save_project(project)

        return self.jobs.submit(
            project_id,
            AUDIO_JOB_KIND,
            lambda ctx: self._run_audio_job(ctx, project_id, voice_overrides),
        )

    async def _run_audio_job(
        self, ctx: JobContext, project_id: str, voice_overrides: dict[str, str] | None = None
    ) -> None:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            storyboard = self.store.load_storyboard(project_id) or Storyboard()
            bible = self.store.load_bible(project_id)
            provider = self._tts()
            project_dir = self.store.ensure_project_dir(project_id)
            audio_dir = self.store.audio_dir(project_id)
            settings = get_settings()

            def on_progress(manifest: AudioManifest) -> None:
                self.store.save_audio_manifest(project_id, manifest)
                done = len(manifest.ready_clips) + len(manifest.failed_clips)
                ctx.set_progress(
                    done=done,
                    total=len(manifest.clips),
                    message=(
                        f"配音 {done}/{len(manifest.clips)}（成功 {len(manifest.ready_clips)}，"
                        f"失败 {len(manifest.failed_clips)}）"
                    ),
                )

            try:
                manifest = await audio_service.synthesize_all(
                    project,
                    storyboard,
                    bible,
                    provider,
                    audio_dir,
                    project_dir,
                    settings=settings,
                    concurrency=settings.tts_concurrency,
                    max_retries=settings.tts_max_retries,
                    retry_backoff_sec=settings.tts_retry_backoff_sec,
                    existing=self.store.load_audio_manifest(project_id),
                    voice_overrides=voice_overrides,
                    on_progress=on_progress,
                )
            except asyncio.CancelledError:
                self._settle_audio_status(
                    project,
                    self.store.load_audio_manifest(project_id),
                    self.store.load_video_manifest(project_id),
                )
                raise
            finally:
                await provider.aclose()

            if manifest.ready_clips:
                manifest.bgm = audio_service.pick_bgm(
                    storyboard, self._bgm(), preferred_id=settings.bgm_id
                )
            audio_service.align_storyboard(
                storyboard,
                manifest,
                gap_sec=settings.audio_gap_sec,
                max_ratio=settings.audio_align_max_ratio,
            )
            self.store.save_storyboard(project_id, storyboard)
            self.store.save_audio_manifest(project_id, manifest)
            # 镜头时长可能被拉长 -> 旧成片作废
            self.store.purge_media(project_id, compose=True)
            self._settle_audio_status(project, manifest, self.store.load_video_manifest(project_id))

            failed, ready = len(manifest.failed_clips), len(manifest.ready_clips)
            ctx.set_progress(
                done=len(manifest.clips),
                total=len(manifest.clips),
                message=f"完成：成功 {ready}，失败 {failed}",
            )
            if failed and not ready:
                raise PipelineError(f"全部 {failed} 条配音生成失败，请检查 TTS 配置 / 网络")

    def _settle_audio_status(
        self,
        project: Project,
        audio: AudioManifest | None,
        video: VideoManifest | None,
    ) -> None:
        """配音齐全（含「本来就没台词」）就进 AUDIO_READY，否则退回 VIDEO_READY。"""
        if audio is not None and audio.is_usable and video and video.ready_clips:
            project.status = ProjectStatus.AUDIO_READY
        else:
            project.status = (
                ProjectStatus.VIDEO_READY
                if video and video.ready_clips
                else ProjectStatus.STORYBOARD_CONFIRMED
            )
        self.store.save_project(project)

    def get_audio_manifest(self, project_id: str) -> AudioManifest | None:
        return self.store.load_audio_manifest(project_id)

    def audio_status(self, project_id: str) -> dict:
        project = self.get_project(project_id)
        manifest = self.store.load_audio_manifest(project_id)
        active = self.jobs.active_for(project_id, AUDIO_JOB_KIND)
        latest = self.jobs.list(project_id, limit=1)
        job = active or (latest[0] if latest else None)
        return {
            "project_status": project.status.value,
            "summary": manifest.summary() if manifest else None,
            "manifest": manifest.model_dump() if manifest else None,
            "voice_map": manifest.voice_map if manifest else {},
            "job": job.to_public() if job else None,
        }

    async def cancel_audio_generation(self, project_id: str) -> int:
        self.get_project(project_id)
        cancelled = await self.jobs.cancel_project(project_id, AUDIO_JOB_KIND)
        if cancelled:
            async with self._lock(project_id):
                project = self.get_project(project_id)
                self._settle_audio_status(
                    project,
                    self.store.load_audio_manifest(project_id),
                    self.store.load_video_manifest(project_id),
                )
        return cancelled

    # ── 合成阶段 ──────────────────────────────────────────

    async def start_compose(self, project_id: str, overrides: dict | None = None) -> Job:
        """提交成片合成后台任务（进度用 compose_status 查）。

        `overrides` 可临时覆盖设置（字幕模式 / BGM / 画质），不改全局配置。
        """
        self._reject_if_running(project_id, COMPOSE_JOB_KIND, "合成")

        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._reject_if_running(project_id, COMPOSE_JOB_KIND, "合成")
            self._require(project, COMPOSE_STATUSES, "合成成片（需先生成配音）")

            storyboard = self.store.load_storyboard(project_id)
            if not storyboard or not storyboard.shots:
                raise PipelineError("分镜不存在或为空，无法合成")
            video = self.store.load_video_manifest(project_id)
            if not video or not video.is_usable:
                raise PipelineError("视频片段不完整（含失败或占位文件），请先补齐再合成")

            project.status = ProjectStatus.COMPOSING
            self.store.save_project(project)

        return self.jobs.submit(
            project_id,
            COMPOSE_JOB_KIND,
            lambda ctx: self._run_compose_job(ctx, project_id, overrides),
        )

    async def _run_compose_job(
        self, ctx: JobContext, project_id: str, overrides: dict | None = None
    ) -> None:
        if not ffmpeg_available():
            raise PipelineError(
                "未找到 ffmpeg，无法合成（brew install ffmpeg 或 pip install imageio-ffmpeg）"
            )

        async with self._lock(project_id):
            project = self.get_project(project_id)
            storyboard = self.store.load_storyboard(project_id) or Storyboard()
            bible = self.store.load_bible(project_id)
            video = self.store.load_video_manifest(project_id)
            audio = self.store.load_audio_manifest(project_id)
            settings = get_settings()
            if overrides:
                allowed = {
                    "compose_subtitle_mode",
                    "compose_crf",
                    "compose_preset",
                    "compose_subtitle_font",
                    "compose_subtitle_font_scale",
                    "bgm_id",
                    "bgm_gain_db",
                    "bgm_enabled",
                }
                clean = {k: v for k, v in overrides.items() if k in allowed and v is not None}
                if clean:
                    settings = settings.model_copy(update=clean)
                    logger.info("合成参数覆盖：%s", ", ".join(sorted(clean)))

            def on_progress(done: int, total: int, message: str) -> None:
                ctx.set_progress(done=done, total=total, message=message)

            try:
                report = await compose_service.compose(
                    project,
                    storyboard,
                    video,
                    audio,
                    segments_dir=self.store.segments_dir(project_id),
                    output_dir=self.store.output_dir(project_id),
                    project_dir=self.store.ensure_project_dir(project_id),
                    bible=bible,
                    settings=settings,
                    on_progress=on_progress,
                )
            except asyncio.CancelledError:
                self.store.purge_media(project_id, compose=True)
                self._settle_compose_status(project, audio, video)
                raise
            except Exception:
                # 失败的半截成片一律删掉，避免被当成可用产物
                self.store.purge_media(project_id, compose=True)
                self._settle_compose_status(project, audio, video)
                raise

            self.store.save_compose_report(project_id, report)
            project.status = ProjectStatus.DONE
            self.store.save_project(project)
            ctx.set_progress(
                done=len(report.segments),
                total=len(report.segments),
                message=f"成片完成：{report.duration_sec:.1f}s",
            )

    def _settle_compose_status(
        self, project: Project, audio: AudioManifest | None, video: VideoManifest | None
    ) -> None:
        if audio is not None and audio.is_usable and video and video.ready_clips:
            project.status = ProjectStatus.AUDIO_READY
        elif video and video.ready_clips:
            project.status = ProjectStatus.VIDEO_READY
        else:
            project.status = ProjectStatus.STORYBOARD_CONFIRMED
        self.store.save_project(project)

    def get_compose_report(self, project_id: str) -> ComposeReport | None:
        return self.store.load_compose_report(project_id)

    def compose_status(self, project_id: str) -> dict:
        project = self.get_project(project_id)
        report = self.store.load_compose_report(project_id)
        active = self.jobs.active_for(project_id, COMPOSE_JOB_KIND)
        latest = self.jobs.list(project_id, limit=1)
        job = active or (latest[0] if latest else None)
        return {
            "project_status": project.status.value,
            "summary": report.summary() if report else None,
            "report": report.model_dump() if report else None,
            "job": job.to_public() if job else None,
            "ffmpeg_available": ffmpeg_available(),
        }

    async def cancel_compose(self, project_id: str) -> int:
        self.get_project(project_id)
        cancelled = await self.jobs.cancel_project(project_id, COMPOSE_JOB_KIND)
        if cancelled:
            async with self._lock(project_id):
                project = self.get_project(project_id)
                self._settle_compose_status(
                    project,
                    self.store.load_audio_manifest(project_id),
                    self.store.load_video_manifest(project_id),
                )
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
            audio = self.store.load_audio_manifest(project.id)
            if project.status == ProjectStatus.VIDEO_GENERATING:
                self._settle_video_status(project, manifest, audio)
            elif project.status == ProjectStatus.AUDIO_GENERATING:
                self._settle_audio_status(project, audio, manifest)
            else:  # COMPOSING：半截成片已经在合成任务里清掉了
                self.store.purge_media(project.id, compose=True)
                project.status = (
                    ProjectStatus.AUDIO_READY
                    if audio and audio.is_usable and manifest and manifest.ready_clips
                    else ProjectStatus.VIDEO_READY
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
