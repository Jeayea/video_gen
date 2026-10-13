from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from videogen.config import Settings, get_settings
from videogen.models.audio import AudioManifest
from videogen.models.bible import StoryBible
from videogen.models.compose import ComposeReport
from videogen.models.consistency import ConsistencyReport
from videogen.models.cost import CostReport, CostStage
from videogen.models.novel import NovelSource
from videogen.models.project import InputMode, Project, ProjectStatus
from videogen.models.script import (
    DEFAULT_CHAPTER_DURATION_SEC,
    OutlineScene,
    Script,
    ScriptOutline,
    StoryOutline,
)
from videogen.models.storyboard import DurationTier, Shot, Storyboard
from videogen.models.video import ClipStatus, VideoManifest
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
    audio_script_service,
    audio_service,
    bible_service,
    compose_service,
    consistency_service,
    cost_service,
    dub_guide_service,
    keyframe_service,
    music_service,
    novel_service,
    preflight_service,
    script_service,
    storyboard_service,
    video_service,
)
from videogen.utils.ffmpeg import ffmpeg_available
from videogen.utils.file_store import FileStore
from videogen.utils.redact import scrub_secrets
from videogen.utils.text import count_speech_chars

logger = logging.getLogger(__name__)


def _chapter_context(story: StoryOutline | None, entry: OutlineScene | None) -> str:
    """这一场在整篇里的位置（章标题/情节点/钩子），防止分镜脱离全篇弧光。"""
    if story is None or entry is None or not entry.chapter_id:
        return ""
    chapter = story.get_chapter(entry.chapter_id)
    if chapter is None:
        return ""
    lines = [f"所属章: [{chapter.id}] {chapter.title}"]
    if story.logline:
        lines.append(f"全篇卖点: {story.logline}")
    if chapter.summary:
        lines.append(f"章正文: {chapter.summary}")
    if chapter.beats:
        lines.append("章内情节点: " + " / ".join(chapter.beats))
    if chapter.hook:
        lines.append(f"章末钩子: {chapter.hook}")
    return "\n".join(lines)


def _pending_shots(storyboard: Storyboard, manifest: VideoManifest | None) -> list[Shot]:
    """还需要生成的镜头（续跑时跳过已经就绪的）。"""
    if not manifest:
        return list(storyboard.shots)
    ready = {c.shot_id for c in manifest.ready_clips}
    return [s for s in storyboard.shots if s.id not in ready]


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
#: 长篇大纲入口：与剧本同权限（退回剧本阶段后可重出/续写大纲）
GENERATE_STORY_STATUSES = set(GENERATE_SCRIPT_STATUSES)
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
#: 退回配音阶段：换音色 / 换念法 / 改台词后重做配音（成片会一起作废）
REOPEN_AUDIO_STATUSES = {
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}

REOPEN_STORYBOARD_STATUSES = {
    ProjectStatus.STORYBOARD_CONFIRMED,
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}

EDIT_BIBLE_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED, ProjectStatus.SCRIPT_REVIEW}
EDIT_SCRIPT_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED, ProjectStatus.SCRIPT_REVIEW}
EDIT_STORYBOARD_STATUSES = {ProjectStatus.SCRIPT_CONFIRMED, ProjectStatus.STORYBOARD_REVIEW}
#: 确认之后（甚至成片之后）也能改分镜：只作废受影响的段，其余已生成的片段保留
EDIT_CONFIRMED_STORYBOARD_STATUSES = {
    ProjectStatus.STORYBOARD_CONFIRMED,
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}

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

    def _llm(self, model: str = "") -> LLMProvider:
        """拿一个 LLM provider；`model` 非空时切换到指定模型（多模型对比用）。"""
        provider = self._llm_factory()
        if model:
            return provider.with_model(model)
        return provider

    # ── 成本记账 / 预算 ───────────────────────────────────

    def cost_report(self, project_id: str) -> CostReport:
        return cost_service.load_report(self.store, project_id)

    def _guard_budget(self, project_id: str, *, upcoming_usd: float = 0.0, action: str) -> None:
        report = cost_service.load_report(self.store, project_id)
        cost_service.check_budget(report, upcoming_usd=upcoming_usd, action=action)

    async def _budget_shots(
        self, storyboard: Storyboard, video: VideoManifest | None
    ) -> list[Shot]:
        """预算口径：这次真正会去生成哪些镜头。

        与 `video_service.generate_clips` 的续跑逻辑保持一致 —— 换了 provider / 模型时
        已就绪的片段会被判为过期并全部重生成，所以预算也得按「全部」算。
        """
        provider = self._video()
        try:
            switched = bool(video) and video_service.provider_switched(video, provider)
        except Exception:  # 判断不了就按「最坏情况全部重跑」保守估
            logger.warning("provider 变更判断失败，按全部镜头预估预算", exc_info=True)
            switched = bool(video and video.clips)
        finally:
            await provider.aclose()
        return list(storyboard.shots) if switched else _pending_shots(storyboard, video)

    async def _estimate_video_usd(
        self, shots: list[Shot], storyboard: Storyboard | None = None
    ) -> float:
        """按 provider 的计费档位与单价估算花费（问不到就退回配置单价）。

        给了 `storyboard` 就按**段落**算：一段只买一次档位（与真正生成同一口径）。
        """
        if not shots:
            return 0.0
        wanted = {shot.id for shot in shots}
        units = [float(shot.duration_sec) for shot in shots]
        if storyboard is not None:
            units = [
                float(segment.duration_sec)
                for segment in storyboard_service.segments_of(storyboard)
                if any(sid in wanted for sid in segment.shot_ids)
            ]
        prices = cost_service.PriceTable.from_settings()
        provider = self._video()
        try:
            price = await provider.price_per_second()
            seconds = 0.0
            for unit in units:
                seconds += await provider.planned_seconds(unit)
        except Exception:
            logger.warning("视频成本预估失败，按配置单价估算", exc_info=True)
            return cost_service.estimate_video_usd(
                Storyboard(shots=list(shots)), prices
            )
        finally:
            await provider.aclose()
        unit = prices.video_per_second if price is None else max(0.0, price)
        return round(seconds * unit, 6)

    async def _estimate_keyframe_usd(
        self, project: Project, shots: list[Shot], storyboard: Storyboard
    ) -> float:
        """只认首帧的模型：这次要新画的开场关键帧（与 preflight 同一口径）。"""
        if not shots:
            return 0.0
        wanted = {shot.id for shot in shots}
        segments = [
            segment
            for segment in storyboard_service.segments_of(storyboard)
            if any(sid in wanted for sid in segment.shot_ids)
        ]
        provider = self._video()
        try:
            plan = await self._keyframe_estimator(project, provider)(segments)
        except Exception:  # 估不出就按 0，真正画之前还会逐张查预算
            logger.warning("关键帧预估失败", exc_info=True)
            return 0.0
        finally:
            await provider.aclose()
        return float(plan.get("estimated_usd") or 0.0)

    async def _apply_segment_mode(
        self,
        storyboard: Storyboard,
        *,
        tier: DurationTier | None = None,
        query_tier: bool = True,
        video: VideoManifest | None = None,
        only_if_missing: bool = False,
    ) -> Storyboard:
        """按配置给分镜编段（段落分镜）；关掉段落模式时返回一份去掉段落标记的副本。

        `only_if_missing=True`（视频阶段/preflight 用）：已经编过段就不动；老项目已经有
        单镜生成好的片段时也不编段 —— 否则已就绪的镜头会被卷进新段落、整段重买。
        """
        settings = get_settings()
        if not settings.video_segment_mode:
            if any(shot.segment_id for shot in storyboard.shots):
                storyboard = storyboard.model_copy(deep=True)
                for shot in storyboard.shots:
                    shot.segment_id = ""
            return storyboard
        if only_if_missing and (
            all(shot.segment_id for shot in storyboard.shots)
            or (video is not None and video.ready_clips)
        ):
            return storyboard
        if query_tier and tier is None:
            tier = await self._duration_tier()
        max_sec = float(settings.video_segment_max_sec)
        if tier is not None and tier.max_sec:
            max_sec = min(max_sec, float(tier.max_sec))
        segments = storyboard_service.assign_segments(
            storyboard,
            target_sec=settings.video_segment_target_sec,
            max_sec=max_sec,
            min_sec=tier.min_sec if tier is not None else 0.0,
            max_shots=settings.video_segment_max_shots,
        )
        logger.info(
            "段落分镜：%d 个镜头 -> %d 段（目标 %gs / 上限 %gs）",
            len(storyboard.shots),
            len(segments),
            settings.video_segment_target_sec,
            max_sec,
        )
        return storyboard

    def _record_llm_cost(self, project_id: str, stage: CostStage, llm: LLMProvider) -> None:
        """把这次阶段调用消耗的 token 记进账本（provider 不支持记账时跳过）。"""
        take = getattr(llm, "take_usage", None)
        if not callable(take):
            return
        usage = take()
        if not usage.calls and not usage.total_tokens:
            return
        cost_service.record(
            self.store,
            project_id,
            cost_service.llm_entry(
                stage,
                usage,
                provider=getattr(llm, "name", type(llm).__name__),
                model=getattr(llm, "model", ""),
            ),
        )

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
        """更新标题 / 比例 / 目标时长 / 国家限定（不影响已生成产物）。"""
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
            # 圣经已经存在时，把「国家限定」同步进去 —— 下游（定妆照/视频）读的是
            # 圣经的 cast_region，只改项目字段的话不会生效（用户会以为改了没用）
            if "cast_region" in fields:
                await self._sync_bible_region(project_id, updated.cast_region)
            return updated

    async def _sync_bible_region(self, project_id: str, cast_region: str) -> None:
        bible = self.store.load_bible(project_id)
        if bible is None or bible.cast_region == cast_region:
            return
        bible.cast_region = cast_region
        self.store.save_bible(project_id, bible)

    # ── 故事圣经 ──────────────────────────────────────────

    async def generate_bible(
        self, project_id: str, model: str = "", *, novel_chapters: int | None = None
    ) -> StoryBible:
        """生成故事圣经。

        小说模式：切章 → 抽取前 `novel_chapters` 章（默认 `NOVEL_INITIAL_CHAPTERS`）→
        合并成圣经，并顺手把章节拼成长篇大纲（章节即大纲，不调 LLM）。
        """
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, GENERATE_BIBLE_STATUSES, "生成故事圣经")
            self._guard_budget(project_id, action="生成故事圣经")
            llm = self._llm(model)

            try:
                if project.input_mode == InputMode.NOVEL:
                    bible = await self._novel_bible(project, llm, novel_chapters)
                else:
                    bible = await bible_service.generate_bible(project, llm)
            except Exception as exc:
                self._fail(project, "故事圣经生成")
                raise PipelineError(f"故事圣经生成失败：{exc}") from exc
            finally:
                self._record_llm_cost(project_id, CostStage.BIBLE, llm)
                # 与 video provider 同一套生命周期：阶段结束就放掉连接，
                # 免得「复用连接」变成「泄漏连接」
                await llm.aclose()

            self.store.save_bible(project_id, bible)
            # 圣经变了，旧的一致性报告和下游媒体全部作废
            self.store.remove_artifact(project_id, "consistency_report")
            self._invalidate_media(project_id)
            return bible

    # ── 小说改编（切章 / 增量抽取 / 合并）────────────────────

    def _load_novel(self, project_id: str) -> NovelSource:
        novel = self.store.load_novel(project_id)
        if novel is None or not novel.chapters:
            raise PipelineError("小说还没有切章：请先生成故事圣经（小说模式会先切章并抽取前几章）")
        return novel

    async def _novel_bible(
        self, project: Project, llm: LLMProvider, chapters: int | None
    ) -> StoryBible:
        """切章 + 抽取前 N 章 + 合并成圣经 + 章节大纲。原文没变时沿用已抽取的章与已有圣经。"""
        settings = get_settings()
        novel, resplit = novel_service.prepare_novel(
            project,
            self.store.load_novel(project.id),
            chunk_chars=settings.novel_chunk_chars,
            max_chars=settings.novel_chapter_max_chars,
        )
        if resplit:
            # 原文变了：旧圣经/旧大纲对应的是另一份文本，整份重来
            self.store.remove_artifact(project.id, "story_bible")
            self.store.remove_artifact(project.id, "story_outline")
        self.store.save_novel(project.id, novel)
        count = max(1, chapters or settings.novel_initial_chapters)
        ids = [c.id for c in novel.chapters[:count]]
        bible, _ = await self._extract_and_merge(project, llm, novel, ids)
        return bible

    async def _extract_and_merge(
        self, project: Project, llm: LLMProvider, novel: NovelSource, chapter_ids: list[str]
    ) -> tuple[StoryBible, StoryOutline]:
        """抽取指定章（已抽过的跳过）→ 把没合并过的章并进圣经 → 重建章节大纲。

        合并是增量的：已有角色/场景/道具的 id、参考图、音色都不动，只补别名和新条目。
        """
        settings = get_settings()
        unknown = [cid for cid in chapter_ids if novel.get_chapter(cid) is None]
        if unknown:
            raise PipelineError(f"未知的章: {', '.join(unknown)}")
        bible = self.store.load_bible(project.id)
        await novel_service.extract_chapters(
            llm,
            novel,
            chapter_ids,
            bible=bible,
            concurrency=settings.novel_extract_concurrency,
            max_props=settings.novel_max_props_per_chapter,
            # 每抽完一章就落盘：中途失败也不会丢掉已经花钱抽好的章
            on_progress=lambda partial: self.store.save_novel(project.id, partial),
        )
        # 圣经不在了（原文改过 / 被删）就把所有抽过的章重新并一遍
        to_merge = [
            c.id for c in novel.chapters if c.extracted and (bible is None or not c.merged)
        ]
        if to_merge or bible is None:
            bible = await novel_service.merge_into_bible(
                llm,
                project,
                bible,
                novel,
                to_merge,
                max_characters=settings.novel_max_characters,
            )
            for chapter in novel.chapters:
                if chapter.id in to_merge:
                    chapter.merged = True
            self.store.save_bible(project.id, bible)
            self.store.save_novel(project.id, novel)
        assert bible is not None
        story = novel_service.build_story_outline(
            project, novel, bible, self.store.load_story_outline(project.id)
        )
        self.store.save_story_outline(project.id, story)
        return bible, story

    async def extract_novel_chapters(
        self, project_id: str, chapter_ids: list[str], *, model: str = ""
    ) -> StoryOutline:
        """只抽取并合并资产（不写剧本）：选章前先看看这几章会带进来哪些角色/场景/道具。"""
        async with self._lock(project_id):
            project = self.get_project(project_id)
            if project.input_mode != InputMode.NOVEL:
                raise PipelineError("只有小说模式的项目才能按章抽取")
            self._require(project, EDIT_BIBLE_STATUSES, "抽取章节资产（需先退回剧本阶段）")
            novel = self._load_novel(project_id)
            self._guard_budget(project_id, action="抽取章节资产")
            llm = self._llm(model)
            try:
                _, story = await self._extract_and_merge(project, llm, novel, chapter_ids)
            except PipelineError:
                raise
            except Exception as exc:
                raise PipelineError(f"章节抽取失败：{exc}") from exc
            finally:
                self._record_llm_cost(project_id, CostStage.BIBLE, llm)
                await llm.aclose()
            return story

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

    # ── 长篇剧集大纲 ──────────────────────────────────────

    async def generate_story_outline(
        self,
        project_id: str,
        *,
        chapters: int | None = None,
        force: bool = False,
        model: str = "",
    ) -> StoryOutline:
        """生成长篇剧集大纲（20+ 章骨架 + 逐章正文）。

        `force=True` 重出骨架（会丢弃对不上的旧正文）；否则在同一份大纲上续写缺失的章。
        """
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, GENERATE_STORY_STATUSES, "生成长篇大纲（需先退回剧本阶段）")
            bible = self.store.load_bible(project_id)
            if not bible:
                raise PipelineError("故事圣经不存在，请先生成圣经")
            if project.input_mode == InputMode.NOVEL:
                # 小说的章就是大纲：按切章 + 已抽取结果重建，不调 LLM、不原创
                outline = novel_service.build_story_outline(
                    project,
                    self._load_novel(project_id),
                    bible,
                    None if force else self.store.load_story_outline(project_id),
                )
                self.store.save_story_outline(project_id, outline)
                return outline

            settings = get_settings()
            self._guard_budget(project_id, action="生成长篇大纲")
            llm = self._llm(model)
            existing = None if force else self.store.load_story_outline(project_id)
            try:
                outline = await script_service.generate_story_outline(
                    llm,
                    project,
                    bible,
                    chapters=chapters or settings.story_chapters,
                    chunk_size=settings.story_chapter_chunk,
                    existing=existing,
                    force_spine=force,
                    # 每批扩写完就落盘：中途失败也不会丢掉已花钱写好的章
                    on_progress=lambda partial: self.store.save_story_outline(
                        project_id, partial
                    ),
                )
            except Exception as exc:
                self._fail(project, "长篇大纲生成")
                raise PipelineError(f"长篇大纲生成失败：{exc}") from exc
            finally:
                self._record_llm_cost(project_id, CostStage.STORY, llm)
                await llm.aclose()

            outline.target_duration_sec = outline.total_duration_sec
            self.store.save_story_outline(project_id, outline)
            logger.info(
                "长篇大纲完成 %s：%d 章 / 约 %ds",
                project_id,
                len(outline.chapters),
                outline.total_duration_sec,
            )
            return outline

    def get_story_outline(self, project_id: str) -> StoryOutline | None:
        return self.store.load_story_outline(project_id)

    # ── 剧本 ──────────────────────────────────────────────

    async def generate_script(
        self,
        project_id: str,
        model: str = "",
        *,
        chapters: list[str] | None = None,
        force: bool = False,
    ) -> Script:
        """生成剧本。

        - 有长篇大纲时：只把**选中的章**展开成场次（`chapters=None` = 全部章），
          已经展开过的章不会重复生成；`force=True` 清空重来
        - 没有长篇大纲时：保持老行为（一次性分场大纲 + 全量重生成）
        """
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, GENERATE_SCRIPT_STATUSES, "生成剧本（需先退回剧本阶段）")

            bible = self.store.load_bible(project_id)
            if not bible:
                raise PipelineError("故事圣经不存在，请先生成圣经")

            self._guard_budget(project_id, action="生成剧本")
            novel_mode = project.input_mode == InputMode.NOVEL
            if novel_mode:
                bible = await self._prepare_novel_chapters(project, chapters, model)
            llm = self._llm(model)
            review_issues: list = []
            try:
                script, world_state = await self._run_script_generation(
                    project, bible, llm, chapters=chapters, force=force
                )
                # 结构评审：机器判不了的那部分（钩子/转折/高潮/因果）交给 LLM 当剧本医生。
                # 放在 try 内是为了让 finally 里的记账把两次调用的 token 一起记进 script 阶段；
                # review_structure 自己吞异常，评审失败不会连累剧本生成
                if get_settings().script_llm_review:
                    review_issues = await script_service.review_structure(
                        llm,
                        script,
                        bible,
                        partial=self._is_partial_arc(project_id),
                        adaptation=novel_mode,
                    )
            except Exception as exc:
                self._fail(project, "剧本生成")
                raise PipelineError(f"剧本生成失败：{exc}") from exc
            finally:
                self._record_llm_cost(project_id, CostStage.SCRIPT, llm)
                await llm.aclose()

            report = consistency_service.validate_script(
                script,
                bible,
                world_state,
                project.target_duration_sec,
                strict=get_settings().script_strict,
                partial=self._is_partial_arc(project_id),
            )
            if review_issues:
                report = report.model_copy(
                    update={"issues": [*report.issues, *review_issues]}
                )
                logger.info(
                    "剧本结构评审：%d 条（其中 error %d 条）",
                    len(review_issues),
                    sum(1 for i in review_issues if i.severity.value == "error"),
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

    async def _prepare_novel_chapters(
        self, project: Project, chapters: list[str] | None, model: str
    ) -> StoryBible:
        """小说模式写剧本前：选中的章没抽取过就先抽取并合并进圣经（增量，id 不变）。

        小说动辄几十章，「不选 = 全部章」会一口气烧掉整本书的视频额度，所以必须显式选章。
        """
        if not chapters:
            raise PipelineError("小说模式必须指定要拍的章（chapters），不会默认展开整本书")
        novel = self._load_novel(project.id)
        llm = self._llm(model)
        try:
            bible, _ = await self._extract_and_merge(project, llm, novel, chapters)
        except PipelineError:
            raise
        except Exception as exc:
            self._fail(project, "章节抽取")
            raise PipelineError(f"章节抽取失败：{exc}") from exc
        finally:
            self._record_llm_cost(project.id, CostStage.BIBLE, llm)
            await llm.aclose()
        return bible

    async def _run_script_generation(
        self,
        project: Project,
        bible: StoryBible,
        llm: LLMProvider,
        *,
        chapters: list[str] | None = None,
        force: bool = False,
    ) -> tuple[Script, WorldState]:
        """逐场生成剧本，每场生成后立即落盘，失败可续跑。

        两种入口共用后半段（逐场生成 + 时长归一化 + logline）：
        - **长篇大纲路径**：把选中的章展开成分场大纲，追加到已有场次后面
        - **老路径**：一次性生成分场大纲，全量重来
        """
        project_id = project.id
        resuming = project.status == ProjectStatus.FAILED
        story = self.store.load_story_outline(project_id)

        outline: ScriptOutline | None = None
        if project.input_mode == InputMode.NOVEL and not (story and story.chapters):
            raise PipelineError("小说模式缺少章节大纲：请先生成故事圣经（会切章并抽取）")
        if story and story.chapters:
            outline, script, world_state = await self._expand_selected_chapters(
                project, bible, llm, story, chapters=chapters, force=force
            )
        else:
            outline = self.store.load_outline(project_id) if resuming else None
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

    async def _expand_selected_chapters(
        self,
        project: Project,
        bible: StoryBible,
        llm: LLMProvider,
        story: StoryOutline,
        *,
        chapters: list[str] | None,
        force: bool,
    ) -> tuple[ScriptOutline, Script, WorldState]:
        """把选中的章展开成场次（追加语义），并把项目目标时长对齐到已展开的章。"""
        project_id = project.id
        wanted = [c for c in (chapters or [c.id for c in story.chapters]) if c]
        selected = [cid for cid in wanted if story.get_chapter(cid) is not None]
        if not selected:
            raise PipelineError("没有可展开的章：请在长篇大纲里选择要拍的章节")

        if force:
            outline = ScriptOutline()
            script = Script(title=story.title or bible.style.genre or project.title, logline="")
            world_state = WorldState()
            self.store.remove_artifact(project_id, "storyboard")
            self._invalidate_media(project_id)
        else:
            outline = self.store.load_outline(project_id) or ScriptOutline()
            script = self.store.load_script(project_id) or Script(
                title=story.title or bible.style.genre or project.title, logline=""
            )
            world_state = self.store.load_world_state(project_id) or WorldState()

        existing = outline.chapter_ids()
        todo = [cid for cid in selected if cid not in existing]
        novel = (
            self.store.load_novel(project_id) if project.input_mode == InputMode.NOVEL else None
        )
        for cid in todo:
            source = novel.get_chapter(cid) if novel else None
            # 一次展开一章：多章挤在一个 prompt 里时模型会漏写后面的章
            extra = await script_service.expand_chapters(
                llm,
                project,
                bible,
                story,
                [cid],
                start_index=len(outline.scenes),
                sources={cid: source.text} if source else None,
            )
            outline.scenes.extend(extra.scenes)
            if extra.scenes:
                script.title = script.title or story.title
            self.store.save_outline(project_id, outline)
            self.store.save_script(project_id, script)

        if todo:
            # 追加了场次：旧分镜/媒体与新剧本不再对应
            self.store.remove_artifact(project_id, "storyboard")
            self._invalidate_media(project_id)

        # 目标时长 = 已展开章的预算之和：时长归一化、成本估算、结构校验都以它为准
        expanded = [c for c in story.chapters if c.id in outline.chapter_ids()]
        total = sum(
            c.duration_budget_sec or DEFAULT_CHAPTER_DURATION_SEC for c in expanded
        )
        if total > 0 and total != project.target_duration_sec:
            logger.info(
                "项目目标时长按已展开章对齐：%ds -> %ds（%d 章 / %d 场）",
                project.target_duration_sec,
                total,
                len(expanded),
                len(outline.scenes),
            )
            project.target_duration_sec = total
            self.store.save_project(project)

        outline.target_duration_sec = project.target_duration_sec
        self.store.save_outline(project_id, outline)
        self.store.save_script(project_id, script)
        self.store.save_world_state(project_id, world_state)
        logger.info(
            "章展开 %s：选中 %d 章（新增 %d）-> 累计 %d 场 / %ds",
            project_id,
            len(selected),
            len(todo),
            len(outline.scenes),
            project.target_duration_sec,
        )
        return outline, script, world_state

    def _is_partial_arc(self, project_id: str) -> bool:
        """当前剧本是否只是长篇剧集里的一段（还有章没展开）。

        partial 会影响两件事：结尾悬念不算硬伤、LLM 剧本医生不要求本段收束全篇。
        """
        story = self.store.load_story_outline(project_id)
        if story is None or not story.chapters:
            return False
        outline = self.store.load_outline(project_id)
        if outline is None:
            return True
        return len(outline.chapter_ids()) < len(story.chapters)

    def _blocking_issues(self, project_id: str, settings: Settings) -> list:
        """严格模式下的拦路问题（确认阶段用）。"""
        if not settings.script_strict:
            return []
        return consistency_service.has_errors(self.store.load_consistency(project_id))

    def _raise_if_blocked(self, project_id: str, action: str, force: bool) -> None:
        blocking = self._blocking_issues(project_id, get_settings())
        if force or not blocking:
            return
        head = "；".join(f"[{i.category}] {i.message}" for i in blocking[:3])
        more = f"（另有 {len(blocking) - 3} 条）" if len(blocking) > 3 else ""
        raise PipelineError(
            f"{action}被结构校验拦下：{head}{more}。"
            "先按建议修改（或重新生成），确认没问题可加 force=true 强制继续"
        )

    async def confirm_script(self, project_id: str, *, force: bool = False) -> Project:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, CONFIRM_SCRIPT_STATUSES, "确认剧本")
            self._raise_if_blocked(project_id, "确认剧本", force)
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

    async def generate_storyboard(self, project_id: str, model: str = "") -> Storyboard:
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
            self._guard_budget(project_id, action="生成分镜")
            llm = self._llm(model)
            try:
                storyboard = await self._run_storyboard_generation(project, script, bible, llm)
            except Exception as exc:
                self._fail(project, "分镜生成")
                raise PipelineError(f"分镜生成失败：{exc}") from exc
            finally:
                self._record_llm_cost(project_id, CostStage.STORYBOARD, llm)
                await llm.aclose()

            # 先做确定性修复（说话人不在画面里 → 补进去），再校验：修完就不该再报这条
            storyboard, repairs = consistency_service.repair_storyboard(
                storyboard, script, bible
            )
            if repairs:
                self.store.save_storyboard(project_id, storyboard)
                logger.info("分镜自动修复 %d 处：%s", len(repairs), "；".join(repairs))
            script_report = consistency_service.validate_script(
                script,
                bible,
                self.store.load_world_state(project_id),
                project.target_duration_sec,
                strict=get_settings().script_strict,
                partial=self._is_partial_arc(project_id),
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

        # 视频模型的最短计费档位（H3 = 5 秒）：分镜阶段就按它拆镜头，
        # 否则 3 秒的镜头也要买 5 秒（事后合并只能补救，不如一开始就不浪费）
        tier = await self._duration_tier()
        # 段落分镜：计费按段算，镜头本身不必被抬到最短档位（短镜头才有剪辑节奏）
        segment_mode = get_settings().video_segment_mode
        if tier is not None and not segment_mode:
            logger.info(
                "分镜按视频档位生成：最短 %gs / 目标 %ss（来源 %s）",
                tier.min_sec,
                tier.target_sec or round(tier.min_sec * 1.6, 1),
                tier.source,
            )

        covered = storyboard.scene_ids() if resuming else set()
        outline = self.store.load_outline(project_id)
        story = self.store.load_story_outline(project_id)
        by_scene = {s.id: s for s in (outline.scenes if outline else [])}
        for scene in script.scenes:
            if scene.id in covered:
                continue
            entry = by_scene.get(scene.id)
            shots = await storyboard_service.generate_shots_for_scene(
                llm,
                project,
                bible,
                scene,
                tier=None if segment_mode else tier,
                environment=entry.environment if entry else "",
                story_context=_chapter_context(story, entry),
                segment_mode=segment_mode,
            )
            storyboard.shots.extend(shots)
            self.store.save_storyboard(project_id, storyboard)

        if tier is not None and not segment_mode:
            warnings = storyboard_service.apply_duration_tier(storyboard, script, tier)
            for warning in warnings:
                logger.warning("分镜档位对齐：%s", warning)
        else:
            storyboard_service.normalize_storyboard_durations(storyboard, script)
            storyboard.duration_tier = tier
        if segment_mode:
            await self._apply_segment_mode(storyboard, tier=tier, query_tier=False)
        self.store.save_storyboard(project_id, storyboard)
        return storyboard

    async def confirm_storyboard(self, project_id: str, *, force: bool = False) -> Project:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, CONFIRM_STORYBOARD_STATUSES, "确认分镜")
            self._raise_if_blocked(project_id, "确认分镜", force)
            # 只表示「分镜已确认」。视频/配音/合成阶段尚未实现，不虚报 VIDEO_READY
            project.status = ProjectStatus.STORYBOARD_CONFIRMED
            self.store.save_project(project)
            return project

    async def reopen_audio(self, project_id: str) -> Project:
        """退回配音阶段（成片作废）。

        场景：片子已经 DONE，但想把某个角色的音色/念法换掉再重做配音 ——
        没有这个入口时状态机不允许重新生成配音，用户只能重建项目。
        """
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, REOPEN_AUDIO_STATUSES, "退回配音阶段")
            project.status = ProjectStatus.VIDEO_READY
            self.store.save_project(project)
            return project

    async def reopen_storyboard(self, project_id: str) -> Project:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, REOPEN_STORYBOARD_STATUSES, "退回分镜阶段")
            project.status = ProjectStatus.SCRIPT_CONFIRMED
            self.store.save_project(project)
            return project

    async def storyboard_impact(self, project_id: str, data: dict) -> dict:
        """只算不写：按这份分镜保存的话，哪些已生成的段要重做、大概多少钱。"""
        async with self._lock(project_id):
            self.get_project(project_id)
            storyboard = await self._parse_storyboard(data)
            impact = video_service.storyboard_impact(
                self.store.load_storyboard(project_id),
                storyboard,
                self.store.load_video_manifest(project_id),
            )
        preflight = await self.preflight(project_id, action="video")
        price = ((preflight.get("video") or {}).get("price_per_second")) or 0.0
        impact["price_per_second"] = price
        impact["estimated_usd"] = round(impact["stale_seconds"] * price, 4)
        return impact

    async def _parse_storyboard(self, data: dict) -> Storyboard:
        try:
            storyboard = Storyboard.model_validate(data)
        except Exception as exc:
            raise PipelineError(f"分镜更新失败：{exc}") from exc
        # 镜头改了时长/顺序，段落要按新分镜重编（编段按场确定性计算，别的场不受影响）
        return await self._apply_segment_mode(storyboard)

    async def update_storyboard(self, project_id: str, data: dict) -> Storyboard:
        """更新分镜。已生成的媒体**只作废受影响的段**（`video_service.storyboard_impact`）。"""
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(
                project,
                EDIT_STORYBOARD_STATUSES | EDIT_CONFIRMED_STORYBOARD_STATUSES,
                "修改分镜",
            )
            storyboard = await self._parse_storyboard(data)
            old = self.store.load_storyboard(project_id)
            video = self.store.load_video_manifest(project_id)
            audio = self.store.load_audio_manifest(project_id)
            impact = video_service.storyboard_impact(old, storyboard, video)
            project_dir = self.store.ensure_project_dir(project_id)
            self.store.save_storyboard(project_id, storyboard)
            if video is not None:
                video_service.invalidate_clips(video, impact["stale_shots"], project_dir)
                gone = {c.shot_id for c in video.clips} - {s.id for s in storyboard.shots}
                video_service.invalidate_clips(video, sorted(gone), project_dir, drop=True)
                self.store.save_video_manifest(project_id, video)
            if audio is not None:
                # 配音按镜头文本做差异：没改字的条目下次配音直接复用；镜头删了的条目丢掉
                keep = {s.id for s in storyboard.shots}
                audio.clips = [c for c in audio.clips if c.shot_id in keep]
                self.store.save_audio_manifest(project_id, audio)
            self.store.purge_media(project_id, compose=True)
            if project.status in EDIT_CONFIRMED_STORYBOARD_STATUSES:
                ready = video is not None and all(
                    (clip := video.get(s.id)) is not None and clip.status == ClipStatus.READY
                    for s in storyboard.shots
                )
                project.status = (
                    ProjectStatus.VIDEO_READY if ready else ProjectStatus.STORYBOARD_CONFIRMED
                )
                self.store.save_project(project)
            if impact["stale_segments"] or impact["removed_shots"]:
                logger.info(
                    "分镜修改：作废 %d 段（%d 镜）、删除 %d 镜，保留 %d 段 project=%s",
                    len(impact["stale_segments"]),
                    len(impact["stale_shots"]),
                    len(impact["removed_shots"]),
                    impact["kept_segments"],
                    project_id,
                )
            self._revalidate(
                project, self.store.load_bible(project_id), self.store.load_script(project_id)
            )
            return storyboard

    async def invalidate_video(
        self, project_id: str, *, segments: list[str] | None = None, shots: list[str] | None = None
    ) -> dict:
        """把指定的段（或镜头所在的整段）标回待生成，下次「生成视频片段」只重买这些段。

        段是一次生成的，没法只重做其中一个镜头 —— 给镜头 id 也会扩成它所在的整段。
        """
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, EDIT_CONFIRMED_STORYBOARD_STATUSES, "重做视频片段")
            storyboard = self.store.load_storyboard(project_id) or Storyboard()
            video = self.store.load_video_manifest(project_id)
            if video is None:
                raise PipelineError("还没有视频片段")
            wanted_segments = set(segments or [])
            wanted_shots = set(shots or [])
            targets: list[dict] = []
            for segment in storyboard_service.segments_of(storyboard):
                ids = [s.id for s in segment.shots]
                if segment.id in wanted_segments or wanted_shots & set(ids):
                    targets.append(
                        {
                            "segment_id": segment.id,
                            "shots": ids,
                            "seconds": sum(max(1, int(s.duration_sec)) for s in segment.shots),
                        }
                    )
            if not targets:
                raise PipelineError("没有找到要重做的段 / 镜头")
            stale = [i for t in targets for i in t["shots"]]
            video_service.invalidate_clips(
                video, stale, self.store.ensure_project_dir(project_id)
            )
            self.store.save_video_manifest(project_id, video)
            self.store.purge_media(project_id, compose=True)
            project.status = ProjectStatus.STORYBOARD_CONFIRMED
            self.store.save_project(project)
            logger.info("手动重做 %d 段（%d 镜）project=%s", len(targets), len(stale), project_id)
            return {"segments": targets, "shots": stale}

    # ── 成本前置（preflight）──────────────────────────────

    async def preflight(
        self, project_id: str, *, action: str = "all", script_mode: str = ""
    ) -> dict:
        """动手花钱之前把账算清楚（镜头数 / 计费秒数 / 单价 / 预算余额 / 省钱建议）。

        与真正扣费同一口径：能问 provider 就用它的单价与档位，问不到才退回配置单价。
        provider 都建不起来（没 key / 没依赖）也不报错 —— 退回配置单价 + 告警，
        因为「看一眼要花多少」这件事本身不该失败。
        """
        project = self.get_project(project_id)
        provider = None
        try:
            provider = self._video()
        except Exception as exc:  # noqa: BLE001 - 估不出价不该让接口 500
            logger.warning("视频 provider 初始化失败，按配置单价估算：%s", exc)
        try:
            storyboard = self.store.load_storyboard(project_id)
            video = self.store.load_video_manifest(project_id)
            if storyboard is not None and storyboard.shots:
                # 只算不写：没编过段的老分镜按「会怎么编」来估价（与真正生成同一口径）
                storyboard = await self._apply_segment_mode(
                    storyboard.model_copy(deep=True), video=video, only_if_missing=True
                )
            return await preflight_service.preflight(
                project=project,
                storyboard=storyboard,
                video=video,
                audio=self.store.load_audio_manifest(project_id),
                provider=provider or preflight_service.UnavailableVideoProvider(),
                report=self.cost_report(project_id),
                settings=get_settings(),
                action=action,
                script_mode=script_mode,
                keyframe_estimator=(
                    self._keyframe_estimator(project, provider) if provider else None
                ),
            )
        finally:
            if provider is not None:
                await provider.aclose()

    def _keyframe_estimator(
        self, project: Project, provider: VideoProvider
    ) -> preflight_service.KeyframeEstimator:
        """preflight / 预算门控共用：这几段要画几张开场关键帧（只认首帧的模型才会画）。"""

        async def estimate(segments: list) -> dict:
            return await keyframe_service.estimate(
                segments,
                project,
                self.store.load_bible(project.id),
                self.store.ensure_project_dir(project.id),
                provider,
            )

        return estimate

    async def merge_short_shots(
        self,
        project_id: str,
        *,
        min_sec: float | None = None,
        max_sec: float = 0.0,
        dry_run: bool = True,
        force: bool = False,
    ) -> dict:
        """把太短的镜头合并成更少更长的片段（省钱），`dry_run` 只算不写。

        安全底线：
        - 有对白的镜头绝不被吸收、也绝不互相合并（不同说话人不能共用一条配音）
        - 已经有**可用片段**时默认拒绝（合并会让花钱买来的片段作废），要 `force=True`
        """
        self._reject_if_running(project_id, VIDEO_JOB_KIND, "视频生成")
        async with self._lock(project_id):
            project = self.get_project(project_id)
            self._require(project, GENERATE_VIDEO_STATUSES, "合并镜头")
            storyboard = self.store.load_storyboard(project_id)
            if not storyboard or not storyboard.shots:
                raise PipelineError("分镜不存在或为空，无法合并镜头")

            settings = get_settings()
            target = min_sec if min_sec is not None else settings.video_min_shot_sec
            if not target or target <= 0:
                target = await self._min_billable_seconds()

            video = self.store.load_video_manifest(project_id)
            ready = len(video.ready_clips) if video else 0
            report = storyboard_service.merge_shots(
                storyboard, min_sec=target, max_sec=max_sec, dry_run=True
            )
            if dry_run:
                payload = report.summary()
                payload["ready_clips"] = ready
                payload["force_required"] = bool(ready)
                return payload

            if ready and not force:
                raise PipelineError(
                    f"已有 {ready} 个可用片段，合并会让它们作废（重新生成要再花钱）。"
                    "确认要合并请带 force=true"
                )

            report = storyboard_service.merge_shots(
                storyboard, min_sec=target, max_sec=max_sec, dry_run=False
            )
            if report.to_shots != report.from_shots:
                self.store.save_storyboard(project_id, storyboard)
                # 镜头没了/时长变了 -> 片段、配音、成片全部作废
                self.store.purge_media(project_id, video=True, audio=True, compose=True)
                was_video_ready = project.status in {
                    ProjectStatus.VIDEO_READY,
                    ProjectStatus.AUDIO_READY,
                    ProjectStatus.DONE,
                    ProjectStatus.AUDIO_GENERATING,
                    ProjectStatus.COMPOSING,
                }
                project.status = ProjectStatus.STORYBOARD_CONFIRMED
                self.store.save_project(project)
                logger.info(
                    "合并短镜头：%d -> %d 个镜头（最短 %ss），下游媒体已作废（原状态 %s）",
                    report.from_shots,
                    report.to_shots,
                    target,
                    "video_ready 等" if was_video_ready else project.status.value,
                )
            payload = report.summary()
            payload["ready_clips"] = ready
            payload["force_required"] = False
            payload["purged_media"] = report.to_shots != report.from_shots
            return payload

    async def _duration_tier(self) -> DurationTier | None:
        """拿视频模型的计费档位（分镜阶段用来决定「一场分几镜」）。

        问不到就退回配置里的 `VIDEO_MIN_SHOT_SEC`；都没有就返回 None = 不约束分镜
        （mock provider 就是这种：它按真实秒数计费，没有最短档位）。
        """
        settings = get_settings()
        configured = max(0.0, float(settings.video_min_shot_sec or 0.0))
        provider = None
        options: list[float] = []
        source = "config"
        try:
            provider = self._video()
            options = list(await provider.duration_options())
            if options:
                source = "provider"
        except Exception:  # noqa: BLE001 - 问不到档位就不约束分镜，别卡住生成
            logger.warning("视频档位查询失败，分镜不受档位约束（可用 VIDEO_MIN_SHOT_SEC 兜底）")
        finally:
            if provider is not None:
                await provider.aclose()

        min_sec = options[0] if options else configured
        if min_sec <= 1.0:
            return None
        max_sec = options[-1] if len(options) > 1 else float(settings.video_max_shot_sec or 0.0)
        target = max(0.0, float(settings.video_shot_target_sec or 0.0))
        return DurationTier(
            min_sec=round(min_sec, 3),
            max_sec=round(max_sec, 3),
            target_sec=round(target, 3),
            source=source,
        )

    async def _min_billable_seconds(self) -> float:
        """问 provider 的最短计费档位（问不到就按 1 秒 = 没有档位约束）。"""
        provider = None
        try:
            provider = self._video()
            options = await provider.duration_options()
        except Exception:  # 问不到就不做档位对齐
            logger.warning("视频档位查询失败，合并按 1 秒处理", exc_info=True)
            return 1.0
        finally:
            if provider is not None:
                await provider.aclose()
        return options[0] if options else 1.0

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

            # 只为「还没生成的那部分」预留预算；真实模型按自己的档位算秒数。
            # 注意：换 provider/模型时旧片段会**全部重跑**，预算必须按全部算 ——
            # 否则「生成缺失片段」会把预算门控绕过去（真实场景：mock → H3 一次 $9.75）。
            video = self.store.load_video_manifest(project_id)
            storyboard = await self._apply_segment_mode(
                storyboard, video=video, only_if_missing=True
            )
            self.store.save_storyboard(project_id, storyboard)
            targets = await self._budget_shots(storyboard, video)
            upcoming = await self._estimate_video_usd(targets, storyboard)
            upcoming += await self._estimate_keyframe_usd(project, targets, storyboard)
            self._guard_budget(
                project_id, upcoming_usd=upcoming, action="生成视频片段"
            )
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
        """生成视频片段。

        **项目锁只包「读状态 → 改状态」**，不包几分钟的生成过程：早期版本把整段
        `generate_clips` 放在锁里，于是生成期间「改标题 / 删项目 / 开始合成」
        这些请求全部挂住（真实症状：HTTP 请求几分钟不返回）。
        并发保护仍然靠 job 互斥（`_reject_if_if_running`）+ 状态机校验，
        manifest 的写入本身是原子的，不需要长时间持锁。
        """
        async with self._lock(project_id):
            project = self.get_project(project_id)
            storyboard = self.store.load_storyboard(project_id) or Storyboard()
            provider = self._video()
            project_dir = self.store.ensure_project_dir(project_id)
            clips_dir = self.store.clips_dir(project_id)
            bible = self.store.load_bible(project_id)
            settings = get_settings()
            existing = self.store.load_video_manifest(project_id)
            # 批量链路不经过 start_video_generation，这里再兜一次编段（已编过就不动）
            storyboard = await self._apply_segment_mode(
                storyboard, video=existing, only_if_missing=True
            )
            if storyboard.shots:
                self.store.save_storyboard(project_id, storyboard)
            # 用「上一次的生成时间」判断这一轮是否真的重跑过片段（换 provider 也算重跑）
            before_generated = {
                c.shot_id: c.generated_at for c in (existing.clips if existing else [])
            }

        def on_progress(manifest: VideoManifest) -> None:
            # 进度回调是同步的（在事件循环里直接跑完，不会被其它协程打断），
            # 里面只有一次原子落盘，因此不需要项目锁
            self.store.save_video_manifest(project_id, manifest)
            done = len(manifest.ready_clips) + len(manifest.failed_clips)
            ctx.set_progress(
                done=done,
                total=len(manifest.clips),
                message=f"片段 {done}/{len(manifest.clips)}（成功 {len(manifest.ready_clips)}，"
                f"失败 {len(manifest.failed_clips)}）",
            )

        try:
            # 单价要在 provider 关闭前问（真实 provider 会去查自己的价目表）
            price_per_second = await provider.price_per_second()
        except Exception:
            logger.warning("读取视频单价失败，记账用配置单价", exc_info=True)
            price_per_second = None
        keyframer = None
        if await keyframe_service.enabled_for(provider, settings):
            keyframer = keyframe_service.Keyframer(
                self.store, project_id, project, bible, settings=settings
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
                existing=existing,
                bible=bible,
                seed=settings.video_seed,
                on_progress=on_progress,
                keyframer=keyframer,
            )
        except asyncio.CancelledError:
            # 取消：保留已生成片段，状态退回可重试的位置
            async with self._lock(project_id):
                self.store.purge_media(project_id, compose=True)
                # **取消也要记账**：已经落盘的片段 provider 已经按秒计过费了，
                # 不记的话账本就少一笔（真实事故：取消时 3 个片段共 $1.95 没进账本，
                # 而片段随后被删掉，连「补记账」的机会都没有了）。
                # 仍按片段上的 `cost_recorded` 标记记，所以续跑不会重复计费。
                cancelled = self.store.load_video_manifest(project_id)
                recorded = self._record_video_cost(
                    project_id,
                    cancelled,
                    price_per_second=price_per_second,
                    settings=settings,
                )
                if recorded:
                    self.store.save_video_manifest(project_id, cancelled)
                    logger.info("视频任务被取消，已为已生成片段补记账 $%.4f", recorded)
                self._settle_video_status(
                    self.get_project(project_id),
                    self.store.load_video_manifest(project_id),
                    self.store.load_audio_manifest(project_id),
                )
            raise
        finally:
            await provider.aclose()
            if keyframer is not None:
                await keyframer.aclose()

        async with self._lock(project_id):
            # 状态可能在生成期间被别处改过（例如用户改了标题），这里重新读一份再落状态
            project = self.get_project(project_id)
            recorded = self._record_video_cost(
                project_id, manifest, price_per_second=price_per_second, settings=settings
            )
            # 必须在记账（打 cost_recorded 标记）之后再落盘，否则标记丢了会重复记账
            self.store.save_video_manifest(project_id, manifest)
            if recorded:
                logger.info(
                    "视频阶段记账 $%.4f（%s）", recorded, manifest.model or manifest.provider
                )
            # 只有**真的重跑了片段**才作废旧成片：否则「补记账」这种空跑会把成片删掉
            regenerated = [
                c
                for c in manifest.ready_clips
                if c.generated_at and before_generated.get(c.shot_id) != c.generated_at
            ]
            if regenerated:
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

    def _record_video_cost(
        self,
        project_id: str,
        manifest: VideoManifest,
        *,
        price_per_second: float | None,
        settings: Settings,
    ) -> float:
        """把「还没记过账」的片段花费写进账本，返回这次记了多少 USD。

        按片段上的 `cost_recorded` 标记，而不是「和上一次清单做集合差」：
        1. **换 provider 全部重跑**时 shot_id 没变，差集会空掉（真实事故：$9.75 一条都没记）
        2. 进程在「片段已落盘、账本还没写」时挂掉，下次续跑只补记账，不会重复计费
        3. 失败重试成功只记一次
        """
        # 段落整段已买到但切分失败的片段是 FAILED 却带着扣费：也必须记账
        unrecorded = [
            c
            for c in manifest.clips
            if not c.cost_recorded
            and (c.status == ClipStatus.READY or (c.status == ClipStatus.FAILED and c.cost_usd > 0))
        ]
        if not unrecorded:
            return 0.0
        actual = round(sum(c.cost_usd for c in unrecorded), 6)
        seconds = sum(c.duration_sec for c in unrecorded)
        if actual > 0 and price_per_second:
            expected = seconds * price_per_second
            if expected > 0 and actual > expected * 1.2:
                logger.warning(
                    "实际扣费 $%.4f 高于价目表估算 $%.4f（%s）："
                    "建议按实际值调整 VIDEO_PRICE_PER_SECOND 或依赖账本",
                    actual,
                    expected,
                    manifest.model,
                )
        cost_service.record(
            self.store,
            project_id,
            cost_service.video_entry(
                manifest.provider,
                manifest.model or settings.video_provider,
                seconds,
                len(unrecorded),
                price_per_second=price_per_second,
                actual_usd=actual if actual > 0 else None,
            ),
        )
        for clip in unrecorded:
            clip.cost_recorded = True
        return actual

    async def reconcile_video_cost(self, project_id: str) -> dict:
        """把视频清单里「已实测但没记账」的花费补进账本。

        给老版本留下的项目用：只读片段上的 `cost_usd`（provider 报的实际扣费），
        **不调 API、不动媒体、不动项目状态**，可以反复调用（幂等）。
        """
        self.get_project(project_id)
        manifest = self.store.load_video_manifest(project_id)
        if manifest is None:
            raise PipelineError("视频片段清单不存在，没有可补记的花费")
        before = self.cost_report(project_id)
        recorded = self._record_video_cost(
            project_id, manifest, price_per_second=None, settings=get_settings()
        )
        self.store.save_video_manifest(project_id, manifest)
        report = self.cost_report(project_id)
        return {
            "recorded_usd": round(recorded, 6),
            "clips": len(manifest.ready_clips),
            "total_before_usd": before.total_usd,
            "total_after_usd": report.total_usd,
            "budget_usd": report.budget_usd,
            "remaining_usd": report.remaining_usd,
            "warnings": [] if recorded else ["没有需要补记的片段花费"],
        }

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
        self,
        project_id: str,
        voice_overrides: dict[str, str] | None = None,
        *,
        script_mode: str | None = None,
        use_vision: bool | None = None,
        force_script: bool | None = None,
    ) -> Job:
        """提交配音后台任务（进度用 audio_status 查）。

        `voice_overrides` 可以按角色换音色；换了音色的条目会重新合成（其余续跑）。
        `script_mode` 覆盖配置里的配音文案模式（off / narration / polish / full），
        非 off 时会先让 LLM 看着画面写/改文案（会花 LLM 额度）。
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

            settings = get_settings()
            mode = audio_script_service.normalize_mode(
                script_mode if script_mode is not None else settings.audio_script_mode
            )
            upcoming = cost_service.estimate_audio_usd(storyboard)
            if mode != "off":
                script_shots = self._script_target_count(storyboard, mode, force=force_script)
                upcoming += audio_script_service.estimate_voice_script_usd(
                    script_shots,
                    frames=audio_script_service.planned_frames(script_shots, settings),
                )
            self._guard_budget(project_id, upcoming_usd=upcoming, action="生成配音")
            project.status = ProjectStatus.AUDIO_GENERATING
            self.store.save_project(project)

        return self.jobs.submit(
            project_id,
            AUDIO_JOB_KIND,
            lambda ctx: self._run_audio_job(
                ctx,
                project_id,
                voice_overrides,
                script_mode=script_mode,
                use_vision=use_vision,
                force_script=force_script,
            ),
        )

    @staticmethod
    def _script_target_count(storyboard: Storyboard, mode: str, *, force: bool | None) -> int:
        """预算门禁用：这一轮大概要处理多少个镜头。"""
        settings = get_settings()
        resolved_force = settings.audio_llm_force if force is None else force
        groups, _ = audio_script_service.script_targets(storyboard, mode, force=resolved_force)
        return sum(len(shots) for _, shots in groups)

    def _record_audio_cost(self, project_id: str, manifest: AudioManifest) -> float:
        """把「还没记过账」的配音花费写进账本，返回这次记了多少 USD。

        与 `_record_video_cost` 同一套规则：按条目上的 `cost_recorded` 标记记，
        所以「取消 / 崩溃后续跑」既不会漏记也不会重复计费。
        """
        unrecorded = [c for c in manifest.ready_clips if not c.cost_recorded]
        if not unrecorded:
            return 0.0
        reported = sum(max(0.0, c.provider_usd) for c in unrecorded)
        cost_service.record(
            self.store,
            project_id,
            cost_service.audio_entry(
                manifest.provider,
                sum(count_speech_chars(c.text) for c in unrecorded),
                len(unrecorded),
                # 云端 TTS 会报实际花费；全为 0（Edge 免费）时按字符单价估算
                actual_usd=reported if reported > 0 else None,
            ),
        )
        for clip in unrecorded:
            clip.cost_recorded = True
        return round(reported, 6)

    async def _run_audio_job(
        self,
        ctx: JobContext,
        project_id: str,
        voice_overrides: dict[str, str] | None = None,
        *,
        script_mode: str | None = None,
        use_vision: bool | None = None,
        force_script: bool | None = None,
    ) -> None:
        """生成配音（可选：先让 LLM 看画面写/改文案）。

        项目锁只包「读状态 → 落状态」，几分钟的 TTS 生成放在锁外 ——
        理由同 `_run_video_job`：早期版本整段持锁，配音期间改标题等请求会全部挂住。
        并发仍然靠 job 互斥（同项目同类型任务只允许一个）保护。
        """
        async with self._lock(project_id):
            project = self.get_project(project_id)
            storyboard = self.store.load_storyboard(project_id) or Storyboard()
            bible = self.store.load_bible(project_id)
            script = self.store.load_script(project_id)
            video = self.store.load_video_manifest(project_id)
            provider = self._tts()
            project_dir = self.store.ensure_project_dir(project_id)
            audio_dir = self.store.audio_dir(project_id)
            settings = get_settings()
            mode = audio_script_service.normalize_mode(
                script_mode if script_mode is not None else settings.audio_script_mode
            )
            existing_audio = self.store.load_audio_manifest(project_id) or AudioManifest()
            existing_audio.script_mode = mode
            dub_mode = video_service.dub_mode_for(project, settings)

        def on_progress(manifest: AudioManifest) -> None:
            # 同步回调 + 原子落盘，不需要持项目锁（同 _run_video_job）
            self.store.save_audio_manifest(project_id, manifest)
            done = len(manifest.ready_clips) + len(manifest.failed_clips)
            total = len(manifest.clips)
            ctx.set_progress(
                done=done,
                total=total,
                message=(
                    f"配音 {done}/{total}（成功 {len(manifest.ready_clips)}，"
                    f"失败 {len(manifest.failed_clips)}）"
                )
                if total
                else "配音文案准备中…",
            )

        if dub_mode == "native":
            # 原声模式：对白 / 旁白都用视频模型原片里的声音，不调 TTS、不写念法、不花钱
            await provider.aclose()
            manifest = AudioManifest(
                provider="native",
                voice_map=existing_audio.voice_map,
                warnings=[
                    "原声模式（AUDIO_DUB_MODE=native）：不做 TTS 配音，所有镜头用视频原声；"
                    "要按镜头配音改成 tts"
                ],
            )
            ctx.note("原声模式：跳过 TTS 配音")
        else:
            # ── 可选：让 LLM 看着真实画面写/改配音文案 ──
            await self._apply_voice_script(
                ctx,
                project,
                storyboard,
                script,
                bible,
                video,
                project_dir=project_dir,
                settings=settings,
                mode=mode,
                use_vision=use_vision,
                force=force_script,
                manifest=existing_audio,
                on_progress=on_progress,
            )

            # ── 可选：原声引导（视频模型念过一遍台词，照着它的位置和节奏重配） ──
            guides: dict = {}
            if settings.dub_guide_mode == "native":
                guides = await dub_guide_service.prepare_guides(
                    storyboard, video, project_dir, audio_dir / dub_guide_service.GUIDE_DIR
                )
                ctx.note(f"原声引导：{len(guides)} 句台词找到了原声")
                if not guides:
                    logger.warning(
                        "原声引导已打开，但没有一个片段带可用的原声（视频要用 "
                        "VIDEO_GENERATE_AUDIO=true 生成）；按普通配音处理 project=%s",
                        project_id,
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
                    existing=existing_audio,
                    voice_overrides=voice_overrides,
                    guides=guides,
                    native_narration=video_service.native_narration_shots(storyboard, video),
                    on_progress=on_progress,
                )
            except asyncio.CancelledError:
                async with self._lock(project_id):
                    # 取消也要记账（理由同 `_run_video_job`）：已经合成的配音 TTS 已经计过费
                    cancelled = self.store.load_audio_manifest(project_id)
                    if cancelled is not None:
                        recorded = self._record_audio_cost(project_id, cancelled)
                        if recorded:
                            self.store.save_audio_manifest(project_id, cancelled)
                            logger.info("配音任务被取消，已为已生成条目补记账 $%.4f", recorded)
                    self._settle_audio_status(
                        self.get_project(project_id),
                        self.store.load_audio_manifest(project_id),
                        self.store.load_video_manifest(project_id),
                    )
                raise
            finally:
                await provider.aclose()

        async with self._lock(project_id):
            # 状态可能在生成期间被别处改过，重新读一份再落
            project = self.get_project(project_id)
            # 选角结果写回圣经：角色卡片上能看到/改到自己的音色，重跑也不会换人
            if bible is not None and manifest.voice_map:
                touched = False
                for index, character in enumerate(bible.characters):
                    voice = manifest.voice_map.get(character.id, "")
                    if voice and character.voice_id != voice:
                        bible.characters[index] = character.model_copy(
                            update={"voice_id": voice}
                        )
                        touched = True
                if touched:
                    self.store.save_bible(project_id, bible)

            # 与视频同一套规则：按条目上的「已记账」标记记，而不是和上次清单做集合差
            # （否则「文案被 LLM 改写后重新合成」这种同 id 重跑会漏记）
            self._record_audio_cost(project_id, manifest)
            if (manifest.ready_clips or dub_mode == "native") and (
                settings.bgm_engine_mode == "library"
            ):
                # 曲库选曲很便宜（只读元数据），配音阶段先选好，UI 能提前看到用哪首。
                # 生成类引擎（generate / cli / auto）留到**合成**阶段：那时才有画面运动实测，
                # 而且「重新合成」就能换配乐，不用重跑配音（配音要花钱，配乐不用）。
                manifest.bgm = audio_service.pick_bgm(
                    storyboard, self._bgm(), preferred_id=settings.bgm_id
                )
            elif settings.bgm_engine_mode != "library":
                manifest.bgm = None
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
            self._settle_audio_status(
                project, manifest, self.store.load_video_manifest(project_id)
            )

        failed, ready = len(manifest.failed_clips), len(manifest.ready_clips)
        ctx.set_progress(
            done=len(manifest.clips),
            total=len(manifest.clips),
            message=f"完成：成功 {ready}，失败 {failed}",
        )
        if failed and not ready:
            raise PipelineError(f"全部 {failed} 条配音生成失败，请检查 TTS 配置 / 网络")

    async def _apply_voice_script(
        self,
        ctx: JobContext,
        project: Project,
        storyboard: Storyboard,
        script: Script | None,
        bible: StoryBible | None,
        video: VideoManifest | None,
        *,
        project_dir: Path,
        settings: Settings,
        mode: str,
        use_vision: bool | None,
        force: bool | None,
        manifest: AudioManifest,
        on_progress: Callable[[AudioManifest], None],
    ) -> None:
        """配音前置：让 LLM 看着画面写/改文案，把结果写回分镜。

        失败一律降级：文案写不出来就用原文案继续配音，绝不让这一步把配音打死。
        """
        if mode == "off":
            return
        llm = self._llm(settings.audio_llm_model)
        ctx.set_progress(done=0, total=0, message=f"生成配音文案（{mode}）…")
        try:
            outcome = await audio_script_service.apply_voice_script(
                llm=llm,
                project=project,
                storyboard=storyboard,
                script=script,
                bible=bible,
                video=video,
                project_dir=project_dir,
                frames_dir=self.store.frames_dir(project.id),
                settings=settings,
                mode=mode,
                use_vision=use_vision,
                force=force,
            )
        except Exception as exc:  # 文案阶段失败不能阻断配音
            logger.warning("配音文案阶段失败，改用原文案配音", exc_info=True)
            manifest.warnings.append(
                f"配音文案（{mode}）生成失败，已用原文案配音："
                f"{scrub_secrets(f'{type(exc).__name__}: {exc}')[:200]}"
            )
            self.store.save_audio_manifest(project.id, manifest)
            on_progress(manifest)
            return
        finally:
            self._record_llm_cost(project.id, CostStage.AUDIO, llm)
            await llm.aclose()

        manifest.script_mode = outcome.mode
        manifest.script_model = outcome.model
        manifest.script_frames += outcome.frames
        manifest.script_lines += outcome.touched
        for warning in outcome.warnings:
            if warning not in manifest.warnings:
                manifest.warnings.append(warning)
        log_summary = logger.info if outcome.touched else logger.warning
        log_summary(
            "配音文案（%s）：%d 个镜头被写/改，跳过 %d 个已生成的，警告 %d 条",
            outcome.mode,
            outcome.touched,
            outcome.skipped,
            len(outcome.warnings),
        )
        self.store.save_storyboard(project.id, storyboard)
        self.store.save_audio_manifest(project.id, manifest)
        on_progress(manifest)

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
                    "compose_speed",
                    "compose_subtitle_style",
                    "compose_subtitle_font",
                    "compose_subtitle_font_scale",
                    "compose_ambient_mode",
                    "compose_ambient_gain_db",
                    "bgm_id",
                    "bgm_gain_db",
                    "bgm_enabled",
                    "bgm_engine",
                    "bgm_mood",
                    "bgm_duck",
                    "bgm_force",
                    "music_note",
                    "audio_normalize",
                    "compose_loudness_normalize",
                    "compose_keep_segments",
                }
                clean = {k: v for k, v in overrides.items() if k in allowed and v is not None}
                if clean:
                    settings = settings.model_copy(update=clean)
                    logger.info("合成参数覆盖：%s", ", ".join(sorted(clean)))
            # 项目上定的字幕样式压过 .env，单次请求又压过项目
            if project.subtitle_style and "compose_subtitle_style" not in (overrides or {}):
                settings = settings.model_copy(
                    update={"compose_subtitle_style": project.subtitle_style}
                )

            # 配乐在**合成阶段**定：重新合成就能换配乐（本地计算，不用重跑配音）
            music_note = ""
            audio, music_note = await self._attach_music(
                project_id, project, storyboard, video, audio, settings, overrides or {}
            )

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

            if music_note:
                report.warnings.append(music_note)
            self.store.save_compose_report(project_id, report)
            cost_service.record(
                self.store,
                project_id,
                cost_service.compose_entry(
                    report.ffmpeg or "ffmpeg", report.duration_sec, len(report.segments)
                ),
            )
            project.status = ProjectStatus.DONE
            self.store.save_project(project)
            ctx.set_progress(
                done=len(report.segments),
                total=len(report.segments),
                message=f"成片完成：{report.duration_sec:.1f}s",
            )

    async def _attach_music(
        self,
        project_id: str,
        project: Project,
        storyboard: Storyboard,
        video: VideoManifest | None,
        audio: AudioManifest | None,
        settings,
        overrides: dict,
    ) -> tuple[AudioManifest | None, str]:
        """给这次合成定下配乐（曲库 / 本机现写 / 外部模型），返回（音频清单, 说明）。

        配乐永远是**可选**的：失败只记一条 warning，绝不因为没音乐就不出片。
        定下来的曲目会写回配音清单，UI 因此能看到/试听这次用的音乐。
        """
        request = music_service.MusicRequest(
            engine=str(overrides.get("bgm_engine") or ""),
            mood=str(overrides.get("bgm_mood") or ""),
            note=str(overrides.get("music_note") or ""),
            gain_db=overrides.get("bgm_gain_db"),
            duck=overrides.get("bgm_duck"),
            force=bool(overrides.get("bgm_force")),
        )
        outcome = await music_service.resolve_music(
            project=project,
            storyboard=storyboard,
            video_manifest=video,
            audio_manifest=audio,
            output_dir=self.store.output_dir(project_id),
            frames_dir=self.store.frames_dir(project_id),
            settings=settings,
            library=self._bgm(),
            request=request,
        )
        if not outcome.ok:
            note = outcome.note or "配乐不可用"
            logger.warning("配乐跳过：%s", note)
            return audio, f"配乐跳过：{note}"
        if outcome.track is None:
            # 结果里没有曲目（关掉配乐 / 曲库为空）→ **必须清掉**清单里上一次的曲目，
            # 否则「这次不要 BGM」会偷偷继续用上一首（真实事故：A/B 的「无 BGM」版本里还有音乐）
            manifest = (audio or AudioManifest()).model_copy(update={"bgm": None})
            try:
                self.store.save_audio_manifest(project_id, manifest)
            except OSError:
                logger.debug("清空配乐写入配音清单失败", exc_info=True)
            # 「关掉配乐」不用在报告里记一条告警（那是配置，不是问题）
            return manifest, "" if outcome.engine == "none" else outcome.note
        manifest = audio or AudioManifest()
        manifest = manifest.model_copy(update={"bgm": outcome.track})
        try:
            self.store.save_audio_manifest(project_id, manifest)
        except OSError:
            logger.debug("配乐写入配音清单失败", exc_info=True)
        return manifest, outcome.note

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

    # ── 成本 / 批量 ───────────────────────────────────────

    def cost_status(self, project_id: str) -> dict:
        project = self.get_project(project_id)
        report = cost_service.load_report(self.store, project_id)
        storyboard = self.store.load_storyboard(project_id)
        video = self.store.load_video_manifest(project_id)
        ready = video.ready_clips if video else []
        # 片段上的 cost_usd 是 provider 报的**实测扣费**；账本可能漏记（老版本 bug）
        actual_video = round(sum(c.cost_usd for c in ready), 6)
        unrecorded_video = round(sum(c.cost_usd for c in ready if not c.cost_recorded), 6)
        ledger_video = round(sum(e.usd for e in report.entries if e.stage == CostStage.VIDEO), 6)
        return {
            "project_status": project.status.value,
            "video_actual_usd": actual_video,
            "video_ledger_usd": ledger_video,
            "video_unrecorded_usd": unrecorded_video,
            "summary": report.summary(),
            "stages": report.stage_totals(),
            "budget_usd": report.budget_usd,
            "prices": cost_service.PriceTable.from_settings().model_dump(),
            "estimate": {
                "video_usd": cost_service.estimate_video_usd(storyboard),
                "audio_usd": cost_service.estimate_audio_usd(storyboard),
            },
        }

    async def run_chain(
        self,
        project_id: str,
        *,
        stop_after: str = "compose",
        model: str = "",
        voices: dict[str, str] | None = None,
        overrides: dict | None = None,
        on_stage: Callable[[str], None] | None = None,
        script_mode: str | None = None,
    ) -> Project:
        """从文本一路跑到成片，**跳过人工确认门控**（批量生产用）。

        复用各自的任务执行体（`_run_*_job`），但用一个临时 JobContext，
        这样进度/错误处理与单步执行完全一致，不会出现两套逻辑。
        """
        def stage(name: str) -> None:
            if on_stage is not None:
                try:
                    on_stage(name)
                except Exception:  # pragma: no cover - 进度回调不该影响生产
                    logger.exception("批量阶段回调失败")

        stage("bible")
        await self.generate_bible(project_id, model=model)
        stage("script")
        chapters: list[str] | None = None
        if self.get_project(project_id).input_mode == InputMode.NOVEL:
            # 批量链路不选章：小说只拍第一章（整本书一口气出片的花费不可控）
            story = self.store.load_story_outline(project_id)
            chapters = [story.chapters[0].id] if story and story.chapters else None
        await self.generate_script(project_id, model=model, chapters=chapters)
        await self.confirm_script(project_id)
        stage("storyboard")
        await self.generate_storyboard(project_id, model=model)
        await self.confirm_storyboard(project_id)
        if stop_after == "storyboard":
            return self.get_project(project_id)

        stage("video")
        await self._run_video_job(self._scratch(project_id, VIDEO_JOB_KIND), project_id)
        if stop_after == "video":
            return self.get_project(project_id)

        stage("audio")
        await self._run_audio_job(
            self._scratch(project_id, AUDIO_JOB_KIND),
            project_id,
            voices,
            script_mode=script_mode,
        )
        if stop_after == "audio":
            return self.get_project(project_id)

        stage("compose")
        await self._run_compose_job(
            self._scratch(project_id, COMPOSE_JOB_KIND), project_id, overrides
        )
        return self.get_project(project_id)

    @staticmethod
    def _scratch(project_id: str, kind: str) -> JobContext:
        """给同步调用链用的临时上下文（不进任务表、不影响互斥）。"""
        return JobContext(Job(project_id=project_id, kind=kind))

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
                script,
                bible,
                self.store.load_world_state(project.id),
                project.target_duration_sec,
                strict=get_settings().script_strict,
                partial=self._is_partial_arc(project.id),
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
