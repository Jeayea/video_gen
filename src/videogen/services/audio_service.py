from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable
from pathlib import Path

from videogen.config import Settings, get_settings
from videogen.models.audio import (
    AudioClip,
    AudioKind,
    AudioManifest,
    BgmTrack,
    DurationAdjustment,
)
from videogen.models.bible import StoryBible
from videogen.models.project import Project
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import ClipStatus, MediaKind
from videogen.providers.bgm.base import BgmLibrary
from videogen.providers.tts.base import TTSProvider, TTSProviderError, TTSRequest
from videogen.utils.redact import scrub_secrets
from videogen.utils.text import clean_spoken_text

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[AudioManifest], None] | None

NARRATOR = "narrator"
DEFAULT_RETRY_BACKOFF_SEC = 1.5

#: 音色判定关键词：角色 role / 描述 / 名字里出现这些就判为男声。
#: 英文关键词必须按整词匹配 —— 否则 "woman" 里的 "man" 会把女主判成男声。
_MALE_HINTS = (
    "男",
    "父",
    "爸",
    "哥",
    "叔",
    "爷",
    "少年",
    "反派",
    "总裁",
    "老板",
    "丈夫",
    "兄",
    "male",
    "man",
    "boy",
    "father",
    "brother",
    "suit",
    "beard",
)
_FEMALE_HINTS = (
    "女",
    "母",
    "妈",
    "姐",
    "妹",
    "奶奶",
    "女主",
    "少女",
    "妻子",
    "夫人",
    "闺蜜",
    "female",
    "woman",
    "girl",
    "mother",
    "sister",
    "dress",
    "long hair",
)


def build_voice_map(
    storyboard: Storyboard,
    bible: StoryBible | None,
    settings: Settings | None = None,
    *,
    existing: dict[str, str] | None = None,
) -> dict[str, str]:
    """给每个说话人分配音色。

    优先级：已有分配（续跑保持一致）> 关键词判定男女声 > 按出场顺序在候选音色里轮转。
    结果会落盘到 `audio_manifest.voice_map`，重跑不会换人声音。
    """
    settings = settings or get_settings()
    mapping: dict[str, str] = dict(existing or {})

    speakers: list[str] = []
    for shot in storyboard.shots:
        speaker = _speaker_of(shot)
        if speaker and speaker not in speakers:
            speakers.append(speaker)

    if NARRATOR in speakers and NARRATOR not in mapping:
        mapping[NARRATOR] = settings.tts_voice_narrator

    characters = {c.id: c for c in (bible.characters if bible else [])}
    fallback = _fallback_voices(settings)

    index = 0
    for speaker in speakers:
        if mapping.get(speaker):
            continue
        character = characters.get(speaker)
        if speaker == NARRATOR:
            mapping[speaker] = settings.tts_voice_narrator
            continue
        voice, label = _guess_voice(character, settings)
        if voice is None:
            voice = fallback[index % len(fallback)]
            label = f"候选音色轮转（{index % len(fallback)}）"
        index += 1
        mapping[speaker] = voice
        logger.info("音色分配：%s(%s) -> %s（%s）", speaker, _name_of(character), voice, label)

    return mapping


def _fallback_voices(settings: Settings) -> list[str]:
    voices = [settings.tts_voice_female, settings.tts_voice_male, settings.tts_voice]
    seen: list[str] = []
    for voice in voices:
        if voice and voice not in seen:
            seen.append(voice)
    return seen or ["zh-CN-XiaoxiaoNeural"]


def _guess_voice(character, settings: Settings) -> tuple[str | None, str]:
    if character is None:
        return None, ""
    haystack = " ".join(
        str(part).lower()
        for part in (
            getattr(character, "role", ""),
            getattr(character, "description", ""),
            getattr(character, "visual_prompt", ""),
            getattr(character, "name", ""),
            " ".join(getattr(character, "personality", []) or []),
        )
    )
    if _hits(haystack, _MALE_HINTS):
        return settings.tts_voice_male, "关键词判定：男声"
    if _hits(haystack, _FEMALE_HINTS):
        return settings.tts_voice_female, "关键词判定：女声"
    return None, ""


def _hits(haystack: str, hints: tuple[str, ...]) -> bool:
    """中文关键词用包含判断，英文关键词按整词判断（woman ≠ man）。"""
    for hint in hints:
        if hint.isascii():
            if re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", haystack):
                return True
        elif hint in haystack:
            return True
    return False


def _speaker_of(shot: Shot) -> str:
    """一个镜头只产出一条配音：有对白就用对白，否则用旁白。"""
    if clean_spoken_text(shot.dialogue_text):
        return (shot.dialogue_speaker or "").strip()
    if clean_spoken_text(shot.narration):
        return NARRATOR
    return ""


def _name_of(character) -> str:
    return str(getattr(character, "name", "") or "")


async def synthesize_all(
    project: Project,
    storyboard: Storyboard,
    bible: StoryBible | None,
    provider: TTSProvider,
    audio_dir: Path,
    project_dir: Path,
    *,
    settings: Settings | None = None,
    concurrency: int = 3,
    max_retries: int = 2,
    retry_backoff_sec: float = DEFAULT_RETRY_BACKOFF_SEC,
    existing: AudioManifest | None = None,
    voice_overrides: dict[str, str] | None = None,
    on_progress: ProgressCallback = None,
) -> AudioManifest:
    """逐镜生成配音，失败隔离 + 断点续跑（与视频阶段同构）。"""
    settings = settings or get_settings()
    manifest = existing or AudioManifest()
    manifest.provider = provider.name
    manifest.concurrency = max(1, concurrency)
    manifest.voice_map = build_voice_map(storyboard, bible, settings, existing=manifest.voice_map)
    for speaker, voice in (voice_overrides or {}).items():
        if voice:
            manifest.voice_map[speaker] = voice
    manifest.warnings = [w for w in manifest.warnings if not w.startswith("跳过镜头")]

    lines = _collect_lines(storyboard, bible, manifest)
    if not lines:
        # 没有台词也是合法的：直接产出一条无声成片，不报错
        manifest.warnings.append("分镜里没有任何台词或旁白，本次没有配音")
        logger.info("分镜没有可配音内容（project=%s）", project.id)
        _emit(on_progress, manifest)
        return manifest

    todo: list[AudioClip] = []
    for clip in lines:
        current = manifest.get(clip.id) or clip
        desired_voice = manifest.voice_map.get(current.speaker, settings.tts_voice)
        voice_changed = bool(current.voice) and current.voice != desired_voice
        # 文案或念法变了也必须重新合成 —— 否则 LLM 改写过的台词会被旧音频盖住
        text_changed = bool(current.text) and clean_spoken_text(current.text) != clean_spoken_text(
            clip.text
        )
        delivery_changed = (
            current.rate or "",
            current.volume or "",
            current.pitch or "",
        ) != (clip.rate or "", clip.volume or "", clip.pitch or "")
        if (
            current.status == ClipStatus.READY
            and not voice_changed
            and not text_changed
            and not delivery_changed
            and _clip_file_exists(project_dir, current)
        ):
            continue  # 续跑：已经有音频、音色/文案都没变的不重复花额度
        if voice_changed:
            logger.info(
                "配音 %s 音色变更（%s -> %s），重新合成", current.id, current.voice, desired_voice
            )
        if text_changed:
            logger.info("配音 %s 文案变更，重新合成", current.id)
        current.status = ClipStatus.PENDING
        current.error = ""
        current.media_kind = MediaKind.AUDIO
        current.provider = provider.name
        current.voice = desired_voice
        current.kind = clip.kind
        current.speaker = clip.speaker
        current.speaker_name = clip.speaker_name
        current.scene_id = clip.scene_id
        current.text = clip.text
        current.rate = clip.rate
        current.volume = clip.volume
        current.pitch = clip.pitch
        current.note = clip.note
        current.script_source = clip.script_source
        manifest.upsert(current)
        todo.append(current)

    _emit(on_progress, manifest)
    if not todo:
        logger.info("配音已全部就绪，无需重新合成（project=%s）", project.id)
        return manifest

    logger.info(
        "开始生成配音 project=%s 待生成 %d/%d 并发=%d 音色=%d 个",
        project.id,
        len(todo),
        len(manifest.clips),
        manifest.concurrency,
        len(manifest.voice_map),
    )

    semaphore = asyncio.Semaphore(manifest.concurrency)

    async def run_one(clip: AudioClip) -> None:
        async with semaphore:
            await _synthesize_one(
                clip,
                provider,
                audio_dir,
                project_dir,
                manifest,
                settings,
                max_retries=max_retries,
                retry_backoff_sec=retry_backoff_sec,
                on_progress=on_progress,
            )
            _emit(on_progress, manifest)

    await asyncio.gather(*(run_one(clip) for clip in todo))

    ready, failed = len(manifest.ready_clips), len(manifest.failed_clips)
    logger.info("配音生成结束 project=%s 成功=%d 失败=%d", project.id, ready, failed)
    return manifest


def _collect_lines(
    storyboard: Storyboard, bible: StoryBible | None, manifest: AudioManifest
) -> list[AudioClip]:
    """把分镜里的对白/旁白摊平成配音任务；同一镜头对白优先于旁白。"""
    characters = {c.id: c for c in (bible.characters if bible else [])}
    lines: list[AudioClip] = []
    for shot in storyboard.shots:
        dialogue = clean_spoken_text(shot.dialogue_text)
        narration = clean_spoken_text(shot.narration)
        direction = shot.voice_direction
        if dialogue:
            if narration:
                manifest.warnings.append(
                    f"跳过镜头 {shot.id} 的旁白：该镜头已有对白（避免互相盖住）"
                )
            speaker = (shot.dialogue_speaker or "").strip()
            lines.append(
                AudioClip(
                    id=shot.id,
                    shot_id=shot.id,
                    scene_id=shot.scene_id,
                    kind=AudioKind.DIALOGUE,
                    speaker=speaker,
                    speaker_name=_name_of(characters.get(speaker)) if speaker else "",
                    text=dialogue,
                    rate=direction.rate if direction else "",
                    volume=direction.volume if direction else "",
                    pitch=direction.pitch if direction else "",
                    note=direction.note if direction else "",
                    script_source=shot.voice_script_source,
                )
            )
        elif narration:
            lines.append(
                AudioClip(
                    id=shot.id,
                    shot_id=shot.id,
                    scene_id=shot.scene_id,
                    kind=AudioKind.NARRATION,
                    speaker=NARRATOR,
                    speaker_name="旁白",
                    text=narration,
                    rate=direction.rate if direction else "",
                    volume=direction.volume if direction else "",
                    pitch=direction.pitch if direction else "",
                    note=direction.note if direction else "",
                    script_source=shot.voice_script_source,
                )
            )
    return lines


async def _synthesize_one(
    clip: AudioClip,
    provider: TTSProvider,
    audio_dir: Path,
    project_dir: Path,
    manifest: AudioManifest,
    settings: Settings,
    *,
    max_retries: int,
    retry_backoff_sec: float,
    on_progress: ProgressCallback,
) -> None:
    output = audio_dir / f"{clip.id}.mp3"
    request = TTSRequest(
        clip_id=clip.id,
        text=clip.text,
        voice=clip.voice or settings.tts_voice,
        # 逐条念法优先（LLM 按画面给的建议），否则用全局设置
        rate=clip.rate or settings.tts_rate,
        volume=clip.volume or settings.tts_volume,
        pitch=clip.pitch or settings.tts_pitch,
        output_path=output,
    )

    clip.status = ClipStatus.GENERATING
    clip.voice = request.voice
    _emit(on_progress, manifest)

    attempts = max_retries + 1
    for attempt in range(1, attempts + 1):
        clip.attempts = attempt
        try:
            generated = await provider.synthesize(request)
        except asyncio.CancelledError:
            _cleanup_partial(output)
            clip.status = ClipStatus.PENDING
            clip.error = "任务被取消"
            manifest.upsert(clip)
            raise
        except (TTSProviderError, Exception) as exc:  # noqa: BLE001 - 单条失败必须隔离
            message = scrub_secrets(f"{type(exc).__name__}: {exc}")[:400]
            clip.error = message
            _cleanup_partial(output)
            if attempt < attempts:
                logger.warning(
                    "配音 %s 第 %d/%d 次失败，准备重试：%s", clip.id, attempt, attempts, message
                )
                manifest.upsert(clip)
                if retry_backoff_sec:
                    await asyncio.sleep(retry_backoff_sec * attempt)
                continue
            clip.status = ClipStatus.FAILED
            manifest.upsert(clip)
            logger.error("配音 %s 失败（已重试 %d 次）：%s", clip.id, max_retries, message)
            return

        clip.status = ClipStatus.READY
        clip.path = _relative(generated.path, project_dir)
        clip.media_kind = generated.media_kind
        clip.duration_sec = round(generated.duration_sec, 3)
        clip.voice = generated.voice or clip.voice
        clip.error = ""
        if clip.duration_sec <= 0:
            clip.duration_sec = 0.0
            manifest.warnings.append(f"配音 {clip.id} 没拿到时长，音画对齐会跳过这一条")
        manifest.upsert(clip)
        return


# ── 音画对齐 ──────────────────────────────────────────


def align_storyboard(
    storyboard: Storyboard,
    manifest: AudioManifest,
    *,
    gap_sec: float = 0.25,
    max_ratio: float = 2.0,
) -> list[DurationAdjustment]:
    """按配音时长把镜头**拉长**（绝不缩短、绝不裁音频）。

    视频片段比镜头短时，合成阶段会冻结最后一帧补足 —— 不重新烧视频额度。
    """
    adjustments: list[DurationAdjustment] = []
    warnings: list[str] = []

    for shot in storyboard.shots:
        clip = manifest.for_shot(shot.id)
        if clip is None or clip.duration_sec <= 0:
            continue
        required = clip.duration_sec + max(0.0, gap_sec)
        target = max(shot.duration_sec, math.ceil(required))
        if target == shot.duration_sec:
            continue
        if shot.duration_sec > 0 and target / shot.duration_sec > max_ratio:
            warnings.append(
                f"镜头 {shot.id} 配音 {clip.duration_sec:.1f}s 远超原时长 "
                f"{shot.duration_sec}s（>{max_ratio:g}x），已按配音拉长"
            )
        adjustments.append(
            DurationAdjustment(
                shot_id=shot.id,
                scene_id=shot.scene_id,
                from_sec=shot.duration_sec,
                to_sec=target,
                audio_sec=clip.duration_sec,
            )
        )
        shot.duration_sec = target

    manifest.adjustments = adjustments
    for warning in warnings:
        if warning not in manifest.warnings:
            manifest.warnings.append(warning)
    storyboard.audio_aligned = True
    if adjustments:
        logger.info(
            "音画对齐完成：%d 个镜头被拉长（总时长 %d 秒）",
            len(adjustments),
            storyboard.total_duration_sec,
        )
    return adjustments


def pick_bgm(
    storyboard: Storyboard,
    library: BgmLibrary,
    *,
    preferred_id: str = "",
    fallback: bool = True,
) -> BgmTrack | None:
    """按分镜里出现最多的情绪挑一首 BGM。"""
    moods: list[str] = []
    for shot in storyboard.shots:
        mood = str(shot.mood or "").strip()
        if mood and mood not in moods:
            moods.append(mood)
    track = library.pick(moods, preferred_id=preferred_id, fallback=fallback)
    if track:
        logger.info(
            "BGM 选中：%s（匹配情绪 %s）", track.title or track.id, "、".join(moods) or "无"
        )
    else:
        logger.info("BGM 曲库为空或未匹配到曲目，本次不配乐")
    return track


# ── 工具 ──────────────────────────────────────────────


def _clip_file_exists(project_dir: Path, clip: AudioClip) -> bool:
    if not clip.path:
        return False
    return (project_dir / clip.path).exists()


def _relative(path: Path, project_dir: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(project_dir).resolve()))
    except ValueError:
        return str(path)


def _cleanup_partial(output: Path) -> None:
    for candidate in (output, output.with_suffix(".placeholder.json")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            logger.debug("清理半截配音失败: %s", candidate)


def _emit(callback: ProgressCallback, manifest: AudioManifest) -> None:
    if callback is None:
        return
    try:
        callback(manifest)
    except Exception:
        logger.exception("配音进度回调执行失败")
