from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from videogen.config import Settings, get_settings
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
        for stale in manifest.clips:
            stale.status = ClipStatus.PENDING
            stale.error = ""
            stale.task_id = None
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
    references, shot_seed = resolve_references(shot, bible, project_dir, provider, seed)
    request = VideoRequest(
        shot_id=shot.id,
        scene_id=shot.scene_id,
        prompt=build_prompt(shot, project, bible),
        duration_sec=max(1, shot.duration_sec),
        aspect_ratio=project.aspect_ratio.value,
        output_path=output,
        reference_images=references,
        seed=shot_seed,
    )

    # 重新生成 = 重新计费：清掉上一条的金额与「已记账」标记
    clip.status = ClipStatus.GENERATING
    clip.cost_usd = 0.0
    clip.cost_recorded = False
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
            # 生成已经成功、只是产物下载失败时**绝不能重试** —— 重新提交等于再买一次
            retryable = bool(getattr(exc, "retryable", True))
            job_id = str(getattr(exc, "job_id", "") or "")
            if job_id:
                clip.task_id = job_id
            _cleanup_partial(output)
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
            if retryable:
                logger.error("镜头 %s 生成失败（已重试 %d 次）：%s", shot.id, max_retries, message)
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
        clip.cost_usd = round(generated.actual_cost_usd or 0.0, 6)
        clip.generated_at = datetime.now(UTC).isoformat()
        clip.error = ""
        manifest.upsert(clip)
        return


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


def resolve_references(
    shot: Shot,
    bible: StoryBible | None,
    project_dir: Path,
    provider: VideoProvider,
    default_seed: int = 0,
) -> tuple[list[Path], int | None]:
    """挑出这个镜头要用的参考图（可多张）与 seed（角色一致性）。

    规则：provider 支持参考图时，**画面里每个配了参考图且文件还在的角色各带一张** ——
    双人镜头必须同时锁住两个人；去重后按 `character_in_frame` 顺序返回。
    seed 优先用第一个角色的，其次用全局 `VIDEO_SEED`。
    一张都没有就返回 `([], None)`，绝不因为缺参考图就跳过生成。
    """
    if not provider.supports_reference_image or not bible:
        return [], (default_seed or None)

    root = Path(project_dir).resolve()
    paths: list[Path] = []
    # 角色自己的 seed 优先于全局 VIDEO_SEED（只在第一次遇到时取）
    char_seed: int | None = None
    for char_id in shot.character_in_frame or []:
        character = bible.get_character(char_id)
        if character is None or not character.reference_image:
            continue
        candidate = (Path(project_dir) / character.reference_image).resolve()
        try:  # 参考图必须留在项目目录内
            candidate.relative_to(root)
        except ValueError:
            logger.warning(
                "角色 %s 的参考图越出项目目录，已忽略：%s", char_id, character.reference_image
            )
            continue
        if not candidate.is_file():
            logger.warning("角色 %s 的参考图不存在，已忽略：%s", char_id, candidate)
            continue
        if candidate not in paths:
            paths.append(candidate)
        if char_seed is None and character.seed is not None:
            char_seed = character.seed

    seed = char_seed if char_seed is not None else (default_seed or None)
    return paths, seed


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
    anchor = appearance_anchor(shot, bible, base)
    if anchor:
        parts.append(anchor)
    if settings.video_prompt_dialogue:
        spoken = dialogue_directive(shot, bible)
        if spoken:
            parts.append(spoken)
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


#: 每个角色的外貌描述最多带这么多字符，避免 prompt 被圣经挤爆
_APPEARANCE_MAX_CHARS = 180


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
    return f"setting: {look[:_APPEARANCE_MAX_CHARS]}"


def dialogue_directive(shot: Shot, bible: StoryBible | None = None) -> str:
    """把这一镜的台词交给视频模型，让它按同一句话演口型。

    不写台词时，模型会**自己编一段话**在说 —— 嘴按它编的话动，我们再叠 TTS，
    口型必然对不上。写上台词（并禁止字幕）后，原生表演与我们的配音才在同一句话上。
    """
    text = " ".join(str(shot.dialogue_text or "").split())
    if not text:
        return ""
    who = ""
    if bible is not None and shot.dialogue_speaker:
        character = bible.get_character(shot.dialogue_speaker)
        if character is not None:
            who = str(character.name or "")
    lead = (
        f"{who} speaks this exact Mandarin line"
        if who
        else "the character speaks this exact Mandarin line"
    )
    return (
        f'{lead}: "{text}" — natural lip-sync matched to the Mandarin speech, '
        "mouth movement and breathing follow the line, no on-screen text or captions"
    )


def appearance_anchor(shot: Shot, bible: StoryBible | None, base: str = "") -> str:
    """在场角色的外貌锚点（跨镜头一致性用的文字版）。

    `base` 里已经出现过的描述会跳过（分镜 LLM 抄过就不重复）。
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
        head = look.split(",")[0].strip().lower()
        if head and head in haystack:
            continue
        chunks.append(f"{character.name or char_id}: {look[:_APPEARANCE_MAX_CHARS]}")
        haystack += " " + head
    if not chunks:
        return ""
    return "same characters as before, consistent face and hairstyle — " + "; ".join(chunks)


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
