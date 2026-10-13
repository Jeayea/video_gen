from __future__ import annotations

import asyncio
import logging
import math
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
from videogen.services import dub_guide_service, voice_catalog
from videogen.services.dub_guide_service import Guide
from videogen.utils.redact import scrub_secrets
from videogen.utils.text import clean_spoken_text

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[AudioManifest], None] | None

NARRATOR = "narrator"
#: 原声引导的配音允许越过镜头末尾这么多（探测余量 + 字尾气声），不为它拉长镜头
_GUIDED_SLACK_SEC = 0.15
DEFAULT_RETRY_BACKOFF_SEC = 1.5

#: 音色判定关键词：角色 role / 描述 / 名字里出现这些就判为男声。
#: 英文关键词必须按整词匹配 —— 否则 "woman" 里的 "man" 会把女主判成男声。


def plan_voices(
    storyboard: Storyboard,
    bible: StoryBible | None,
    settings: Settings | None = None,
    *,
    existing: dict[str, str] | None = None,
    target: str = "",
) -> voice_catalog.VoiceCasting:
    """给每个说话人选角（音色 + 为区分而复用的念法 + 说明）。

    `target` 是 TTS provider 名：云端引擎会把音色名换成云端音色表里的名字。
    """
    settings = settings or get_settings()
    return voice_catalog.cast_voice_map(
        storyboard,
        bible,
        settings,
        existing=existing,
        target=target or settings.tts_provider,
    )


#: 「旁白由视频原声直出」提示的前缀（重跑时按它去重）
_NATIVE_NOTE_PREFIX = "原声旁白："


def build_voice_map(
    storyboard: Storyboard,
    bible: StoryBible | None,
    settings: Settings | None = None,
    *,
    existing: dict[str, str] | None = None,
) -> dict[str, str]:
    """给每个说话人分配音色（只返回映射；完整选角结果用 `plan_voices`）。

    真正的规则在 `voice_catalog.cast_voice_map`：按**性别/定位/年龄**打分、
    保证不撞音色、撞了就靠念法区分。这里保留旧签名，方便老的调用方与测试。
    """
    return plan_voices(storyboard, bible, settings, existing=existing).voice_map


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
    guides: dict[str, Guide] | None = None,
    native_narration: set[str] | None = None,
    on_progress: ProgressCallback = None,
) -> AudioManifest:
    """逐镜生成配音，失败隔离 + 断点续跑（与视频阶段同构）。

    `guides`（原声引导，见 `dub_guide_service`）：有原声片段的镜头照着原声念、
    时长对齐原声、放在原声开口的位置。

    `native_narration`（见 `video_service.native_narration_shots`）：这些镜头的旁白
    视频模型已经在原声里念过了，不再用 TTS 念第二遍，合成时直接用片段原声。
    """
    guides = guides or {}
    native_narration = native_narration or set()
    settings = settings or get_settings()
    manifest = existing or AudioManifest()
    manifest.provider = provider.name
    manifest.concurrency = max(1, concurrency)
    casting = plan_voices(
        storyboard, bible, settings, existing=manifest.voice_map, target=provider.name
    )
    manifest.voice_map = casting.voice_map
    if casting.delivery:
        # 音色池用尽时靠念法区分：只补没有显式念法的角色，重跑不覆盖已有设置
        for speaker, direction in casting.delivery.items():
            manifest.voice_delivery.setdefault(speaker, direction)
    for note in casting.notes:
        if note not in manifest.warnings:
            manifest.warnings.append(note)
        logger.warning("配音选角：%s", note)
    for speaker, voice in (voice_overrides or {}).items():
        if voice:
            manifest.voice_map[speaker] = voice
    manifest.warnings = [
        w
        for w in manifest.warnings
        if not w.startswith(("跳过镜头", _NATIVE_NOTE_PREFIX, "分镜里没有任何台词"))
    ]

    lines = [
        line
        for line in _collect_lines(storyboard, bible, manifest)
        if not (line.kind == AudioKind.NARRATION and line.shot_id in native_narration)
    ]
    if native_narration:
        # 以前用 TTS 配过的旧条目作废：留着会和原声叠成两层旁白
        manifest.clips = [c for c in manifest.clips if c.shot_id not in native_narration]
        manifest.warnings.append(
            f"{_NATIVE_NOTE_PREFIX}{len(native_narration)} 个镜头的旁白由视频原声直出，不另配音"
        )
    if not lines:
        # 没有台词也是合法的：直接产出一条无声成片（或全靠原声），不报错
        if not native_narration:
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
            current.instruction or "",
        ) != (clip.rate or "", clip.volume or "", clip.pitch or "", clip.instruction or "")
        guide = guides.get(clip.shot_id)
        # 原声换了（视频重做 / 开关原声引导）也要重新合成：位置和时长都跟着原声走
        guide_changed = (
            current.guide_path,
            current.guide_start_sec,
            current.guide_end_sec,
        ) != _guide_key(guide, project_dir)
        if (
            current.status == ClipStatus.READY
            and not voice_changed
            and not text_changed
            and not delivery_changed
            and not guide_changed
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
        # 重新合成 = 重新计费：清掉上一条的记账标记（否则新花费进不了账本）
        current.cost_recorded = False
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
        current.instruction = clip.instruction
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
                guide=guides.get(clip.shot_id),
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
        # 念法优先级：镜头自己的（LLM 按画面给的）> 角色默认念法（选角时为区分音色）
        direction = shot.voice_direction or manifest.voice_delivery.get(
            (shot.dialogue_speaker or "").strip()
        ) or (manifest.voice_delivery.get(NARRATOR) if not shot.dialogue_text else None)
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
                    instruction=direction.instruction if direction else "",
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
                    instruction=direction.instruction if direction else "",
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
    guide: Guide | None = None,
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
        # 云端 TTS 用自然语言念法；没有逐条建议时用兜底（去播音腔）
        # 有原声参考时只让它「照着原声念」：念法里的快慢要求会和参考录音打架
        # （实测念成原声的 1.3~2.7 倍长，见 dub_guide_service.GUIDE_INSTRUCTION）
        instruction=dub_guide_service.GUIDE_INSTRUCTION
        if guide is not None
        else clip.instruction
        or (settings.tts_default_instruction if provider.name == "openrouter" else ""),
        reference_audio=guide.path if guide else None,
        target_sec=guide.span_sec if guide else 0.0,
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
        clip.guide_path, clip.guide_start_sec, clip.guide_end_sec = _guide_key(
            guide, project_dir
        )
        clip.offset_sec = 0.0
        if guide is not None and generated.media_kind == MediaKind.AUDIO:
            fitted, fitted_sec, factor = await dub_guide_service.fit_to_span(
                Path(generated.path),
                guide.span_sec,
                max_ratio=settings.audio_dub_fit_max_ratio,
            )
            clip.path = _relative(fitted, project_dir)
            clip.duration_sec = round(fitted_sec, 3)
            clip.offset_sec = guide.start_sec
            logger.info(
                "配音 %s 按原声对齐：放在 %.2fs，原声 %.2fs，配音 %.2fs（变速 %.2fx）",
                clip.id,
                guide.start_sec,
                guide.span_sec,
                fitted_sec,
                factor,
            )
        clip.voice = generated.voice or clip.voice
        clip.provider_usd = round(max(0.0, generated.usd), 6)
        clip.error = ""
        if generated.transcript:
            # 云端 TTS 会回读文本：模型自己改词/加解释时留痕，人工能查
            logger.debug("配音 %s 回读：%s", clip.id, generated.transcript[:80])
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
        if clip.guide_path:
            # 原声引导：配音落在原声说话的区间里（原声本来就在这个镜头之内），
            # 不再加余量 —— 否则 3.01 秒也会被取整成 4 秒，镜头尾巴多出一秒冻结帧
            required = max(0.0, clip.offset_sec) + clip.duration_sec - _GUIDED_SLACK_SEC
        else:
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


def _guide_key(guide: Guide | None, project_dir: Path) -> tuple[str, float, float]:
    if guide is None:
        return ("", 0.0, 0.0)
    return (_relative(guide.path, project_dir), guide.start_sec, guide.end_sec)


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
