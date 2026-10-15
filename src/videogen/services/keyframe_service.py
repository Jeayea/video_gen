"""开场关键帧：给「只认首帧、不收参考图」的视频模型（Kling / Wan）锁角色。

H3 这类模型收 `input_references`，定妆照全程生效；Kling / Wan 只收一张首帧。
直接把定妆照当首帧，每段都会从一张「正面半身、干净背景」的证件照开场。所以先用
图片模型把「定妆照 + 场景图 + 这一段第一个镜头的画面」画成一张开场关键帧，再拿它当首帧：

    定妆照 / 场景图 ──┐
    第一个镜头的画面 ──┴─> 图片模型（图生图，$0.04/张）──> keyframes/<段>.png ──> 首帧

能保证的只有「开场这一帧是那张脸、那个房间」；段内硬切之后长相靠视频模型自己延续，
比不上全程参考图。

- 只在 `VIDEO_KEYFRAME=auto` 且模型是 `first_frame` 模式、第一个镜头有参考图时才画
- `VIDEO_KEYFRAME_SUBJECT=scene`：只画场景和道具、画面里没有人（Seedance 拒收写实人脸的首帧时用）。
  只锁景不锁人，人物长相靠文字；视频 prompt 会说明「开场是空场景，人物自然入画」
- 缓存：prompt + 图片模型 + 参考图内容不变就复用（重跑 / 续跑不重复花钱）
- 失败一律降级成纯文生视频（记 warning），绝不卡住视频生成
- 预估（preflight / 预算门控）与真正生成用同一个 `plan_jobs` 口径
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from videogen.config import Settings, get_settings
from videogen.models.bible import StoryBible
from videogen.models.project import Project
from videogen.models.storyboard import Shot
from videogen.providers.image import ImageProvider, get_image_provider
from videogen.providers.image.base import ImageProviderError, ImageRequest
from videogen.providers.video.base import VideoProvider
from videogen.services import cost_service, video_service
from videogen.services.asset_service import english_only, text_rule
from videogen.services.storyboard_service import Segment
from videogen.utils.file_store import FileStore
from videogen.utils.redact import scrub_secrets

logger = logging.getLogger(__name__)

__all__ = [
    "KEYFRAME_DIR",
    "KeyframeJob",
    "Keyframer",
    "enabled_for",
    "estimate",
    "forget",
    "keyframe_prompt",
    "plan_jobs",
]

KEYFRAME_DIR = "keyframes"

_STILL_RULES = (
    "Keep every person's face, hairstyle, body and outfit exactly as in the reference images; "
    "the room and set dressing follow the location reference image if one is given. "
    "Photorealistic cinematic film frame, natural lighting, one single frame. "
    "No split screen, no collage, no character sheet, no plain studio background."
)


@dataclass
class KeyframeJob:
    unit_id: str
    shot: Shot
    references: list[Path]
    prompt: str
    path: Path
    key: str
    cached: bool


async def enabled_for(provider: VideoProvider, settings: Settings | None = None) -> bool:
    """这个视频模型要不要画关键帧：只认首帧 + 配置没关。"""
    settings = settings or get_settings()
    if (settings.video_keyframe or "").strip().lower() == "off":
        return False
    try:
        return await provider.frame_mode() == "first_frame"
    except Exception:  # 问不到能力就按不画处理，不能卡住生成
        logger.warning("查询视频模型首帧能力失败，不画关键帧", exc_info=True)
        return False


_SCENE_RULES = (
    "The set is empty: no people, no faces, no hands, no human figures, no silhouettes, "
    "no mannequins, no portraits or photos of people. Keep the room and set dressing exactly "
    "as in the location reference image if one is given. Photorealistic cinematic film frame, "
    "natural lighting, one single frame. No split screen, no collage."
)

#: 关键帧里「不许有字」的默认说法；要画黑板 / 招牌上的原文时由 `text_rule` 换掉
_NO_TEXT = "No text, no captions, no labels, no watermark"


def scene_only(settings: Settings | None = None) -> bool:
    """开场关键帧只画场景、不画人（`VIDEO_KEYFRAME_SUBJECT=scene`）。"""
    settings = settings or get_settings()
    return (settings.video_keyframe_subject or "").strip().lower() == "scene"


def scene_keyframe_prompt(
    shot: Shot,
    project: Project,
    bible: StoryBible | None,
    *,
    settings: Settings | None = None,
) -> str:
    """只有场景和道具的开场帧：按镜头的景别 + 场景描述 + 道具外观画，画面里不许有人。

    不用分镜的 video_prompt：它的主语就是人（「A student lifts his head…」），删不干净。
    """
    parts = [f"A single still frame, the opening frame of a film shot, aspect ratio "
             f"{project.aspect_ratio.value}"]
    camera = video_service.camera_directive(shot.camera)
    if camera:
        parts.append(f"framing: {camera}")
    location = video_service.location_anchor(shot, bible)
    if location:
        parts.append(location)
    props = video_service.prop_anchor(shot, bible)
    if props:
        parts.append(props)
    parts.append(_SCENE_RULES)
    joined = ". ".join(part.strip().rstrip(".").strip() for part in parts)
    body = english_only(joined, keep_signage=True)
    return f"{body}. {text_rule(body, default=_NO_TEXT)}."


def _scene_references(shot: Shot, bible: StoryBible | None, project_dir: Path) -> list[Path]:
    """只要场景图 + 画面里的道具图（定妆照一张都不带）。"""
    if not bible:
        return []
    assets: list[tuple[str, str]] = []
    setting = bible.get_setting(shot.location) if shot.location else None
    if setting is not None and setting.reference_image:
        assets.append((setting.reference_image, f"场景 {setting.id}"))
    for prop_id in shot.props_in_frame or []:
        prop = bible.get_prop(prop_id)
        if prop is not None and prop.reference_image:
            assets.append((prop.reference_image, f"道具 {prop.id}"))
    root = Path(project_dir).resolve()
    paths: list[Path] = []
    for relative, label in assets:
        path = video_service._ref_path(root, project_dir, relative, label)
        if path is not None and path not in paths:
            paths.append(path)
    return paths


def keyframe_prompt(
    shot: Shot,
    project: Project,
    bible: StoryBible | None,
    *,
    settings: Settings | None = None,
) -> str:
    """第一个镜头的画面描述 → 一张静帧的 prompt（不要台词、表演、运镜，一个中文字符都不许有）。"""
    settings = settings or get_settings()
    still = settings.model_copy(
        update={"video_prompt_dialogue": False, "video_performance_directive": ""}
    )
    body = video_service.build_prompt(shot, project, bible, settings=still)
    aspect = project.aspect_ratio.value
    text = english_only(
        f"A single still frame, the opening frame of a film shot, aspect ratio {aspect}. "
        f"{body}. {_STILL_RULES}",
        keep_signage=True,
    )
    return f"{text}. {text_rule(text, default=_NO_TEXT)}."


def plan_jobs(
    segments: list[Segment],
    project: Project,
    bible: StoryBible | None,
    project_dir: Path,
    provider: VideoProvider,
    *,
    image_model: str = "",
    settings: Settings | None = None,
) -> list[KeyframeJob]:
    """每段一张：取段内第一个镜头**自己的**参考图（段里后面才出场的角色不进开场帧）。"""
    settings = settings or get_settings()
    jobs: list[KeyframeJob] = []
    out_dir = Path(project_dir) / KEYFRAME_DIR
    for segment in segments:
        if not segment.shots:
            continue
        first = segment.shots[0]
        if scene_only(settings):
            # 只画场景：没有场景图也照画（按文字），锁的是房间与陈设
            references = _scene_references(first, bible, project_dir)
            prompt = scene_keyframe_prompt(first, project, bible, settings=settings)
        else:
            references, _ = video_service.resolve_references(
                first, bible, project_dir, provider, settings=settings
            )
            if not references:
                continue  # 没有任何参考图：画关键帧没有可锁的东西，纯文生视频即可
            prompt = keyframe_prompt(first, project, bible, settings=settings)
        digest = hashlib.sha256()
        digest.update(f"{image_model}\n{prompt}\n{project.aspect_ratio.value}".encode())
        for ref in references:
            digest.update(hashlib.sha256(ref.read_bytes()).digest())
        key = digest.hexdigest()
        path = out_dir / f"{segment.id}.png"
        meta = out_dir / f"{segment.id}.json"
        cached = False
        if meta.is_file():
            try:
                saved = json.loads(meta.read_text(encoding="utf-8"))
                stored = out_dir / str(saved.get("file") or "")
                cached = saved.get("key") == key and stored.is_file()
                if cached:
                    path = stored
            except (OSError, ValueError):
                cached = False
        jobs.append(
            KeyframeJob(
                unit_id=segment.id,
                shot=first,
                references=references,
                prompt=prompt,
                path=path,
                key=key,
                cached=cached,
            )
        )
    return jobs


def forget(project_dir: Path, unit_id: str) -> None:
    """作废这一段的关键帧缓存（用户要求重画，例如视频模型拒收了这张首帧）。"""
    out_dir = Path(project_dir) / KEYFRAME_DIR
    meta = out_dir / f"{unit_id}.json"
    try:
        saved = json.loads(meta.read_text(encoding="utf-8"))
        stored = str(saved.get("file") or "")
    except (OSError, ValueError):
        stored = ""
    meta.unlink(missing_ok=True)
    if stored and Path(stored).name == stored:  # 只删 keyframes/ 目录里的那张
        (out_dir / stored).unlink(missing_ok=True)


async def estimate(
    segments: list[Segment],
    project: Project,
    bible: StoryBible | None,
    project_dir: Path,
    provider: VideoProvider,
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    """这次要新画几张关键帧、花多少（preflight 与预算门控共用）。"""
    settings = settings or get_settings()
    if not await enabled_for(provider, settings):
        return {"enabled": False, "to_generate": 0, "cached": 0, "estimated_usd": 0.0}
    jobs = plan_jobs(
        segments,
        project,
        bible,
        project_dir,
        provider,
        image_model=settings.openrouter_image_model,
        settings=settings,
    )
    todo = [job for job in jobs if not job.cached]
    return {
        "enabled": True,
        "to_generate": len(todo),
        "cached": len(jobs) - len(todo),
        "estimated_usd": cost_service.estimate_image_usd(len(todo)),
    }


class Keyframer:
    """视频生成过程中按段画关键帧（生成、缓存、记账都在这里，video_service 只拿首帧路径）。"""

    def __init__(
        self,
        store: FileStore,
        project_id: str,
        project: Project,
        bible: StoryBible | None,
        *,
        image_provider: ImageProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.project_id = project_id
        self.project = project
        self.bible = bible
        self.settings = settings or get_settings()
        self._provider = image_provider
        self._owns_provider = image_provider is None
        self._record_lock = asyncio.Lock()
        self.generated = 0
        self.spent_usd = 0.0

    def _image(self) -> ImageProvider:
        if self._provider is None:
            self._provider = get_image_provider()
        return self._provider

    async def first_frame(self, segment: Segment, provider: VideoProvider) -> Path | None:
        """这一段的开场关键帧；不需要时返回 None，需要但画不出来时抛 `KeyframeUnavailable`。"""
        project_dir = self.store.ensure_project_dir(self.project_id)
        image = self._image()
        jobs = plan_jobs(
            [segment],
            self.project,
            self.bible,
            project_dir,
            provider,
            image_model=image.model or self.settings.openrouter_image_model,
            settings=self.settings,
        )
        if not jobs:
            return None
        job = jobs[0]
        if job.cached:
            logger.info("关键帧复用缓存 project=%s 段=%s", self.project_id, job.unit_id)
            return job.path

        try:
            cost_service.check_budget(
                cost_service.load_report(self.store, self.project_id, self.settings),
                upcoming_usd=self.settings.image_price_usd,
                action=f"画开场关键帧（{job.unit_id}）",
            )
            job.path.parent.mkdir(parents=True, exist_ok=True)
            generated = await image.generate(
                ImageRequest(
                    subject_id=job.unit_id,
                    prompt=job.prompt,
                    output_path=job.path,
                    aspect_ratio=self.project.aspect_ratio.value,
                    resolution=self.settings.image_resolution,
                    reference_images=job.references,
                )
            )
        except (ImageProviderError, cost_service.BudgetExceeded) as exc:
            message = scrub_secrets(str(exc))[:200]
            logger.warning("段 %s 的开场关键帧没画出来，这一段先不生成：%s", job.unit_id, message)
            raise video_service.KeyframeUnavailable(f"开场关键帧没画出来：{message}") from exc

        saved = Path(generated.path)
        if not saved.is_file() or saved.stat().st_size == 0:
            logger.warning("关键帧没有产出有效文件，段 %s 先不生成", job.unit_id)
            raise video_service.KeyframeUnavailable("开场关键帧没有产出有效文件")
        entry = cost_service.image_entry(
            image.name,
            image.model or self.settings.image_provider,
            1,
            price_usd=self.settings.image_price_usd,
            actual_usd=generated.actual_cost_usd,
        )
        async with self._record_lock:
            cost_service.record(self.store, self.project_id, entry)
            # 账记上之后才写缓存标记：标记在、账没记的情况不会出现
            (saved.parent / f"{job.unit_id}.json").write_text(
                json.dumps({"key": job.key, "file": saved.name}, ensure_ascii=False),
                encoding="utf-8",
            )
        self.generated += 1
        self.spent_usd += entry.usd
        logger.info(
            "关键帧完成 project=%s 段=%s 参考图=%d 张 usd=%.4f",
            self.project_id,
            job.unit_id,
            len(job.references),
            entry.usd,
        )
        return saved

    async def aclose(self) -> None:
        if self._owns_provider and self._provider is not None:
            await self._provider.aclose()
