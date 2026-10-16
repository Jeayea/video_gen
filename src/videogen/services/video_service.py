from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from videogen.config import Settings, get_settings
from videogen.models.bible import StoryBible
from videogen.models.project import Project
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import (
    ClipStatus,
    MediaKind,
    ReferenceRejection,
    VideoClip,
    VideoManifest,
)
from videogen.providers.video.base import (
    VideoArtifactError,
    VideoJobPending,
    VideoProvider,
    VideoReferenceRejected,
    VideoRequest,
)
from videogen.services import explainer_service, voice_catalog
from videogen.services.storyboard_service import Segment, chain_predecessors, segments_of
from videogen.utils import language, media_history, region
from videogen.utils.ffmpeg import cut_clip, detect_cuts, ffmpeg_available, probe_duration
from videogen.utils.redact import scrub_secrets

if TYPE_CHECKING:  # 关键帧服务反过来依赖本模块，这里只用于类型标注
    from videogen.services.keyframe_service import Keyframer

logger = logging.getLogger(__name__)

#: 片段卡在「已提交、没取回」时附在错误后面的提示
PENDING_HINT = "（下次生成会接着查这个任务、不重新计费；想放弃它重新生成，点这段的「重做这段」）"
#: 异常文案里已经说了「会接着查」时只补后半句
PENDING_HINT_SHORT = "（想放弃这个任务重新生成，点这段的「重做这段」）"


def _with_pending_hint(message: str) -> str:
    return message + (PENDING_HINT_SHORT if "接着查" in message else PENDING_HINT)

#: 老版本的错误文案：轮询超时 / 已生成但下载失败（都带 `job=<id>`）
_LEGACY_PENDING_RE = re.compile(r"(?:生成超时|已生成但下载失败)（job=([\w.-]+)")


def legacy_pending_job(clip: VideoClip) -> str:
    """老版本失败的片段里还没取回的任务 id（没有就返回空串）。"""
    if clip.status != ClipStatus.FAILED or clip.pending_job_id or clip.segment_path:
        return ""
    match = _LEGACY_PENDING_RE.search(clip.error or "")
    return match.group(1) if match else ""


#: `ReferenceRejection.kind`：开场关键帧没画出来（不是被视频模型拒收）
KEYFRAME_FAILED = "keyframe_failed"
#: 接戏帧（上一段的最后一帧）的文件名后缀：`keyframes/<段>.chain.jpg`
CHAIN_FRAME_SUFFIX = ".chain.jpg"


class KeyframeUnavailable(RuntimeError):
    """这一段需要开场关键帧，但没画出来（图片模型失败 / 预算不够 / 没产出文件）。

    **不退回纯文生视频**：那样钱照花、人物 / 场景一致性悄悄没了（和首帧被拒同一个坑）。
    上层把它当成「这一段的图有问题」中断，交给用户决定：重画 / 这段不带图。
    """

ProgressCallback = Callable[[VideoManifest], None] | None

#: 首次失败后等待多久再试（秒），测试里传 0
DEFAULT_RETRY_BACKOFF_SEC = 1.5


async def generate_clips(
    project: Project,
    storyboard: Storyboard,
    provider: VideoProvider,
    clips_dir: Path,
    project_dir: Path,
    *,
    concurrency: int = 3,
    max_retries: int = 2,
    existing: VideoManifest | None = None,
    bible: StoryBible | None = None,
    seed: int = 0,
    on_progress: ProgressCallback = None,
    retry_backoff_sec: float = DEFAULT_RETRY_BACKOFF_SEC,
    keyframer: Keyframer | None = None,
) -> VideoManifest:
    """逐镜并发生成视频片段。

    - **并发**：`concurrency` 控制同时在跑的镜头数
    - **重试**：单镜失败重试 `max_retries` 次，仍失败只标记该镜头，不影响其他镜头
    - **续跑**：已有 manifest 里 `ready` 且文件仍在的镜头直接跳过（可在失败后重试）
    - **进度**：每完成一个镜头回调一次 `on_progress`，调用方负责落盘/更新任务
    - **关键帧**：给了 `keyframer`（只认首帧的模型）时，每段先画一张开场关键帧当首帧
    - **图被拒就中断**：某段的首帧 / 参考图被视频模型拒收时记进 `manifest.rejections`，
      之后不再开始新的段（已提交的照常跑完），等用户决定（见 `ReferenceRejection`）
    """
    manifest = existing or VideoManifest()
    # 换了 provider / 模型（例如 mock → 真实模型）时，旧片段一律作废重跑，
    # 否则「跳过已就绪片段」的续跑逻辑会一直沿用占位视频，看起来像没生效。
    switched = provider_switched(manifest, provider)
    if switched:
        logger.warning(
            "视频 provider/模型变更：%s/%s -> %s/%s，已就绪的 %d 个片段将重新生成",
            manifest.provider or "?",
            manifest.model or "?",
            provider.name,
            provider.model or "?",
            len(manifest.ready_clips),
        )
        # 旧片段会在原位置被新片段覆盖：先留进历史版本
        old = manifest.model or manifest.provider
        _archive_clips(
            manifest.clips,
            project_dir,
            f"换模型重跑（{old} → {provider.model or provider.name}）",
            model=manifest.model,
        )
        for stale in manifest.clips:
            stale.status = ClipStatus.PENDING
            stale.error = ""
            stale.task_id = None
            stale.pending_job_id = ""  # 旧模型上没取回的任务不再要了
            stale.segment_path = ""
            # 旧花费已经记过账了，重跑要当成一笔新账（否则新花费永远进不了账本）
            stale.cost_usd = 0.0
            stale.cost_recorded = False
    manifest.provider = provider.name
    manifest.model = provider.model
    manifest.aspect_ratio = project.aspect_ratio.value
    manifest.concurrency = max(1, concurrency)

    shots = list(storyboard.shots)
    if not shots:
        raise ValueError("分镜为空，没有可生成的镜头")

    # 老版本留下的「超时 / 下载失败」片段没记 pending_job_id：从当时的错误里把 job id 捞回来，
    # 这次接着查原任务（服务端可能早已跑完、扣过费），而不是再买一份
    for clip in manifest.clips:
        recovered = legacy_pending_job(clip)
        if recovered:
            clip.pending_job_id = recovered
            logger.info("片段 %s：从旧错误里找回未取回的任务 %s，接着查", clip.shot_id, recovered)

    # 段落分镜：同一段的镜头一次生成；没编段的镜头各自成段（= 原来的单镜生成）
    todo: list[Segment] = []
    for segment in segments_of(storyboard):
        stale = False
        for shot in segment.shots:
            clip = manifest.get(shot.id)
            if not (
                clip is not None
                and clip.status == ClipStatus.READY
                and _clip_file_exists(project_dir, clip)
            ):
                stale = True
        if not stale:
            continue  # 续跑：已经生成好的不重复烧额度
        for shot in segment.shots:
            clip = manifest.get(shot.id) or VideoClip(shot_id=shot.id, scene_id=shot.scene_id)
            clip.status = ClipStatus.PENDING
            clip.error = ""
            clip.media_kind = MediaKind.VIDEO
            clip.duration_sec = shot.duration_sec
            clip.aspect_ratio = project.aspect_ratio.value
            clip.provider = provider.name
            clip.segment_id = shot.segment_id
            manifest.upsert(clip)
        todo.append(segment)

    _emit(on_progress, manifest)
    if not todo:
        logger.info("视频片段已全部就绪，无需生成（project=%s）", project.id)
        return manifest

    logger.info(
        "开始生成视频片段 project=%s 待生成 %d 段（%d 镜）/%d 镜 并发=%d 重试=%d",
        project.id,
        len(todo),
        sum(len(seg.shots) for seg in todo),
        len(shots),
        manifest.concurrency,
        max_retries,
    )

    semaphore = asyncio.Semaphore(manifest.concurrency)
    # 接戏帧：同场同地点的下一段等上一段生成完，拿它的最后一帧当首帧
    chain: dict[str, Segment] = {}
    if keyframer is not None and get_settings().video_keyframe_chain:
        chain = chain_predecessors(segments_of(storyboard), waived=manifest.chain_waived)
    done = {segment.id: asyncio.Event() for segment in todo}

    async def run_one(segment: Segment) -> None:
        try:
            previous = chain.get(segment.id)
            if previous is not None and previous.id in done:
                await done[previous.id].wait()  # 等的时候不占并发名额
            await run_segment(segment, _chain_source(previous, manifest, project_dir))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 一段出意外不能把别的段（已在花钱）一起带走
            message = scrub_secrets(f"{type(exc).__name__}: {exc}")[:400]
            logger.exception("段 %s 生成时出现意外错误", segment.id)
            for shot in segment.shots:
                clip = manifest.get(shot.id)
                if clip is not None and clip.status != ClipStatus.READY:
                    clip.status = ClipStatus.FAILED
                    clip.error = message
                    manifest.upsert(clip)
            _emit(on_progress, manifest)
        finally:
            done[segment.id].set()

    async def run_segment(segment: Segment, previous: Path | None) -> None:
        async with semaphore:
            if manifest.rejections:
                # 有段的图被拒了：后面的段先不开工（开工就花钱），等用户决定
                logger.info("段 %s 未开始：有段的参考图 / 首帧被拒，等用户决定", segment.id)
                return
            if len(segment.shots) > 1:
                await _generate_segment(
                    segment,
                    project,
                    provider,
                    clips_dir,
                    project_dir,
                    manifest,
                    bible=bible,
                    seed=seed,
                    max_retries=max_retries,
                    retry_backoff_sec=retry_backoff_sec,
                    on_progress=on_progress,
                    keyframer=keyframer,
                    previous=previous,
                )
                _emit(on_progress, manifest)
                return
            await _generate_one(
                segment.shots[0],
                project,
                provider,
                clips_dir,
                project_dir,
                manifest,
                bible=bible,
                seed=seed,
                max_retries=max_retries,
                retry_backoff_sec=retry_backoff_sec,
                on_progress=on_progress,
                keyframer=keyframer,
                segment=segment,
                previous=previous,
            )
            _emit(on_progress, manifest)

    await asyncio.gather(*(run_one(segment) for segment in todo))

    ready, failed = len(manifest.ready_clips), len(manifest.failed_clips)
    logger.info("视频片段生成结束 project=%s 成功=%d 失败=%d", project.id, ready, failed)
    return manifest


async def _generate_one(
    shot: Shot,
    project: Project,
    provider: VideoProvider,
    clips_dir: Path,
    project_dir: Path,
    manifest: VideoManifest,
    *,
    bible: StoryBible | None = None,
    seed: int = 0,
    max_retries: int,
    retry_backoff_sec: float,
    on_progress: ProgressCallback,
    keyframer: Keyframer | None = None,
    segment: Segment | None = None,
    previous: Path | None = None,
) -> None:
    clip = manifest.get(shot.id)
    if clip is None:  # pragma: no cover - 调用方保证已 upsert
        return

    output = clips_dir / f"{shot.id}.mp4"
    unit = segment or Segment(id=shot.id, scene_id=shot.scene_id, shots=[shot])
    references, shot_seed = resolve_references(shot, bible, project_dir, provider, seed)
    first_frame = None
    if unit.id in manifest.reference_waived:
        logger.warning("镜头 %s：用户已同意不带参考图 / 首帧生成", shot.id)
        references = []
    elif keyframer is not None:
        try:
            first_frame = await keyframer.first_frame(unit, provider, previous=previous)
        except KeyframeUnavailable as exc:
            _record_keyframe_failure(manifest, unit, exc)
            clip.status = ClipStatus.FAILED
            clip.error = str(exc)
            manifest.upsert(clip)
            return
    request = VideoRequest(
        shot_id=shot.id,
        scene_id=shot.scene_id,
        prompt=with_opening_frame_note(build_prompt(shot, project, bible), first_frame),
        still_prompt=(
            still_prompt(shot, project, bible) if getattr(provider, "draws_stills", False) else ""
        ),
        duration_sec=max(1, shot.duration_sec),
        aspect_ratio=project.aspect_ratio.value,
        output_path=output,
        reference_images=references,
        first_frame=first_frame,
        seed=shot_seed,
        resume_job_id=clip.pending_job_id,
    )

    def submitted(job_id: str) -> None:
        clip.pending_job_id = job_id
        manifest.upsert(clip)
        _emit(on_progress, manifest)

    request.on_submitted = submitted

    # 重新生成 = 重新计费：清掉上一条的金额与「已记账」标记
    clip.status = ClipStatus.GENERATING
    clip.cost_usd = 0.0
    clip.cost_recorded = False
    # 插图模式记真正发给图片模型的那句（历史版本 / 排查用）
    clip.prompt = request.still_prompt or request.prompt
    _emit(on_progress, manifest)

    attempts = max_retries + 1
    for attempt in range(1, attempts + 1):
        clip.attempts = attempt
        try:
            generated = await provider.generate(request)
        except asyncio.CancelledError:
            _cleanup_partial(output)
            clip.status = ClipStatus.PENDING
            clip.error = "任务被取消"
            manifest.upsert(clip)
            raise
        except Exception as exc:  # noqa: BLE001 - 单镜失败必须被隔离
            message = scrub_secrets(f"{type(exc).__name__}: {exc}")[:400]
            clip.error = message
            # 生成已经成功、只是产物下载失败时**绝不能重试** —— 重新提交等于再买一次
            retryable = bool(getattr(exc, "retryable", True))
            job_id = str(getattr(exc, "job_id", "") or "")
            if job_id:
                clip.task_id = job_id
            # 任务还在服务端（超时 / 查询失败 / 下载失败）：留着 job id，下次接着查；
            # 其余失败（服务端报失败、图被拒）不计费，下次重新提交
            clip.pending_job_id = (
                job_id if isinstance(exc, (VideoJobPending, VideoArtifactError)) else ""
            )
            if clip.pending_job_id:
                clip.error = _with_pending_hint(message)
            request.resume_job_id = ""
            _cleanup_partial(output)
            if isinstance(exc, VideoReferenceRejected):
                _record_rejection(manifest, unit, exc, request, project_dir)
            if attempt < attempts and retryable:
                logger.warning(
                    "镜头 %s 第 %d/%d 次生成失败，准备重试：%s", shot.id, attempt, attempts, message
                )
                manifest.upsert(clip)
                if retry_backoff_sec:
                    await asyncio.sleep(retry_backoff_sec * attempt)
                continue
            clip.status = ClipStatus.FAILED
            manifest.upsert(clip)
            if isinstance(exc, VideoReferenceRejected):
                pass  # _record_rejection 已经记过
            elif retryable:
                logger.error("镜头 %s 生成失败（已重试 %d 次）：%s", shot.id, max_retries, message)
            elif isinstance(exc, VideoJobPending):
                logger.warning(
                    "镜头 %s 的任务已提交但还没取回（job=%s），下次生成接着查：%s",
                    shot.id,
                    job_id or "?",
                    message,
                )
            else:
                logger.error(
                    "镜头 %s 生成成功但落盘失败，已停止重试（避免重复计费）：job=%s %s",
                    shot.id,
                    job_id or "?",
                    message,
                )
            return

        clip.status = ClipStatus.READY
        clip.path = _relative(generated.path, project_dir)
        clip.media_kind = generated.media_kind
        clip.duration_sec = generated.duration_sec or request.duration_sec
        clip.task_id = generated.task_id
        clip.pending_job_id = ""
        clip.cost_usd = round(generated.actual_cost_usd or 0.0, 6)
        clip.generated_at = datetime.now(UTC).isoformat()
        clip.error = ""
        manifest.upsert(clip)
        _clear_waiver(manifest, unit.id)
        return


#: 计划切点附近多远内的真实硬切会被采用（秒；硬切数与计划对不上时才用）
_SNAP_WINDOW_SEC = 1.0
#: 切出来的单镜片段最短多长（秒）
_MIN_PIECE_SEC = 0.3


async def _generate_segment(
    segment: Segment,
    project: Project,
    provider: VideoProvider,
    clips_dir: Path,
    project_dir: Path,
    manifest: VideoManifest,
    *,
    bible: StoryBible | None = None,
    seed: int = 0,
    max_retries: int,
    retry_backoff_sec: float,
    on_progress: ProgressCallback,
    settings: Settings | None = None,
    keyframer: Keyframer | None = None,
    previous: Path | None = None,
) -> None:
    """一段（多个镜头）一次生成，再按镜头切回单镜片段。

    计费口径：整段只买一次（扣费记在段内第一个镜头的片段上，其余为 0，账本不重复）。
    **整段已经生成成功、只是切分失败时绝不重买**：整段文件留在 `clips/segments/`，
    下次续跑直接重切。
    """
    settings = settings or get_settings()
    clips = [manifest.get(shot.id) for shot in segment.shots]
    if any(clip is None for clip in clips):  # pragma: no cover - 调用方保证已 upsert
        return
    output = clips_dir / "segments" / f"{segment.id}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    references, segment_seed = resolve_segment_references(
        segment.shots, bible, project_dir, provider, seed, settings=settings
    )
    first_frame = None
    if segment.id in manifest.reference_waived:
        logger.warning("段落 %s：用户已同意不带参考图 / 首帧生成", segment.id)
        references = []
    elif keyframer is not None:
        try:
            first_frame = await keyframer.first_frame(segment, provider, previous=previous)
        except KeyframeUnavailable as exc:
            _record_keyframe_failure(manifest, segment, exc)
            for clip in clips:
                clip.status = ClipStatus.FAILED
                clip.error = str(exc)
                manifest.upsert(clip)
            return
    request = VideoRequest(
        shot_id=segment.id,
        scene_id=segment.scene_id,
        prompt=with_opening_frame_note(
            build_segment_prompt(segment.shots, project, bible, settings=settings),
            first_frame,
            settings=settings,
        ),
        duration_sec=max(1, segment.duration_sec),
        aspect_ratio=project.aspect_ratio.value,
        output_path=output,
        reference_images=references,
        first_frame=first_frame,
        seed=segment_seed,
        resume_job_id=clips[0].pending_job_id,
    )

    def update(**fields: object) -> None:
        for clip in clips:
            for key, value in fields.items():
                setattr(clip, key, value)
            manifest.upsert(clip)

    def submitted(job_id: str) -> None:
        update(pending_job_id=job_id)
        _emit(on_progress, manifest)

    request.on_submitted = submitted

    # 上一轮整段已经买到、只是没切成功：直接重切，不重新提交
    reuse = clips[0].segment_path and (Path(project_dir) / clips[0].segment_path).is_file()
    generated = None
    if not reuse:
        update(
            status=ClipStatus.GENERATING,
            cost_usd=0.0,
            cost_recorded=False,
            prompt=request.prompt,
            segment_id=segment.id,
            segment_path="",
        )
        _emit(on_progress, manifest)
        attempts = max_retries + 1
        for attempt in range(1, attempts + 1):
            update(attempts=attempt)
            try:
                generated = await provider.generate(request)
                break
            except asyncio.CancelledError:
                _cleanup_partial(output)
                update(status=ClipStatus.PENDING, error="任务被取消")
                raise
            except Exception as exc:  # noqa: BLE001 - 单段失败必须被隔离
                message = scrub_secrets(f"{type(exc).__name__}: {exc}")[:400]
                retryable = bool(getattr(exc, "retryable", True))
                job_id = str(getattr(exc, "job_id", "") or "")
                pending = (
                    job_id if isinstance(exc, (VideoJobPending, VideoArtifactError)) else ""
                )
                update(
                    error=_with_pending_hint(message) if pending else message,
                    pending_job_id=pending,
                    **({"task_id": job_id} if job_id else {}),
                )
                request.resume_job_id = ""
                _cleanup_partial(output)
                if isinstance(exc, VideoReferenceRejected):
                    _record_rejection(manifest, segment, exc, request, project_dir)
                if attempt < attempts and retryable:
                    logger.warning(
                        "段落 %s 第 %d/%d 次生成失败，准备重试：%s",
                        segment.id,
                        attempt,
                        attempts,
                        message,
                    )
                    if retry_backoff_sec:
                        await asyncio.sleep(retry_backoff_sec * attempt)
                    continue
                update(status=ClipStatus.FAILED)
                logger.error("段落 %s 生成失败：%s", segment.id, message)
                return
        assert generated is not None
        # 先把「已经买到」记下来：后面切分失败也不会丢掉这笔账和这段视频
        clips[0].cost_usd = round(generated.actual_cost_usd or 0.0, 6)
        update(
            task_id=generated.task_id,
            pending_job_id="",
            segment_path=_relative(generated.path, project_dir),
            media_kind=generated.media_kind,
            generated_at=datetime.now(UTC).isoformat(),
        )
        _clear_waiver(manifest, segment.id)

    source = Path(project_dir) / clips[0].segment_path
    if clips[0].media_kind == MediaKind.PLACEHOLDER or not ffmpeg_available():
        # 占位文件切不了（mock 缺 ffmpeg）：每个镜头都指向整段占位文件，合成会照常拒绝
        update(
            status=ClipStatus.READY,
            path=clips[0].segment_path,
            media_kind=MediaKind.PLACEHOLDER,
            error="" if clips[0].media_kind == MediaKind.PLACEHOLDER else "没有 ffmpeg，无法切分",
        )
        return
    try:
        pieces = await split_segment(
            source,
            [max(1, int(shot.duration_sec)) for shot in segment.shots],
            snap=settings.video_segment_snap_cuts,
        )
        for shot, clip, (offset, length) in zip(segment.shots, clips, pieces, strict=True):
            target = clips_dir / f"{shot.id}.mp4"
            await cut_clip(source, target, start=offset, duration=length)
            clip.path = _relative(target, project_dir)
            clip.segment_offset_sec = round(offset, 3)
            clip.duration_sec = max(1, round(length))
    except Exception as exc:  # noqa: BLE001 - 切分失败只影响这一段，且不重买
        message = scrub_secrets(f"{type(exc).__name__}: {exc}")[:300]
        update(
            status=ClipStatus.FAILED,
            error=f"整段已生成（{clips[0].segment_path}），切分失败：{message}；重试会直接重切，不重新计费",
        )
        logger.error("段落 %s 切分失败（整段已保留）：%s", segment.id, message)
        return
    update(status=ClipStatus.READY, error="")
    logger.info("段落 %s 生成并切分为 %d 个镜头", segment.id, len(segment.shots))


async def split_segment(
    source: Path, durations: list[int], *, snap: bool = True
) -> list[tuple[float, float]]:
    """算出每个镜头在整段里的 (起点, 时长)。

    计划切点 = 镜头时长累加；模型实际出片比计划短时按比例缩（比计划长的尾巴是档位多买的，
    直接丢掉）。`snap=True` 时把计划切点换成模型真实的硬切 —— 模型很少严格按时间戳切镜，
    按计划时间硬切会把上一镜的半秒画面带进下一镜：

    - 检测到的硬切数量**正好等于**计划切点数：按顺序一一对应，不管偏多远
      （Seedance 2.0 实测 12 刀全切了，但有 3 刀偏 1.0~1.4 秒，窗口吸附接不住）
    - 数量对不上（模型少切 / 多切 / 有闪光被误判）：退回「±1 秒内最近的硬切」
    """
    planned_total = float(sum(durations))
    actual = await probe_duration(source)
    total = min(planned_total, actual) if actual else planned_total
    scale = total / planned_total if planned_total else 1.0
    bounds: list[float] = []
    acc = 0.0
    for value in durations[:-1]:
        acc += value * scale
        bounds.append(acc)
    if snap and bounds:
        cuts = await detect_cuts(source) or []
        bounds = _match_cuts(bounds, cuts, total)
    edges = [0.0, *bounds, total]
    return [
        (round(edges[i], 3), round(max(0.04, edges[i + 1] - edges[i]), 3))
        for i in range(len(durations))
    ]


async def resplit_segments(
    storyboard: Storyboard,
    manifest: VideoManifest,
    project_dir: Path,
    clips_dir: Path,
    *,
    snap: bool = True,
    dry_run: bool = False,
) -> list[dict]:
    """用已经买到的整段文件**重新切**单镜片段（不调 API、不花钱）。

    切点规则改了之后给老项目补救用。只动「整段文件还在」的段；返回每个镜头的新旧起点与长度。
    """
    order = {shot.id: index for index, shot in enumerate(storyboard.shots)}
    shots = {shot.id: shot for shot in storyboard.shots}
    groups: dict[str, list[VideoClip]] = {}
    for clip in manifest.clips:
        if clip.segment_id and clip.segment_path and clip.shot_id in shots:
            groups.setdefault(clip.segment_id, []).append(clip)
    rows: list[dict] = []
    for segment_id, clips in groups.items():
        clips.sort(key=lambda c: order[c.shot_id])
        source = Path(project_dir) / clips[0].segment_path
        if not source.is_file():
            continue
        pieces = await split_segment(
            source, [max(1, int(shots[c.shot_id].duration_sec)) for c in clips], snap=snap
        )
        for clip, (offset, length) in zip(clips, pieces, strict=True):
            rows.append(
                {
                    "segment_id": segment_id,
                    "shot_id": clip.shot_id,
                    "old_offset": clip.segment_offset_sec,
                    "new_offset": offset,
                    "length": length,
                }
            )
            if dry_run:
                continue
            target = Path(clips_dir) / f"{clip.shot_id}.mp4"
            await cut_clip(source, target, start=offset, duration=length)
            clip.path = _relative(target, project_dir)
            clip.segment_offset_sec = round(offset, 3)
            clip.duration_sec = max(1, round(length))
    return rows


def _match_cuts(planned: list[float], cuts: list[float], total: float) -> list[float]:
    """计划切点 → 实际用的切点（升序，相邻至少 `_MIN_PIECE_SEC`）。"""
    inner = [c for c in cuts if _MIN_PIECE_SEC <= c <= total - _MIN_PIECE_SEC]
    if len(inner) == len(planned) and all(
        b - a >= _MIN_PIECE_SEC for a, b in zip([0.0, *inner], [*inner, total], strict=True)
    ):
        return inner
    snapped: list[float] = []
    floor = 0.0
    for target in planned:
        near = [c for c in cuts if abs(c - target) <= _SNAP_WINDOW_SEC and c > floor + 0.3]
        choice = min(near, key=lambda c: abs(c - target)) if near else target
        choice = max(choice, floor + 0.3)
        snapped.append(choice)
        floor = choice
    return snapped


def resolve_segment_references(
    shots: list[Shot],
    bible: StoryBible | None,
    project_dir: Path,
    provider: VideoProvider,
    default_seed: int = 0,
    *,
    settings: Settings | None = None,
) -> tuple[list[Path], int | None]:
    """整段的参考图：段内所有角色在前（按出场顺序），再是场景、道具，合计不超上限。"""
    if not shots:
        return [], (default_seed or None)
    merged = shots[0].model_copy(
        update={
            "character_in_frame": _unique(c for s in shots for c in s.character_in_frame),
            "props_in_frame": _unique(p for s in shots for p in s.props_in_frame),
            "location": next((s.location for s in shots if s.location), ""),
        }
    )
    return resolve_references(
        merged, bible, project_dir, provider, default_seed, settings=settings
    )


def _unique(items) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def build_segment_prompt(
    shots: list[Shot],
    project: Project,
    bible: StoryBible | None = None,
    *,
    settings: Settings | None = None,
) -> str:
    """一段多镜头的 prompt：逐镜头写清时间戳、景别运镜、画面与台词，公共锚点只写一次。"""
    settings = settings or get_settings()
    total = sum(max(1, int(s.duration_sec)) for s in shots)
    lines = [
        f"A multi-shot sequence in one continuous video: {len(shots)} shots joined by hard cuts, "
        f"{total} seconds in total, aspect ratio {project.aspect_ratio.value}. "
        "Cut exactly at the listed timestamps; keep the same characters, wardrobe, setting and "
        "lighting across every shot"
    ]
    start = 0
    for index, shot in enumerate(shots, start=1):
        end = start + max(1, int(shot.duration_sec))
        base = _strip_prompt_label(shot.video_prompt or shot.visual_description or "")
        parts = [camera_directive(shot.camera), base or "cinematic shot"]
        mood = mood_directive(shot.mood)
        if mood:
            parts.append(f"emotion: {mood}")
        if settings.video_prompt_dialogue:
            spoken = dialogue_directive(shot, bible)
            if spoken:
                parts.append(spoken)
        narrated = narration_directive(shot, settings=settings, project=project)
        if narrated:
            parts.append(narrated)
        body = ", ".join(p for p in parts if p)
        lines.append(f"Shot {index} ({start}-{end}s): {body}")
        start = end
    merged = shots[0].model_copy(
        update={
            "character_in_frame": _unique(c for s in shots for c in s.character_in_frame),
            "props_in_frame": _unique(p for s in shots for p in s.props_in_frame),
            "location": next((s.location for s in shots if s.location), ""),
        }
    )
    joined = " ".join(lines)
    for anchor in (
        location_anchor(merged, bible, joined),
        prop_anchor(merged, bible, joined),
        appearance_anchor(merged, bible, joined, settings=settings),
    ):
        if anchor:
            lines.append(anchor)
    for directive in (
        settings.video_background_directive,
        _without_no_cut(settings.video_performance_directive),
        settings.video_style_prompt,
    ):
        text = " ".join(str(directive or "").split())
        if text:
            lines.append(text)
    return ". ".join(lines)


#: 单镜表演要求里「不许切」的说法：段落 prompt 要求按时间戳硬切，两者自相矛盾
_NO_CUT = re.compile(r"\b(no cuts?|without cuts?|continuous take|single take|one take)\b", re.I)


#: 首帧是空场景（`VIDEO_KEYFRAME_SUBJECT=scene`）时追加：第 0 帧里没有人，人物得自然入画
SCENE_OPENING_NOTE = (
    "Opening frame: the video starts exactly on the provided image, which shows the set with "
    "nobody in it. Bring the characters in naturally — they walk into frame, or the camera moves "
    "to reveal them — and never morph furniture or objects into people."
)


CHAIN_OPENING_NOTE = (
    "Opening frame: the video starts exactly on the provided image, which is the last frame of "
    "the previous shot. Continue seamlessly from it: same people, same faces, same outfits, "
    "same room."
)


def with_opening_frame_note(
    prompt: str, first_frame: Path | None, *, settings: Settings | None = None
) -> str:
    """首帧是接戏帧时说明「接着上一段」；只有场景、没有人时，告诉视频模型人物怎么出现
    （否则容易凭空长出来）。"""
    settings = settings or get_settings()
    if first_frame is not None and str(first_frame).endswith(CHAIN_FRAME_SUFFIX):
        return f"{prompt} {CHAIN_OPENING_NOTE}"
    if first_frame is None or (settings.video_keyframe_subject or "").strip().lower() != "scene":
        return prompt
    return f"{prompt} {SCENE_OPENING_NOTE}"


def _without_no_cut(directive: str) -> str:
    """去掉表演要求里禁止剪切的条目（逗号分隔），其余原样保留。"""
    items = [item for item in str(directive or "").split(",") if not _NO_CUT.search(item)]
    return ",".join(items)


def provider_switched(manifest: VideoManifest, provider: VideoProvider) -> bool:
    """旧清单是不是另一个 provider / 模型的产物（是就要全部重跑、也就全部重新计费）。

    预算门控与 preflight 都必须用这个判断，否则「换模型后全部重跑」会被按
    「没有待生成镜头」估成 $0，预算形同虚设。
    """
    if not manifest.clips:
        return False
    if manifest.provider and manifest.provider != provider.name:
        return True
    return bool(
        manifest.model and provider.model and manifest.model != provider.model
    )


def _ref_path(root: Path, project_dir: Path, relative: str, label: str) -> Path | None:
    """参考图的绝对路径；越出项目目录或文件不存在就返回 None（并告警）。"""
    candidate = (Path(project_dir) / relative).resolve()
    try:  # 参考图必须留在项目目录内
        candidate.relative_to(root)
    except ValueError:
        logger.warning("%s 的参考图越出项目目录，已忽略：%s", label, relative)
        return None
    if not candidate.is_file():
        logger.warning("%s 的参考图不存在，已忽略：%s", label, candidate)
        return None
    return candidate


def resolve_references(
    shot: Shot,
    bible: StoryBible | None,
    project_dir: Path,
    provider: VideoProvider,
    default_seed: int = 0,
    *,
    settings: Settings | None = None,
) -> tuple[list[Path], int | None]:
    """挑出这个镜头要用的参考图（可多张）与 seed（角色一致性）。

    规则：provider 支持参考图时，**画面里每个配了参考图且文件还在的角色各带一张** ——
    双人镜头必须同时锁住两个人；角色图永远排在最前（provider 只收一张时退回第一张）。
    剧集资产（`VIDEO_ASSET_REFERENCES`）：再按「场景 → 画面里的关键道具」补上配了图的资产，
    合计不超过 `VIDEO_MAX_REFERENCE_IMAGES` 张。
    seed 优先用第一个角色的，其次用全局 `VIDEO_SEED`。
    一张都没有就返回 `([], None)`，绝不因为缺参考图就跳过生成。
    """
    if not provider.supports_reference_image or not bible:
        return [], (default_seed or None)

    settings = settings or get_settings()
    root = Path(project_dir).resolve()
    paths: list[Path] = []
    # 角色自己的 seed 优先于全局 VIDEO_SEED（只在第一次遇到时取）
    char_seed: int | None = None
    for char_id in shot.character_in_frame or []:
        character = bible.get_character(char_id)
        if character is None or not character.reference_image:
            continue
        candidate = _ref_path(root, project_dir, character.reference_image, f"角色 {char_id}")
        if candidate is None:
            continue
        if candidate not in paths:
            paths.append(candidate)
        if char_seed is None and character.seed is not None:
            char_seed = character.seed

    if settings.video_asset_references:
        assets: list[tuple[str, str]] = []
        setting = bible.get_setting(shot.location) if shot.location else None
        if setting is not None and setting.reference_image:
            assets.append((setting.reference_image, f"场景 {setting.id}"))
        for prop_id in shot.props_in_frame or []:
            prop = bible.get_prop(prop_id)
            if prop is not None and prop.reference_image:
                assets.append((prop.reference_image, f"道具 {prop.id}"))
        for relative, label in assets:
            candidate = _ref_path(root, project_dir, relative, label)
            if candidate is not None and candidate not in paths:
                paths.append(candidate)

    limit = max(1, settings.video_max_reference_images)
    seed = char_seed if char_seed is not None else (default_seed or None)
    return paths[:limit], seed


def resolve_reference(
    shot: Shot,
    bible: StoryBible | None,
    project_dir: Path,
    provider: VideoProvider,
    default_seed: int = 0,
) -> tuple[Path | None, int | None]:
    """只取第一张参考图（老接口，给「只支持单图」的调用方用）。"""
    paths, seed = resolve_references(shot, bible, project_dir, provider, default_seed)
    return (paths[0] if paths else None), seed


#: 分镜里的 `camera` 是中文（"特写 · 固定"、"近景 · 推"）。**它原来根本没进 prompt** ——
#: 运镜信息 100% 丢失，模型只能自己猜，动作自然容易飘。
_SHOT_SIZE_EN = (
    ("大特写", "extreme close-up"),
    ("特写", "close-up"),
    ("中近景", "medium close-up"),
    ("近景", "medium close-up"),
    ("中景", "medium shot"),
    ("全景", "wide shot"),
    ("远景", "extreme wide shot"),
    ("过肩", "over-the-shoulder shot"),
)
_CAMERA_MOVE_EN = (
    ("固定", "static camera"),
    ("推", "slow dolly in"),
    ("拉", "slow dolly out"),
    ("摇", "slow pan"),
    ("移", "slow tracking move"),
    ("跟", "smooth follow"),
    ("升", "slow crane up"),
    ("降", "slow crane down"),
    ("手持", "subtle handheld camera"),
)


def camera_directive(camera: str) -> str:
    """把中文景别/运镜翻成英文镜头指令（找不到就返回空，不猜）。"""
    text = " ".join(str(camera or "").split())
    if not text:
        return ""
    if text.isascii():
        return text  # 英文项目的分镜本来就写英文镜头语言
    parts: list[str] = []
    for token, english in _SHOT_SIZE_EN:
        if token in text:
            parts.append(english)
            break
    for token, english in _CAMERA_MOVE_EN:
        if token in text:
            parts.append(english)
            break
    return ", ".join(parts)


#: 中文情绪词 → 英文。原来 `mood` 是中文，直接被拼进英文 prompt：
#: 模型基本读不懂（等于没给），而且图片模型的前车之鉴是**中文会被画进画面**。
_MOOD_EN = {
    "震惊": "shocked",
    "惊讶": "surprised",
    "失落": "crestfallen",
    "沮丧": "dejected",
    "嘲讽": "mocking",
    "讽刺": "sarcastic",
    "受伤": "hurt",
    "委屈": "wronged",
    "不甘": "resentful",
    "愤怒": "angry",
    "生气": "angry",
    "冷漠": "cold",
    "轻蔑": "contemptuous",
    "紧张": "tense",
    "焦虑": "anxious",
    "恐惧": "fearful",
    "悲伤": "sad",
    "痛苦": "painful",
    "喜悦": "joyful",
    "欣慰": "relieved",
    "坚定": "determined",
    "自信": "confident",
    "温柔": "gentle",
    "复杂": "complicated",
    "期待": "hopeful",
    "迷茫": "lost",
    "释然": "at peace",
    "得意": "smug",
    "羡慕": "envious",
    "疑惑": "puzzled",
}


def mood_directive(mood: str) -> str:
    """把中文情绪翻成英文；一个词都翻不出来就丢弃（**绝不把中文塞进英文 prompt**）。"""
    text = " ".join(str(mood or "").split())
    if not text:
        return ""
    if text.isascii():
        return text
    found: list[str] = []
    for chunk in re.split(r"[，,、/|;；\s]+", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        english = _MOOD_EN.get(chunk)
        if english is None:
            english = next((en for cn, en in _MOOD_EN.items() if cn in chunk), None)
        if english and english not in found:
            found.append(english)
    return ", ".join(found)


def build_prompt(
    shot: Shot,
    project: Project,
    bible: StoryBible | None = None,
    *,
    settings: Settings | None = None,
) -> str:
    """组装给视频模型的 prompt（video_prompt 已在分镜阶段转成纯英文）。

    给了 `bible` 就额外拼一段**在场角色的圣经外貌**：视频模型每个镜头都是独立生成的，
    分镜 LLM 有时把 `character.visual_prompt` 抄进 `video_prompt`、有时只写
    "a young woman" —— 抄了就跳过，没抄就补上，让同一个角色在每个镜头里的文字描述一致。
    """
    base = _strip_prompt_label(shot.video_prompt or shot.visual_description or "")
    if not base:
        base = "cinematic shot"
    settings = settings or get_settings()
    parts = [base]
    if not _mentions(base, project.aspect_ratio.value):
        parts.append(f"aspect ratio {project.aspect_ratio.value}")
    camera = camera_directive(shot.camera)
    if camera:
        parts.append(camera)
    mood = mood_directive(shot.mood)
    if mood:
        parts.append(f"emotion: {mood}")
    location = location_anchor(shot, bible, base)
    if location:
        parts.append(location)
    props = prop_anchor(shot, bible, base)
    if props:
        parts.append(props)
    anchor = appearance_anchor(shot, bible, base, settings=settings)
    if anchor:
        parts.append(anchor)
    if settings.video_prompt_dialogue:
        spoken = dialogue_directive(shot, bible)
        if spoken:
            parts.append(spoken)
    narrated = narration_directive(shot, settings=settings, project=project)
    if narrated:
        parts.append(narrated)
    # 背景丰富度：分镜只写 "in an office" 时模型会画空墙，这段是统一的最低要求
    background = " ".join(str(settings.video_background_directive or "").split())
    if background:
        parts.append(background)
    # 表演 + 连贯性 + 风格：真实样本里模型会把哭戏演成舞台剧、长 prompt 下画面变形，
    # 这段是每个镜头统一的最低要求（可在 .env 关掉/改写）
    directive = " ".join(str(settings.video_performance_directive or "").split())
    if directive:
        parts.append(directive)
    style = " ".join(str(settings.video_style_prompt or "").split())
    if style:
        parts.append(style)
    parts.append(f"{max(1, shot.duration_sec)} seconds")
    return ", ".join(parts)


#: 分镜 LLM 偶尔把「标签」也写进 video_prompt（真实样本：`English prompt: A confident…`）。
#: 这种前缀对视频模型毫无意义，还会挤占注意力，生成前统一剥掉。
_PROMPT_LABEL_RE = re.compile(
    r"^(?:english\s+prompt|video\s+prompt|visual\s+prompt|prompt|description"
    r"|英文提示词|英文描述|提示词|描述)\s*[:：]\s*",
    re.IGNORECASE,
)


def _strip_prompt_label(text: str) -> str:
    """剥掉 prompt 开头反复出现的标签前缀（最多三轮，防止 `Prompt: English prompt:`）。"""
    out = str(text or "").strip()
    for _ in range(3):
        stripped = _PROMPT_LABEL_RE.sub("", out, count=1).strip()
        if stripped == out:
            break
        out = stripped
    return out


#: 每个角色的外貌描述最多带这么多字符，避免 prompt 被圣经挤爆。
#: 以前是 180 且按字符硬截：《重回2000》的校服描述被截成「royal blue body with white s」，
#: 白肩、袖条、白 T 一次都没发出去，第 2 场起校服换了款式
_APPEARANCE_MAX_CHARS = 360

#: 超长时优先保留的特征：跨段认人靠的是「穿什么、头发脸、戴什么」。
#: 按类别写通用词表（不针对某部片子的某件衣服），命中任一类的分句优先保留
_DISTINCTIVE_WORDS = (
    # 穿着（动词 + 常见上下装 / 外套 / 鞋）
    "wear|wears|wearing|dressed|outfit|uniform|costume|suit|robe|gown|jacket|coat|shirt|"
    "t-shirt|blouse|vest|cardigan|sweater|sweatshirt|hoodie|tracksuit|dress|skirt|"
    "pants|trousers|jeans|shorts|shoes|boots|sneakers|sandals|"
    # 头发 / 脸上的固定特征
    "hair|hairstyle|ponytail|braid|braids|bun|bangs|fringe|bald|beard|mustache|moustache|"
    "stubble|scar|tattoo|freckles|mole|birthmark|"
    # 配饰 / 图案
    "glasses|spectacles|sunglasses|hat|cap|scarf|necktie|necklace|earrings|bracelet|"
    "headband|mask|stripes?|striped|plaid|checkered|polka"
)
_DISTINCTIVE = re.compile(rf"\b({_DISTINCTIVE_WORDS})\b", re.IGNORECASE)


def clip_clauses(parts: list[str], limit: int = _APPEARANCE_MAX_CHARS) -> str:
    """把描述片段拼回一句，超长时整段整段地丢（先丢不含服装 / 发型等特征的），绝不截在半个词上。"""
    parts = [p.strip() for p in parts if p and p.strip()]
    text = ", ".join(parts)
    if len(text) <= limit:
        return text
    ranked = sorted(range(len(parts)), key=lambda i: (not _DISTINCTIVE.search(parts[i]), i))
    keep: set[int] = set()
    used = 0
    for i in ranked:
        cost = len(parts[i]) + (2 if keep else 0)
        if used + cost <= limit:
            keep.add(i)
            used += cost
    if not keep:  # 单独一段就超长：在词边界截
        return parts[ranked[0]][:limit].rsplit(" ", 1)[0]
    return ", ".join(parts[i] for i in sorted(keep))


def _split_clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,.;]", text) if part.strip()]


_STILL_NO_TEXT = (
    "No text, no letters, no numbers, no labels, no captions, no watermark; "
    "objects carry no printed markings, no measurement scales, no brand names"
)
_STILL_RULES = (
    "One single image with one clear subject. No split panels, no collage, no film strip, "
    "no borders or frames, no storyboard layout"
)


def still_prompt(
    shot: Shot, project: Project, bible: StoryBible | None, *, settings: Settings | None = None
) -> str:
    """插图模式（科普 `visual_mode=still`）的图片 prompt：画面描述 + 场景 / 道具锚点 + 画风。

    不用 `build_prompt`：表演、运镜、「一镜到底」对一张图没有意义；关键帧试拍里
    「opening frame of a film shot」还把图画成了带胶片齿孔的多格画面。画面里一律不许有字
    （知识性的字留给叠加层），场景里自然出现的招牌照旧按原文保留。
    """
    from videogen.services.asset_service import english_only, text_rule

    settings = settings or get_settings()
    base = " ".join((shot.video_prompt or shot.visual_description or "").split())
    parts = [
        f"An illustration for a science explainer video, aspect ratio {project.aspect_ratio.value}",
        base,
        location_anchor(shot, bible, base),
        prop_anchor(shot, bible, base),
        f"style: {style}" if (style := explainer_service.illustration_prompt(project, settings))
        else "",
        _STILL_RULES,
    ]
    joined = ". ".join(part.strip().rstrip(".").strip() for part in parts if part.strip())
    body = english_only(joined, keep_signage=True)
    return f"{body}. {text_rule(body, default=_STILL_NO_TEXT)}."


def location_anchor(shot: Shot, bible: StoryBible | None, base: str = "") -> str:
    """这一镜的**背景锚点**：注入故事圣经里该场景的环境描述。

    背景不够丰富的根因是这一层从来没进过 prompt —— 分镜 LLM 只写一句
    "in an office"，模型就把背景画成一面空墙。这里把场景的视觉描述补上，
    顺带让同一场景的多个镜头背景保持一致。
    """
    if not bible:
        return ""
    loc_id = str(getattr(shot, "location", "") or "").strip()
    if not loc_id:
        return ""
    setting = next((s for s in bible.settings if s.id == loc_id), None)
    if setting is None:
        return ""
    look = " ".join(str(setting.visual_prompt or setting.description or "").split())
    if not look:
        return ""
    head = look.split(",")[0].strip().lower()
    if head and head in base.lower():
        return ""
    return f"setting: {clip_clauses(_split_clauses(look))}"


def prop_anchor(shot: Shot, bible: StoryBible | None, base: str = "") -> str:
    """画面里关键道具的外观锚点（同一件道具跨镜头长一个样）。prompt 里已经写了就跳过。"""
    if not bible or not shot.props_in_frame:
        return ""
    looks: list[str] = []
    for prop_id in shot.props_in_frame:
        prop = bible.get_prop(prop_id)
        if prop is None:
            continue
        look = " ".join(str(prop.visual_prompt or "").split())
        if not look:
            continue
        head = look.split(",")[0].strip().lower()
        if head and head in base.lower():
            continue
        looks.append(clip_clauses(_split_clauses(look)))
    return f"key props: {'; '.join(looks)}" if looks else ""


def speaker_voice(shot: Shot, bible: StoryBible | None) -> str:
    """说话人是谁（性别 + 年龄段，英文，如 "a middle-aged woman, about 45"）。

    给开着原生音频的视频模型选嗓子用。

    不写时模型自己挑：画外台词没有画面可参照，45 岁的女老师被配成了中年男声
    （2026-10-13《重回2000》Seedance 试拍）。只写性别年龄、不写名字（理由同下）。
    """
    if bible is None or not shot.dialogue_speaker:
        return ""
    character = bible.get_character(shot.dialogue_speaker)
    if character is None:
        return ""
    gender, _ = voice_catalog.infer_gender(character)
    age = character.age
    if gender is None:
        return ""
    if age is not None and age < 13:
        person = "girl" if gender == "female" else "boy"
        return f"a young {person}"
    if age is not None and age < 20:
        person = "girl" if gender == "female" else "boy"
        return f"a teenage {person}"
    person = "woman" if gender == "female" else "man"
    if age is None:
        return f"a {person}"
    if age < 35:
        return f"a young {person}"
    if age < 60:
        return f"a middle-aged {person}, about {age}"
    return f"an elderly {person}, about {age}"


def dialogue_directive(shot: Shot, bible: StoryBible | None = None) -> str:
    """把这一镜的台词交给视频模型，让它按同一句话演口型。

    不写台词时，模型会**自己编一段话**在说 —— 嘴按它编的话动，我们再叠 TTS，
    口型必然对不上。写上台词（并禁止字幕）后，原生表演与我们的配音才在同一句话上。

    **故意不写说话人的名字**：中文名（「林悦」）出现在 prompt 里有被画成画面文字的风险
    （图片 prompt 上真实发生过：中文名被当成海报标题）。对模型来说「画面里正在说话的那个人」
    已经足够 —— 这一镜通常就一个说话人，两人同框时模型按台词本身也能对上。
    """
    text = " ".join(str(shot.dialogue_text or "").split())
    if not text:
        return ""
    spoken = language.spoken_language_name(text)
    voice = speaker_voice(shot, bible)
    if shot.voice_offscreen:
        # 说话人在门外/电话里：让模型画「听见声音」，别把说话人硬塞进画面对口型
        return (
            f"an off-screen voice{f' ({voice})' if voice else ''} is heard saying this exact "
            f"{spoken} line: "
            f'"{text}" — the speaker is not visible, '
            "nobody on screen lip-syncs this line, no on-screen text or captions"
        )
    who = f"the on-screen speaker ({voice})" if voice else "the on-screen speaker"
    return (
        f'{who} says this exact {spoken} line: "{text}" — natural lip-sync '
        f"matched to the {spoken} speech, mouth movement and breathing follow the line, "
        "no on-screen text or captions"
    )


def narration_directive(
    shot: Shot, *, settings: Settings | None = None, project: Project | None = None
) -> str:
    """把这一镜的旁白交给开着原生音频的视频模型念（`VIDEO_PROMPT_NARRATION`）。

    旁白是画外音：必须写明「画面里没人说话、没人动嘴」，否则模型会让画面里的人对着旁白
    动嘴（和画外台词一个道理）。有台词的镜头不写旁白 —— 一个镜头只有一条声音（已知限制 3）。

    科普项目一律不写：写了之后 `native_narration_shots` 会把这些镜头判成「原声念过了」、
    跳过 TTS，每段换一个嗓子，「全片一个旁白声音」就做不到了。
    """
    settings = settings or get_settings()
    if explainer_service.is_explainer(project):
        return ""
    if not settings.video_prompt_narration or str(shot.dialogue_text or "").strip():
        return ""
    text = " ".join(str(shot.narration or "").split())
    if not text:
        return ""
    voice = " ".join(str(settings.video_narrator_voice or "").split()) or "a calm narrator"
    return (
        f"documentary voice-over: {voice} {_narration_marker(text)} — nobody on screen speaks "
        "or moves their lips, no on-screen text or captions"
    )


def _narration_marker(text: str) -> str:
    return (
        f"says this exact {language.spoken_language_name(text)} narration off-screen: "
        f'"{text}"'
    )


#: 改了会让已生成的画面不再对应分镜的字段（台词 / 旁白也算：视频 prompt 里有，原声里也念了）
_VIDEO_FIELDS = (
    "scene_id",
    "duration_sec",
    "camera",
    "visual_description",
    "character_in_frame",
    "mood",
    "video_prompt",
    "location",
    "props_in_frame",
    "narration",
    "dialogue_text",
    "dialogue_speaker",
    "voice_offscreen",
)


def shot_signature(shot: Shot) -> tuple:
    """镜头里影响画面的那部分（念法、LLM 文案标记这类不影响画面的字段不算）。"""
    return tuple(
        tuple(value) if isinstance(value := getattr(shot, name), list) else value
        for name in _VIDEO_FIELDS
    )


def storyboard_impact(
    old: Storyboard | None, new: Storyboard, manifest: VideoManifest | None
) -> dict:
    """新分镜相对旧分镜，哪些**已经生成过**的段要重做、哪些能保留。

    一段能保留 = 段 id 相同、段内镜头与顺序相同、每个镜头影响画面的字段都没变。
    编段是按场确定性计算的，所以只改一场里的一个镜头，别的场的段不会动。
    """
    old_shots = {shot.id: shot for shot in (old.shots if old else [])}
    old_segments = {seg.id: [s.id for s in seg.shots] for seg in segments_of(old)} if old else {}
    new_ids = {shot.id for shot in new.shots}

    def generated(shot_id: str) -> bool:
        clip = manifest.get(shot_id) if manifest else None
        return clip is not None and clip.status == ClipStatus.READY

    stale: list[dict] = []
    kept = 0
    for segment in segments_of(new):
        ids = [s.id for s in segment.shots]
        if not any(generated(i) for i in ids):
            continue  # 本来就没生成过，谈不上作废
        changed = old_segments.get(segment.id) != ids or any(
            i not in old_shots or shot_signature(old_shots[i]) != shot_signature(s)
            for i, s in zip(ids, segment.shots, strict=True)
        )
        if changed:
            stale.append(
                {
                    "segment_id": segment.id,
                    "shots": ids,
                    "seconds": sum(max(1, int(s.duration_sec)) for s in segment.shots),
                }
            )
        else:
            kept += 1
    removed = [i for i in old_shots if i not in new_ids and generated(i)]
    return {
        "stale_segments": stale,
        "stale_shots": [i for seg in stale for i in seg["shots"]],
        "stale_seconds": sum(seg["seconds"] for seg in stale),
        "kept_segments": kept,
        "removed_shots": removed,
    }


def invalidate_clips(
    manifest: VideoManifest,
    shot_ids: list[str],
    project_dir: Path,
    *,
    drop: bool = False,
    reason: str = "片段作废",
) -> None:
    """把这些镜头的片段作废（标回待生成）；`drop=True` 直接从清单里删掉（镜头已不存在）。

    旧片段文件不直接删，先挪进 `history/` 留一份（连同当时的 prompt / 扣费），
    新的生成失败 / 被拒 / 不如旧的时还找得回来。抽帧是缓存，照删。
    已经花掉的钱早就记过账，这里清掉记账标记，重新生成就是一笔新账。
    """
    project_dir = Path(project_dir)
    targets = set(shot_ids)
    _archive_clips(
        [c for c in manifest.clips if c.shot_id in targets],
        project_dir,
        reason,
        model=manifest.model,
    )
    for clip in list(manifest.clips):
        if clip.shot_id not in targets:
            continue
        for cached in (project_dir / "frames").glob(f"{clip.shot_id}.*"):
            cached.unlink(missing_ok=True)
        if drop:
            manifest.clips.remove(clip)
            continue
        if clip.pending_job_id:
            logger.warning(
                "镜头 %s 作废时还有没取回的任务 %s（服务端可能已扣费），不再接着查",
                clip.shot_id,
                clip.pending_job_id,
            )
        clip.status = ClipStatus.PENDING
        clip.error = ""
        clip.task_id = None
        clip.pending_job_id = ""
        clip.path = ""
        clip.segment_path = ""
        clip.prompt = ""
        clip.cost_usd = 0.0
        clip.cost_recorded = False


#: 历史版本里给每个片段记下的字段（够把它原样恢复回清单）
_ARCHIVED_CLIP_FIELDS = {
    "shot_id",
    "scene_id",
    "segment_id",
    "provider",
    "task_id",
    "cost_usd",
    "generated_at",
    "prompt",
    "duration_sec",
    "path",
    "segment_path",
    "segment_offset_sec",
    "media_kind",
    "aspect_ratio",
}


def _archive_clips(
    clips: list[VideoClip], project_dir: Path, reason: str, *, model: str = ""
) -> None:
    clips = [clip for clip in clips if clip.path or clip.segment_path]
    files = [rel for clip in clips for rel in (clip.path, clip.segment_path) if rel]
    if not files:
        return
    media_history.archive(
        project_dir,
        files,
        reason=reason,
        extra={
            "model": model,
            "clips": [
                clip.model_dump(mode="json", include=_ARCHIVED_CLIP_FIELDS)
                for clip in clips
                if clip.status == ClipStatus.READY
            ],
        },
    )


def restore_clips(
    manifest: VideoManifest,
    storyboard: Storyboard,
    entry: Path,
    meta: dict,
    project_dir: Path,
    *,
    shot_ids: list[str] | None = None,
) -> dict:
    """把一批历史版本里的片段拷回 `clips/`、写回清单（历史里那份留着，可以再恢复）。

    以**段**为单位：段是一次生成的、整段文件按段 id 命名，只恢复其中几个镜头会和
    现在的整段文件对不上。所以只恢复「现在分镜里这一段的每个镜头在这批里都有、
    而且当时也是同一段」的段，其余列进 `skipped`。给了 `shot_ids` 就只看这些镜头所在的段。
    被替换下来的当前片段照样先挪进历史版本。钱早就记过账，恢复不再记。
    """
    project_dir = Path(project_dir)
    old = _archived_clips(entry, meta)
    wanted = set(shot_ids or [])
    restored: list[Segment] = []
    skipped: list[dict] = []
    for segment in segments_of(storyboard):
        ids = segment.shot_ids
        if wanted and not wanted & set(ids):
            continue
        reason = _restore_blocker(segment, old, entry)
        if reason:
            skipped.append({"segment_id": segment.id, "shots": ids, "reason": reason})
        else:
            restored.append(segment)
    if not restored:
        return {"restored_segments": [], "restored_shots": [], "skipped": skipped}

    # 先把要拷回来的文件放到旁边（归档当前片段可能触发清理，把这批历史删掉）
    staged: dict[Path, Path] = {}  # 目标 → 临时文件（同一段的镜头共用一个整段文件）
    for segment in restored:
        for shot in segment.shots:
            clip = old[shot.id]
            for relative in {clip.path, clip.segment_path} - {""}:
                target = project_dir / relative
                if target in staged:
                    continue
                temp = target.with_name(target.name + ".restoring")
                temp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry / relative, temp)
                staged[target] = temp
    shots = [shot for segment in restored for shot in segment.shots]
    targets = {shot.id for shot in shots}
    current = [c for c in manifest.clips if c.shot_id in targets]
    for clip in current:
        if clip.pending_job_id:
            logger.warning(
                "镜头 %s 恢复历史版本时还有没取回的任务 %s（服务端可能已扣费），不再接着查",
                clip.shot_id,
                clip.pending_job_id,
            )
    _archive_clips(current, project_dir, f"恢复 {entry.name} 之前", model=manifest.model)
    for target, temp in staged.items():
        os.replace(temp, target)
    for shot in shots:
        clip = old[shot.id].model_copy(
            update={
                "scene_id": shot.scene_id,
                "status": ClipStatus.READY,
                "error": "",
                "pending_job_id": "",
                "cost_recorded": True,
            }
        )
        manifest.upsert(clip)
        for cached in (project_dir / "frames").glob(f"{shot.id}.*"):
            cached.unlink(missing_ok=True)
    restored_from = str(meta.get("model") or "")
    warnings = []
    if restored_from and manifest.model and restored_from != manifest.model:
        warnings.append(f"恢复的片段是 {restored_from} 生成的，其余片段是 {manifest.model}")
    logger.info(
        "从历史版本 %s 恢复 %d 段（%d 镜）", entry.name, len(restored), len(shots)
    )
    return {
        "restored_segments": [segment.id for segment in restored],
        "restored_shots": [shot.id for shot in shots],
        "skipped": skipped,
        "warnings": warnings,
    }


def _archived_clips(entry: Path, meta: dict) -> dict[str, VideoClip]:
    """这批历史里的片段：新版本在 history.json 的 `clips` 里；整批归档时用当时的清单。"""
    clips: dict[str, VideoClip] = {}
    manifest_file = entry / "video_manifest.json"
    if manifest_file.is_file():
        try:
            saved = VideoManifest.model_validate_json(manifest_file.read_text(encoding="utf-8"))
            for clip in saved.ready_clips:
                clips[clip.shot_id] = clip
        except ValueError:
            logger.warning("历史版本 %s 的视频清单读不了，只按 history.json 恢复", entry.name)
    for raw in meta.get("clips") or []:
        try:
            clip = VideoClip.model_validate(raw)
        except ValueError:
            continue
        # 老的 history.json 没记路径：按命名规则补上
        if not clip.path:
            clip.path = f"clips/{clip.shot_id}.mp4"
        if not clip.segment_path and clip.segment_id:
            guess = f"clips/segments/{clip.segment_id}.mp4"
            if (entry / guess).is_file():
                clip.segment_path = guess
        clips.setdefault(clip.shot_id, clip)
    return clips


def _restore_blocker(segment: Segment, old: dict[str, VideoClip], entry: Path) -> str:
    """这一段为什么不能从这批历史恢复（能恢复返回空串）。"""
    missing = [shot.id for shot in segment.shots if shot.id not in old]
    if missing:
        return f"这批历史里没有 {'、'.join(missing)} 的片段"
    for shot in segment.shots:
        clip = old[shot.id]
        if (clip.segment_id or "") != (shot.segment_id or ""):
            return "当时和现在的分段不一样（段内镜头换过），整段文件对不上"
        for relative in {clip.path, clip.segment_path} - {""}:
            if not _safe_relative(relative) or not (entry / relative).is_file():
                return f"历史里缺文件 {relative}"
    if segment.shots[0].segment_id:
        members = {sid for sid, clip in old.items() if clip.segment_id == segment.id}
        if members != set(segment.shot_ids):
            return "当时这一段的镜头和现在不一样，整段文件对不上"
    return ""


def _safe_relative(relative: str) -> bool:
    path = Path(relative)
    return not path.is_absolute() and ".." not in path.parts and path.parts[:1] == ("clips",)


def dub_mode_for(project: Project | None, settings: Settings | None = None) -> str:
    """这个项目的人声来源：项目字段优先，其次内容类型（科普默认 tts），最后 `AUDIO_DUB_MODE`。

    科普要「全片一个旁白声音」，视频原声每段一个嗓子，所以项目没明确选时不跟全局的 native。
    """
    settings = settings or get_settings()
    mode = (getattr(project, "dub_mode", "") or "").strip().lower()
    if mode in {"native", "tts"}:
        return mode
    if explainer_service.is_explainer(project):
        return "tts"
    return settings.dub_mode


def native_voice_shots(
    storyboard: Storyboard, manifest: VideoManifest | None, *, dub_mode: str
) -> set[str]:
    """哪些镜头直接用片段原声当人声。

    native 模式：所有有就绪视频片段的镜头（对白 / 旁白都是视频模型念的）；
    tts 模式：只有生成时把旁白交给视频模型念过的镜头（见 `native_narration_shots`）。
    """
    if dub_mode != "native":
        return native_narration_shots(storyboard, manifest)
    if manifest is None:
        return set()
    return {
        shot.id
        for shot in storyboard.shots
        if (clip := manifest.get(shot.id)) is not None
        and clip.status == ClipStatus.READY
        and clip.media_kind == MediaKind.VIDEO
    }


def native_narration_shots(storyboard: Storyboard, manifest: VideoManifest | None) -> set[str]:
    """哪些镜头的旁白已经由视频模型在原声里念过了（`VIDEO_PROMPT_NARRATION` 生成的片段）。

    判据是**片段生成时实际发出去的 prompt**，不是当前的开关：开关事后改了，
    已经生成好的片段里那句旁白也还在。这些镜头配音阶段不再用 TTS 念一遍
    （否则成片里是两层旁白），合成时按原音量用片段原声。
    """
    if manifest is None:
        return set()
    shots: set[str] = set()
    for shot in storyboard.shots:
        if str(shot.dialogue_text or "").strip():
            continue
        text = " ".join(str(shot.narration or "").split())
        clip = manifest.get(shot.id)
        if (
            text
            and clip is not None
            and clip.status == ClipStatus.READY
            and clip.media_kind == MediaKind.VIDEO
            and _narration_marker(text) in (clip.prompt or "")
        ):
            shots.add(shot.id)
    return shots


def appearance_anchor(
    shot: Shot,
    bible: StoryBible | None,
    base: str = "",
    *,
    settings: Settings | None = None,
) -> str:
    """在场角色的外貌锚点（跨镜头一致性用的文字版）。

    `base` 里已经出现过的描述会跳过（分镜 LLM 抄过就不重复）。
    另外会补一句「整个剧组按哪国人来」的人种锚点：没有参考图时，这是唯一能拦住
    「中文名角色长出欧美脸」的东西（有参考图时它也只是锦上添花）。
    """
    if not bible:
        return ""
    haystack = base.lower()
    chunks: list[str] = []
    for char_id in shot.character_in_frame or []:
        character = bible.get_character(char_id)
        if character is None:
            continue
        look = " ".join(str(character.visual_prompt or "").split())
        if not look:
            continue
        # 逐句比：分镜只抄了开头一句（「A Chinese male high school student」）时，后面的服装 / 发型
        # 仍要补上。以前只比第一句，命中就整条跳过 —— 圣经里后加的服装描述永远进不了视频 prompt
        missing = [part for part in _split_clauses(look) if part.lower() not in haystack]
        if not missing:
            continue
        # 这里**不写角色名**：中文名进 prompt 会被模型画成画面上的文字
        # （真实事故：中文名 + 风格变成海报标题，见 character_image_service 的说明）。
        # 名字对人类读者有用，对模型没用 —— 模型只需要「画面里这个人的长相」。
        text = clip_clauses(missing)
        chunks.append(text)
        haystack += " " + text.lower()
    clause = ""
    if chunks:
        clause = "same characters as before, consistent face and hairstyle — " + "; ".join(chunks)
    # 人种锚点与外貌描述无关：分镜 LLM 抄过外貌（上面跳过）时也要补。
    # 真实事故：《变形记》全员 west，但外貌已被抄进 prompt → 锚点跟着丢 → Kling 画成东亚脸
    settings = settings or get_settings()
    if settings.image_region_auto and shot.character_in_frame:
        guess = region.project_region(
            bible,
            override=settings.image_region_override,
            default=settings.image_region_default,
        )
        anchor = region.region_anchor(guess.region)
        if anchor and not region.region_mentioned(f"{base} {clause}"):
            clause = f"{clause}. cast look: {anchor}" if clause else f"cast look: {anchor}"
    return clause


def _mentions(text: str, aspect_ratio: str) -> bool:
    compact = text.replace(" ", "")
    return aspect_ratio in compact or aspect_ratio.replace(":", "x") in compact


def _record_rejection(
    manifest: VideoManifest,
    unit: Segment,
    exc: VideoReferenceRejected,
    request: VideoRequest,
    project_dir: Path,
) -> None:
    """把「这一段的图被拒」记进清单，等用户决定（同一段只留最新一条）。"""
    images = (
        [request.first_frame]
        if exc.kind == "first_frame" and request.first_frame
        else list(request.reference_images)
    )
    manifest.rejections = [r for r in manifest.rejections if r.unit_id != unit.id]
    manifest.rejections.append(
        ReferenceRejection(
            unit_id=unit.id,
            shot_ids=[shot.id for shot in unit.shots],
            kind=exc.kind,
            chained=exc.kind == "first_frame"
            and str(request.first_frame or "").endswith(CHAIN_FRAME_SUFFIX),
            images=[_relative(Path(image), project_dir) for image in images],
            code=exc.code,
            message=scrub_secrets(str(exc))[:400],
        )
    )
    logger.error(
        "段 %s 的%s被视频模型拒收（%s），生成中断，等用户决定",
        unit.id,
        "首帧" if exc.kind == "first_frame" else "参考图",
        exc.code or "无错误码",
    )


def _record_keyframe_failure(
    manifest: VideoManifest, unit: Segment, exc: KeyframeUnavailable
) -> None:
    """开场关键帧没画出来：和「图被拒」一样中断这一段，等用户选重画 / 不带图。"""
    manifest.rejections = [r for r in manifest.rejections if r.unit_id != unit.id]
    manifest.rejections.append(
        ReferenceRejection(
            unit_id=unit.id,
            shot_ids=[shot.id for shot in unit.shots],
            kind=KEYFRAME_FAILED,
            message=scrub_secrets(str(exc))[:400],
        )
    )


def _clear_waiver(manifest: VideoManifest, unit_id: str) -> None:
    """「不带图」只管这一次：生成成功后清掉，下次重做这一段仍会先带图。"""
    if unit_id in manifest.reference_waived:
        manifest.reference_waived = [u for u in manifest.reference_waived if u != unit_id]


def _chain_source(
    previous: Segment | None, manifest: VideoManifest, project_dir: Path
) -> Path | None:
    """上一段最后一个镜头的片段（就绪的真视频才算）；没有就返回 None，这一段照常画关键帧。"""
    if previous is None:
        return None
    clip = manifest.get(previous.shots[-1].id)
    if (
        clip is None
        or clip.status != ClipStatus.READY
        or clip.media_kind != MediaKind.VIDEO
        or not _clip_file_exists(project_dir, clip)
    ):
        logger.info("上一段 %s 没有就绪的片段，下一段不接戏、照常画开场关键帧", previous.id)
        return None
    return Path(project_dir) / clip.path


def _clip_file_exists(project_dir: Path, clip: VideoClip) -> bool:
    if not clip.path:
        return False
    return (project_dir / clip.path).exists()


def _relative(path: Path, project_dir: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(project_dir).resolve()))
    except ValueError:
        return str(path)


def _cleanup_partial(output: Path) -> None:
    """失败/取消时清掉半截文件，避免下次续跑误判为已完成。"""
    try:
        output.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 清理失败不应掩盖原始错误
        logger.debug("清理半截文件失败: %s", output)


def _emit(callback: ProgressCallback, manifest: VideoManifest) -> None:
    if callback is None:
        return
    try:
        callback(manifest)
    except Exception:
        logger.exception("进度回调执行失败")
