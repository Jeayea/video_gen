from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from videogen.config import Settings, get_settings
from videogen.models.bible import StoryBible
from videogen.models.llm_io import VoiceLineDraft, VoiceScriptResponse
from videogen.models.project import Project
from videogen.models.script import Scene, Script
from videogen.models.storyboard import Shot, Storyboard, VoiceDirection
from videogen.models.video import ClipStatus, VideoManifest
from videogen.providers.llm.base import (
    LLMMessage,
    LLMProvider,
    TokenUsage,
    image_message,
    text_message,
)
from videogen.services.cost_service import PriceTable
from videogen.utils import ffmpeg as ffmpeg_utils
from videogen.utils.redact import scrub_secrets
from videogen.utils.text import clean_spoken_text

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIO_SCRIPT_SYSTEM",
    "MODES",
    "VoiceScriptOutcome",
    "apply_voice_script",
    "coerce_delivery",
    "delivery_from_draft",
    "estimate_voice_script_usd",
    "extract_frames",
    "frame_data_url",
    "normalize_mode",
    "planned_frames",
    "script_targets",
]

#: 支持的文案模式：off 不调 LLM，其余会花 LLM 额度
MODES = ("off", "narration", "polish", "full", "perform")

#: 单张抽帧的最大字节数：太大既费 token 又可能被 provider 拒收
MAX_FRAME_BYTES = 1_500_000
#: 抽帧在片段上的采样位置（占整段时长的比例）
_FRAME_RATIOS = {1: (0.5,), 2: (0.3, 0.7), 3: (0.15, 0.5, 0.85)}
#: 不知道片段时长时的兜底采样秒数
_FALLBACK_FRAME_SEC = (0.5, 1.5, 2.5)

#: 假 LLM / 测试靠这个锚点识别「配音文案」阶段，改动请同步 tests/fakes.py
AUDIO_SCRIPT_SYSTEM = """你是一位短剧配音导演，负责根据**真实画面**写或改配音文案。

你要读到的信息：故事圣经、本场剧本、本场分镜（镜头 id / 时长 / 景别 / 画面描述 /
已有台词或旁白），以及部分镜头的画面抽帧。

写作要求：
1. 只写画面与剧情支持得住的内容，不要凭空编造画面上没有的信息
2. 文案是要**念出来**的：口语、短句；不要书面语、不要括号里的动作/音效提示、
   不要引号书名号、不要"（此处停顿）"这类标注
3. 每个镜头的文案单独一行，`shot_id` 必须原样照抄给定的 id
4. 每个候选镜头都要出现在 lines 里：
   - 要配音 → `text` 写文案
   - 刻意留白（纯空镜、情绪停顿）→ `text` 写空字符串
   - 改写模式下 `text` 写空字符串表示「保留原文案，不用改」
5. 旁白要服务于剧情推进，一句话说清一个信息点；对白要符合该角色的人设与当前情绪
6. 单句不超过给定字数上限；宁短勿长，配音时长要和镜头时长匹配
7. 需要念法建议时给出 `rate` / `volume` / `pitch`（Edge 用，例如 "-10%"、"+5%"、"-20Hz"），
   不需要就给空字符串
8. **`instruction` 是给云端 TTS 的自然语言念法**（20-50 字，中文）：
   写清「情绪 + 语气 + 语速 + 停顿/气息」，例如「压抑的哭腔，语速慢，句尾发颤，像在忍着不哭」。
   不要写成抽象评价（「很好」「有感情」），要能被配音演员直接照做

只输出 JSON：{"lines": [{"shot_id": "...", "text": "...", "rate": "", "volume": "",
"pitch": "", "instruction": "", "note": "一句话说明为什么这么写"}]}"""

_MODE_TASK = {
    "narration": (
        "## 任务：补旁白\n"
        "下面列出的镜头**没有台词也没有旁白**。请根据画面与剧情给每个镜头写一句旁白，"
        "把剧情串起来。写不出信息量的纯空镜可以留空（text 为空字符串）。"
    ),
    "polish": (
        "## 任务：改写已有文案\n"
        "下面列出的镜头**已经有台词或旁白**。请让文案更口语、更贴合画面与情绪，"
        "保持原意与说话人不变。原文已经够好就留空（text 为空字符串）表示不用改。"
    ),
    "full": (
        "## 任务：完整配音文案\n"
        "下面列出的镜头里有缺文案的（补旁白），也有已有文案的（改得更口语更贴画面）。"
        "缺文案的镜头必须给文案；已有文案的镜头，原文够好可以留空表示不用改。"
        "同时为每条文案给出念法建议（rate / volume / pitch）与云端念法 instruction。"
    ),
    "perform": (
        "## 任务：只给念法（不改文案）\n"
        "下面列出的镜头都已有台词或旁白。**一个字都不要改**：`text` 必须一律填空字符串。"
        "你的工作是根据画面与情绪，为每个镜头写出该怎么说 —— "
        "`instruction` 写自然语言念法（情绪/语气/语速/停顿/气息，20-50 字），"
        "`rate`/`pitch` 可选（给 Edge 用），`note` 一句话说明依据。"
        "要读出角色的年龄、处境与当下情绪的差别，不要每个镜头都用同一种念法。"
    ),
}


@dataclass
class VoiceScriptOutcome:
    """一轮「LLM 写配音文案」的结果，供 manifest 与日志使用。"""

    mode: str = "off"
    model: str = ""
    frames: int = 0
    #: 真正被写/改的镜头 id（按处理顺序）
    applied: list[str] = field(default_factory=list)
    #: 被跳过的镜头数（已有同模式文案 / 本轮无需改）
    skipped: int = 0
    #: 模型漏掉的镜头 id
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def touched(self) -> int:
        return len(self.applied)


# ── 主流程 ────────────────────────────────────────────


async def apply_voice_script(
    *,
    llm: LLMProvider,
    project: Project,
    storyboard: Storyboard,
    script: Script | None,
    bible: StoryBible | None,
    video: VideoManifest | None,
    project_dir: Path,
    frames_dir: Path,
    settings: Settings | None = None,
    mode: str = "",
    use_vision: bool | None = None,
    force: bool | None = None,
) -> VoiceScriptOutcome:
    """让 LLM 看着真实画面写/改配音文案，直接写回分镜（按场分次调用）。

    设计取舍：
    - **按场调用**而不是整片一次：一场的镜头有限，图不会堆太多，单场失败也只影响一场
    - 结果写回 `shot.narration / shot.dialogue_text` 并打上 `voice_script_source` 标记：
      同模式重跑直接跳过（不重复花额度），字幕也自动跟着新文案走
    - 任何失败都**不阻断配音**：记一条 warning 后用原文案继续，绝不因为文案阶段把整条流水线打死
    """
    settings = settings or get_settings()
    mode = normalize_mode(mode or settings.audio_script_mode)
    force = settings.audio_llm_force if force is None else force
    vision = settings.audio_llm_vision if use_vision is None else use_vision
    outcome = VoiceScriptOutcome(mode=mode, model=getattr(llm, "model", "") or "")

    if mode == "off":
        return outcome

    scenes = _scene_index(script)
    groups, skipped = script_targets(storyboard, mode, force=force)
    outcome.skipped = skipped
    if not groups:
        logger.info("配音文案（%s）：没有需要处理的镜头，跳过 LLM 调用", mode)
        outcome.warnings.append(f"配音文案（{mode}）：没有需要处理的镜头，未调用 LLM")
        return outcome

    clips = _clip_paths(video, project_dir)
    clips_total = len(storyboard.shots)

    for scene_id, shots in groups:
        scene = scenes.get(scene_id)
        frames, frame_note = await _collect_frames(
            shots,
            clips,
            frames_dir,
            enabled=vision,
            count=settings.audio_frame_count,
            width=settings.audio_frame_width,
        )
        if frame_note:
            outcome.warnings.append(frame_note)
        outcome.frames += len(frames)

        try:
            drafts = await _ask_model(
                llm,
                project=project,
                bible=bible,
                scene=scene,
                shots=shots,
                frames=frames,
                mode=mode,
                max_chars=settings.audio_llm_max_chars,
                scene_totals=(len(shots), clips_total),
            )
        except Exception as exc:  # noqa: BLE001 - 文案失败不能拖死配音
            message = scrub_secrets(f"{type(exc).__name__}: {exc}")[:300]
            logger.warning("配音文案（%s）场次 %s 调用失败：%s", mode, scene_id, message)
            outcome.warnings.append(f"配音文案场次 {scene_id} 生成失败（已用原文案）：{message}")
            continue

        applied, missing = _apply_drafts(storyboard, shots, drafts, mode, settings)
        outcome.applied.extend(applied)
        outcome.missing.extend(missing)
        if missing:
            outcome.warnings.append(
                f"配音文案场次 {scene_id}：模型漏了 {len(missing)} 个镜头"
                f"（{'、'.join(missing[:5])}{'…' if len(missing) > 5 else ''}）"
            )

    if outcome.applied:
        logger.info(
            "配音文案（%s）完成：%d 个镜头被写/改，抽帧 %d 张，模型 %s",
            mode,
            len(outcome.applied),
            outcome.frames,
            outcome.model or "?",
        )
    return outcome


def normalize_mode(mode: str) -> str:
    value = (mode or "off").strip().lower()
    return value if value in MODES else "off"


def _scene_index(script: Script | None) -> dict[str, Scene]:
    if not script:
        return {}
    return {scene.id: scene for scene in script.scenes}


def script_targets(
    storyboard: Storyboard, mode: str, *, force: bool
) -> tuple[list[tuple[str, list[Shot]]], int]:
    """挑出这一轮需要 LLM 处理的镜头，按场分组（保持分镜顺序）。

    返回 (分组, 因为已经写过同模式文案而跳过的镜头数) —— 跳过数要让用户看得见，
    否则「重跑没反应」会被当成 bug。
    """
    grouped: dict[str, list[Shot]] = {}
    skipped = 0
    for shot in storyboard.shots:
        if not force and shot.voice_script_source == f"llm:{mode}":
            skipped += 1  # 这一轮已经写过，不再花钱
            continue
        if not _needs_script(shot, mode, force=force):
            continue
        grouped.setdefault(shot.scene_id, []).append(shot)
    return list(grouped.items()), skipped


def _needs_script(shot: Shot, mode: str, *, force: bool = False) -> bool:
    has_dialogue = bool(clean_spoken_text(shot.dialogue_text))
    has_narration = bool(clean_spoken_text(shot.narration))
    if mode == "narration":
        if not has_dialogue and not has_narration:
            return True
        # 强制重跑：把之前由本模式补的旁白也重新写一遍
        return force and shot.voice_script_source == "llm:narration"
    if mode == "polish":
        return has_dialogue or has_narration
    if mode == "perform":
        # 已经有文案的镜头才需要念法；没文案的镜头留给 narration/full 模式
        return has_dialogue or has_narration
    return True  # full：全都要过一遍


# ── 抽帧 ──────────────────────────────────────────────


def _clip_paths(video: VideoManifest | None, project_dir: Path) -> dict[str, Path]:
    """可用的片段路径：只认真实视频且文件真的在（占位片段抽不出帧）。"""
    result: dict[str, Path] = {}
    if not video:
        return result
    for clip in video.clips:
        if clip.status != ClipStatus.READY or not clip.path:
            continue
        path = Path(project_dir) / clip.path
        if path.is_file() and path.stat().st_size > 0:
            result[clip.shot_id] = path
    return result


async def _collect_frames(
    shots: list[Shot],
    clips: dict[str, Path],
    frames_dir: Path,
    *,
    enabled: bool,
    count: int,
    width: int,
) -> tuple[list[tuple[str, str]], str]:
    """给这批镜头抽帧并转成 data URL。返回 (frames, 需要提示的 warning)。"""
    if not enabled:
        return [], ""
    if not ffmpeg_utils.ffmpeg_available():
        return [], "配音文案：本机没有 ffmpeg，无法抽帧，本次是纯文本生成（对不上画面）"
    missing_clip = False
    failed = False
    frames: list[tuple[str, str]] = []
    for shot in shots:
        clip = clips.get(shot.id)
        if clip is None:
            missing_clip = True
            continue
        images = await extract_frames(
            clip, frames_dir, shot_id=shot.id, count=count, width=width
        )
        if not images:
            failed = True
            continue
        for index, image in enumerate(images, start=1):
            url = frame_data_url(image)
            if not url:
                continue
            label = f"### 镜头 {shot.id} 的画面（{index}/{len(images)}）"
            frames.append((label, url))
    notes: list[str] = []
    if missing_clip:
        notes.append("配音文案：部分镜头没有可用的真实视频片段，这些镜头只能靠文字描述生成")
    if failed:
        notes.append("配音文案：部分片段抽帧失败，这些镜头只能靠文字描述生成")
    return frames, "；".join(notes)


async def extract_frames(
    clip: Path,
    out_dir: Path,
    *,
    shot_id: str,
    count: int = 1,
    width: int = 640,
    timeout_sec: float = 60.0,
) -> list[Path]:
    """从一个视频片段里抽 `count` 帧存成 jpg（已存在且非空则复用）。

    抽不出来（没有 ffmpeg / 文件不是真视频 / 命令失败）返回空列表 ——
    调用方必须能接受「没有画面」继续走纯文本。
    """
    count = min(3, max(1, int(count or 1)))
    ratios = _FRAME_RATIOS.get(count, _FRAME_RATIOS[1])
    duration = await ffmpeg_utils.probe_duration(clip)
    if duration and duration > 0:
        stamps = [max(0.0, min(duration - 0.05, duration * ratio)) for ratio in ratios]
    else:
        stamps = list(_FALLBACK_FRAME_SEC[:count])

    out_dir.mkdir(parents=True, exist_ok=True)
    images: list[Path] = []
    for index, stamp in enumerate(stamps, start=1):
        target = out_dir / f"{shot_id}_{index}.jpg"
        if target.is_file() and target.stat().st_size > 0:
            images.append(target)
            continue
        try:
            await ffmpeg_utils.run_ffmpeg(
                [
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{stamp:.2f}",
                    "-i",
                    str(clip),
                    "-frames:v",
                    "1",
                    "-an",
                    "-vf",
                    f"scale={width}:-2",
                    "-q:v",
                    "4",
                    "-y",
                    str(target),
                ],
                timeout_sec=timeout_sec,
            )
        except ffmpeg_utils.FFmpegError as exc:
            logger.debug("抽帧失败 shot=%s at=%.2fs: %s", shot_id, stamp, exc)
            target.unlink(missing_ok=True)
            continue
        if target.is_file() and target.stat().st_size > 0:
            images.append(target)
    return images


def frame_data_url(path: Path, *, max_bytes: int = MAX_FRAME_BYTES) -> str:
    """把抽帧文件转成 data URL；读不出来或太大就返回空串。"""
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return ""
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    return f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}"


# ── 提示词与调用 ──────────────────────────────────────


async def _ask_model(
    llm: LLMProvider,
    *,
    project: Project,
    bible: StoryBible | None,
    scene: Scene | None,
    shots: list[Shot],
    frames: list[tuple[str, str]],
    mode: str,
    max_chars: int,
    scene_totals: tuple[int, int],
) -> list[VoiceLineDraft]:
    header = _build_user_prompt(
        project=project,
        bible=bible,
        scene=scene,
        shots=shots,
        mode=mode,
        max_chars=max_chars,
        frames=len(frames),
        scene_totals=scene_totals,
    )
    user = image_message("user", header, frames) if frames else text_message("user", header)
    messages: list[LLMMessage] = [text_message("system", AUDIO_SCRIPT_SYSTEM), user]
    temperature = 0.7 if mode in {"narration", "full"} else 0.4
    response = await llm.chat_json(
        messages, schema=VoiceScriptResponse, temperature=temperature, max_tokens=4096
    )
    if isinstance(response, VoiceScriptResponse):
        return list(response.lines)
    if isinstance(response, dict):
        return list(VoiceScriptResponse.model_validate(response).lines)
    return []


def _build_user_prompt(
    *,
    project: Project,
    bible: StoryBible | None,
    scene: Scene | None,
    shots: list[Shot],
    mode: str,
    max_chars: int,
    frames: int,
    scene_totals: tuple[int, int],
) -> str:
    scene_count, clip_count = scene_totals
    lines: list[str] = [
        _MODE_TASK.get(mode, _MODE_TASK["full"]),
        "",
        "## 项目",
        f"标题：{project.title}",
        f"画面比例：{project.aspect_ratio.value}",
        f"目标时长：{project.target_duration_sec} 秒",
    ]
    if bible:
        lines.append("## 角色")
        for character in bible.characters:
            persona = character.description or character.visual_prompt
            lines.append(
                f"  - id={character.id} 姓名={character.name} "
                f"角色={character.role or '—'} 人设={persona}"
            )
    if scene is not None:
        lines += [
            "## 本场剧本",
            f"场次：{scene.id}",
            f"标题：{scene.heading}",
            f"摘要：{scene.summary}",
            f"情绪：{scene.emotion}",
        ]
        if scene.dialogue:
            lines.append("剧本对白：")
            lines.extend(f"  - {d.speaker}: {d.line}" for d in scene.dialogue)

    lines += ["", "## 本场分镜（需要处理的镜头）"]
    for shot in shots:
        existing = clean_spoken_text(shot.dialogue_text) or clean_spoken_text(shot.narration)
        speaker = (shot.dialogue_speaker or "").strip()
        kind = "对白" if clean_spoken_text(shot.dialogue_text) else "旁白" if existing else "无"
        in_frame = (
            f" 出镜角色：{'、'.join(shot.character_in_frame)}" if shot.character_in_frame else ""
        )
        lines += [
            f"### 镜头 {shot.id}（第 {shot.shot_no} 镜 · {shot.duration_sec} 秒"
            f" · {shot.camera or '—'}）",
            f"画面：{shot.visual_description or shot.video_prompt or '—'}",
            f"情绪：{shot.mood or '—'}" + in_frame,
            f"现有文案（{kind}"
            + (f"·说话人 {speaker}" if speaker else "")
            + f"）：{existing or '（无）'}",
        ]

    lines += [
        "",
        "## 输出要求",
        f"- 只处理上面这 {scene_count} 个镜头（全片共 {clip_count} 个镜头，其它镜头不用管）",
        f"- 单句不超过 {max_chars} 个字符（汉字按 1 个算）",
        f"- 本次收到 {frames} 张画面抽帧（可能为 0 = 只能靠文字）",
        '- 输出 JSON：{"lines": [{"shot_id": "...", "text": "...", "rate": "", "volume": "",'
        ' "pitch": "", "instruction": "", "note": ""}]}',
    ]
    return "\n".join(lines)


# ── 结果落盘（写回分镜）───────────────────────────────


def _apply_drafts(
    storyboard: Storyboard,
    shots: list[Shot],
    drafts: list[VoiceLineDraft],
    mode: str,
    settings: Settings,
) -> tuple[list[str], list[str]]:
    """把模型输出写回分镜，返回 (被改动的镜头 id, 模型漏掉的镜头 id)。"""
    by_id = {shot.id: shot for shot in shots}
    seen: set[str] = set()
    applied: list[str] = []

    for draft in drafts:
        shot_id = (draft.shot_id or "").strip()
        shot = by_id.get(shot_id)
        if shot is None:
            logger.debug("配音文案返回了未知镜头 id: %r（忽略）", shot_id)
            continue
        seen.add(shot_id)
        direction = delivery_from_draft(draft)
        if mode == "perform":
            # 只给念法：文案一个字都不动（避免「改文案」顺手把台词改坏）
            if direction is not None:
                shot.voice_direction = direction
                shot.voice_script_source = "llm:perform"
                applied.append(shot_id)
            continue
        text = clean_spoken_text(draft.text)
        if not text:
            continue  # 留白 / 不用改：保持原样
        text, truncated = _clamp_text(text, settings.audio_llm_max_chars)
        if truncated:
            logger.info("配音文案 %s 超长已截断为 %d 字", shot_id, settings.audio_llm_max_chars)
        original = clean_spoken_text(shot.dialogue_text) or clean_spoken_text(shot.narration)
        if original and not shot.voice_script_original:
            shot.voice_script_original = original
        if clean_spoken_text(shot.dialogue_text) or (shot.dialogue_speaker or "").strip():
            shot.dialogue_text = text
        else:
            shot.narration = text
        shot.voice_script_source = f"llm:{mode}"
        if direction is not None:
            shot.voice_direction = direction
        applied.append(shot_id)

    missing = [shot.id for shot in shots if shot.id not in seen]
    return applied, missing


def _clamp_text(text: str, max_chars: int) -> tuple[str, bool]:
    """超长文案按标点断句截断，实在没有标点就硬切。"""
    limit = max(8, int(max_chars or 60))
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in "。！？…；.!?;，,")
    if cut >= limit // 2:
        head = head[: cut + 1]
    return head.rstrip("，,、；; "), True


# ── 念法建议 ──────────────────────────────────────────


def coerce_delivery(value: str, *, kind: str = "rate", default: str = "") -> str:
    """把模型给的各种写法解析成 edge-tts 认的 `+N%` / `+NHz`。

    接受 `"+10%"`、`"-10%"`、`"10"`、`"慢 10%"`；解析不出来返回 `default`（空 = 用全局设置）。
    超出范围的会被夹到区间内 —— 免得配出一个听不清的怪声音。
    """
    raw = str(value or "").strip()
    if not raw:
        return default
    if kind == "pitch":
        low, high, unit = -50.0, 50.0, "Hz"
    elif kind == "volume":
        low, high, unit = -50.0, 50.0, "%"
    else:
        low, high, unit = -50.0, 100.0, "%"
    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw)
    if not match:
        return default
    try:
        number = float(match.group(0))
    except ValueError:
        return default
    number = min(high, max(low, number))
    rounded = round(number)
    sign = "+" if rounded >= 0 else "-"
    return f"{sign}{abs(rounded)}{unit}"


def delivery_from_draft(draft: VoiceLineDraft | None) -> VoiceDirection | None:
    """把模型的念法建议转成落盘用的 `VoiceDirection`（全是空就不落）。"""
    if draft is None:
        return None
    rate = coerce_delivery(draft.rate, kind="rate")
    volume = coerce_delivery(draft.volume, kind="volume")
    pitch = coerce_delivery(draft.pitch, kind="pitch")
    instruction = " ".join(str(draft.instruction or "").split())[:200]
    note = (draft.note or "").strip()[:200]
    if not (rate or volume or pitch or instruction or note):
        return None
    return VoiceDirection(
        rate=rate, volume=volume, pitch=pitch, instruction=instruction, note=note
    )


# ── 成本预估 ──────────────────────────────────────────


def estimate_voice_script_usd(
    shots: int,
    *,
    frames: int = 0,
    prices: PriceTable | None = None,
) -> float:
    """粗估「LLM 写配音文案」这一轮的输入/输出 token 成本。

    经验值：一张图 ≈ 280 token，一个镜头 ≈ 150 token 提示 + 60 token 输出，
    外加每场约 900 token 的固定提示。真实扣费以 provider 的 usage 为准。
    """
    prices = prices or PriceTable.from_settings()
    shots = max(0, int(shots))
    scenes = max(1, shots // 4 + (1 if shots % 4 else 0))
    input_tokens = frames * 280 + shots * 150 + scenes * 900
    output_tokens = shots * 60 + 40
    return prices.llm_usd(
        TokenUsage(calls=scenes, input_tokens=input_tokens, output_tokens=output_tokens)
    )


def planned_frames(shots: int, settings: Settings | None = None) -> int:
    """按设置估算会抽多少帧（预算门禁用）。"""
    settings = settings or get_settings()
    if not settings.audio_llm_vision:
        return 0
    return max(0, int(shots)) * settings.audio_frame_count
