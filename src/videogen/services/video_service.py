from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from videogen.models.bible import StoryBible
from videogen.models.project import Project
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import ClipStatus, MediaKind, VideoClip, VideoManifest
from videogen.providers.video.base import VideoProvider, VideoRequest
from videogen.utils.redact import scrub_secrets

logger = logging.getLogger(__name__)

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
) -> VideoManifest:
    """逐镜并发生成视频片段。

    - **并发**：`concurrency` 控制同时在跑的镜头数
    - **重试**：单镜失败重试 `max_retries` 次，仍失败只标记该镜头，不影响其他镜头
    - **续跑**：已有 manifest 里 `ready` 且文件仍在的镜头直接跳过（可在失败后重试）
    - **进度**：每完成一个镜头回调一次 `on_progress`，调用方负责落盘/更新任务
    """
    manifest = existing or VideoManifest()
    # 换了 provider / 模型（例如 mock → 真实模型）时，旧片段一律作废重跑，
    # 否则「跳过已就绪片段」的续跑逻辑会一直沿用占位视频，看起来像没生效。
    switched = _provider_switched(manifest, provider)
    if switched:
        logger.warning(
            "视频 provider/模型变更：%s/%s -> %s/%s，已就绪的 %d 个片段将重新生成",
            manifest.provider or "?",
            manifest.model or "?",
            provider.name,
            provider.model or "?",
            len(manifest.ready_clips),
        )
        for stale in manifest.clips:
            stale.status = ClipStatus.PENDING
            stale.error = ""
            stale.task_id = None
    manifest.provider = provider.name
    manifest.model = provider.model
    manifest.aspect_ratio = project.aspect_ratio.value
    manifest.concurrency = max(1, concurrency)

    shots = list(storyboard.shots)
    if not shots:
        raise ValueError("分镜为空，没有可生成的镜头")

    todo: list[Shot] = []
    for shot in shots:
        clip = manifest.get(shot.id) or VideoClip(shot_id=shot.id, scene_id=shot.scene_id)
        if clip.status == ClipStatus.READY and _clip_file_exists(project_dir, clip):
            continue  # 续跑：已经生成好的不重复烧额度
        clip.status = ClipStatus.PENDING
        clip.error = ""
        clip.media_kind = MediaKind.VIDEO
        clip.duration_sec = shot.duration_sec
        clip.aspect_ratio = project.aspect_ratio.value
        clip.provider = provider.name
        manifest.upsert(clip)
        todo.append(shot)

    _emit(on_progress, manifest)
    if not todo:
        logger.info("视频片段已全部就绪，无需生成（project=%s）", project.id)
        return manifest

    logger.info(
        "开始生成视频片段 project=%s 待生成 %d/%d 并发=%d 重试=%d",
        project.id,
        len(todo),
        len(shots),
        manifest.concurrency,
        max_retries,
    )

    semaphore = asyncio.Semaphore(manifest.concurrency)

    async def run_one(shot: Shot) -> None:
        async with semaphore:
            await _generate_one(
                shot,
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
            )
            _emit(on_progress, manifest)

    await asyncio.gather(*(run_one(shot) for shot in todo))

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
) -> None:
    clip = manifest.get(shot.id)
    if clip is None:  # pragma: no cover - 调用方保证已 upsert
        return

    output = clips_dir / f"{shot.id}.mp4"
    reference, shot_seed = resolve_reference(shot, bible, project_dir, provider, seed)
    request = VideoRequest(
        shot_id=shot.id,
        scene_id=shot.scene_id,
        prompt=build_prompt(shot, project),
        duration_sec=max(1, shot.duration_sec),
        aspect_ratio=project.aspect_ratio.value,
        output_path=output,
        reference_image=reference,
        seed=shot_seed,
    )

    clip.status = ClipStatus.GENERATING
    clip.prompt = request.prompt
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
            _cleanup_partial(output)
            if attempt < attempts:
                logger.warning(
                    "镜头 %s 第 %d/%d 次生成失败，准备重试：%s", shot.id, attempt, attempts, message
                )
                manifest.upsert(clip)
                if retry_backoff_sec:
                    await asyncio.sleep(retry_backoff_sec * attempt)
                continue
            clip.status = ClipStatus.FAILED
            manifest.upsert(clip)
            logger.error("镜头 %s 生成失败（已重试 %d 次）：%s", shot.id, max_retries, message)
            return

        clip.status = ClipStatus.READY
        clip.path = _relative(generated.path, project_dir)
        clip.media_kind = generated.media_kind
        clip.duration_sec = generated.duration_sec or request.duration_sec
        clip.task_id = generated.task_id
        clip.error = ""
        manifest.upsert(clip)
        return


def _provider_switched(manifest: VideoManifest, provider: VideoProvider) -> bool:
    """旧清单是不是另一个 provider / 模型的产物。"""
    if not manifest.clips:
        return False
    if manifest.provider and manifest.provider != provider.name:
        return True
    return bool(
        manifest.model and provider.model and manifest.model != provider.model
    )


def resolve_reference(
    shot: Shot,
    bible: StoryBible | None,
    project_dir: Path,
    provider: VideoProvider,
    default_seed: int = 0,
) -> tuple[Path | None, int | None]:
    """挑出这个镜头要用的参考图与 seed（角色一致性）。

    规则：provider 支持参考图时，取「画面里第一个配了参考图、且文件还在」的角色；
    seed 优先用角色自己的，其次用全局 `VIDEO_SEED`。没有就返回 `(None, None)`，
    绝不因为缺参考图就跳过生成。
    """
    if not provider.supports_reference_image or not bible:
        return None, (default_seed or None)

    for char_id in shot.character_in_frame or []:
        character = bible.get_character(char_id)
        if character is None or not character.reference_image:
            continue
        candidate = (Path(project_dir) / character.reference_image).resolve()
        try:  # 参考图必须留在项目目录内
            candidate.relative_to(Path(project_dir).resolve())
        except ValueError:
            logger.warning(
                "角色 %s 的参考图越出项目目录，已忽略：%s", char_id, character.reference_image
            )
            continue
        if candidate.is_file():
            seed = character.seed if character.seed is not None else (default_seed or None)
            return candidate, seed
        logger.warning("角色 %s 的参考图不存在，已忽略：%s", char_id, candidate)

    return None, (default_seed or None)


def build_prompt(shot: Shot, project: Project) -> str:
    """组装给视频模型的 prompt（video_prompt 已在分镜阶段转成纯英文）。"""
    base = (shot.video_prompt or shot.visual_description or "").strip()
    if not base:
        base = "cinematic shot"
    parts = [base]
    if not _mentions(base, project.aspect_ratio.value):
        parts.append(f"aspect ratio {project.aspect_ratio.value}")
    if shot.mood:
        parts.append(f"mood: {shot.mood}")
    parts.append(f"{max(1, shot.duration_sec)} seconds")
    return ", ".join(parts)


def _mentions(text: str, aspect_ratio: str) -> bool:
    compact = text.replace(" ", "")
    return aspect_ratio in compact or aspect_ratio.replace(":", "x") in compact


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
