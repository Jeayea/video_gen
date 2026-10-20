"""背景音乐：本地曲库选曲 / 按分镜现写一首 / 接外部音乐模型。

三种引擎，解决的是三个不同的问题：

| 引擎 | 音乐哪来的 | 代价 | 适合 |
|---|---|---|---|
| `library` | `BGM_DIR` 里的现成曲子，按镜头情绪挑一首 | 需要你有版权音乐 | 已经有曲库 |
| `generate` | **本机按这条片子的分镜现写**（作曲 + 合成） | 本地计算，$0 | 想让音乐跟着画面走 |
| `cli` | 外部音乐模型（MusicGen / Stable Audio …） | 自己承担，可能吃 GPU | 想用真模型 |

`generate` 是这里的重点：段落边界对齐镜头、画面运动最猛的时刻给重音、
有人说话的地方去鼓并让旋律避开、混音时人声再压一层（闪避）。
曲库曲目做不到这些 —— 它只是一首按情绪挑出来的成品，整段循环。

失败**绝不阻断合成**：任何一步出问题都返回 `ok=False` 的 outcome + 一条 note，
调用方（流水线）记一条 warning 继续出片；只有用户显式点「生成配乐」时才会报错。
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import json
import logging
import math
import os
import shlex
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from videogen.config import Settings, get_settings
from videogen.models.audio import AudioManifest, BgmTrack
from videogen.models.music import MusicBrief, MusicPlan, MusicRequest
from videogen.models.project import Project
from videogen.models.storyboard import Storyboard
from videogen.models.video import VideoManifest
from videogen.providers.bgm.base import BgmLibrary
from videogen.providers.llm.base import LLMError, LLMMessage, LLMProvider, text_message
from videogen.utils import motion as motion_utils
from videogen.utils import music as music_utils
from videogen.utils.ffmpeg import FFmpegError, ffmpeg_available, run_ffmpeg

logger = logging.getLogger(__name__)

__all__ = [
    "MUSIC_ENGINES",
    "MusicOutcome",
    "command_for",
    "engine_problem",
    "music_prompt",
    "resolve_music",
]

#: 支持的引擎名
MUSIC_ENGINES = ("library", "generate", "cli", "auto", "none")

#: 混响/动态/响度处理这一趟 ffmpeg 的超时
POST_TIMEOUT_SEC = 300.0
#: 外部模型那一趟的超时上限（防止一个卡死的进程把任务挂住）
CLI_TIMEOUT_CAP_SEC = 7200.0
#: 生成的音频放在 `output/` 下的子目录
MUSIC_DIR = "music"
#: 目标响度（EBU R128）：比人声低一档，混音时再叠 BGM_GAIN_DB
TARGET_LUFS = -18.0
#: 外部模型命令必须带的占位符
REQUIRED_PLACEHOLDERS = ("{out}", "{prompt}")


@dataclass
class MusicOutcome:
    """一次配乐的结果。"""

    ok: bool = False
    engine: str = "none"
    track: BgmTrack | None = None
    plan: MusicPlan | None = None
    note: str = ""
    cached: bool = False
    elapsed_sec: float = 0.0
    #: 生成曲的绝对路径
    audio: Path | None = None
    #: 复核用的实测（峰值 / 响度 / 时长）
    measure: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "note": self.note,
            "cached": self.cached,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "audio": self.audio.name if self.audio else "",
            "track": self.track.model_dump() if self.track else None,
            "plan": self.plan.summary() if self.plan else None,
            "measure": self.measure,
        }


def _absolute(value: str | Path) -> Path:
    """相对路径按**当前工作目录**补成绝对路径，且不解析软链（与逐帧口型同一套理由）。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(os.path.abspath(path))


# ── 引擎可用性 ──────────────────────────────────────────


def engine_problem(settings: Settings, engine: str = "") -> str:
    """引擎不可用时返回原因（可用返回空串）—— 配置阶段就能提前发现。"""
    mode = (engine or settings.bgm_engine_mode).strip().lower()
    if mode in {"library", "none"}:
        return ""
    if mode == "cli":
        template = (settings.bgm_cli_cmd or "").strip()
        if not template:
            return "bgm_engine=cli 但没有配置 BGM_CLI_CMD（模板要含 {out} 与 {prompt}）"
        missing = [item for item in REQUIRED_PLACEHOLDERS if item not in template]
        if missing:
            return f"BGM_CLI_CMD 缺少占位符 {'、'.join(missing)}"
        return ""
    if not ffmpeg_available():
        return (
            "本机没有 ffmpeg：生成配乐需要它做混响与响度处理"
            "（brew install ffmpeg 或 pip install imageio-ffmpeg）"
        )
    return ""


def command_for(settings: Settings, *, out: Path, duration: float, prompt: str) -> list[str]:
    """拼出外部音乐模型的命令（`BGM_CLI_CMD` 模板）。"""
    template = (settings.bgm_cli_cmd or "").strip()
    if not template:
        raise ValueError("bgm_engine=cli 但没有配置 BGM_CLI_CMD")
    for placeholder in REQUIRED_PLACEHOLDERS:
        if placeholder not in template:
            raise ValueError(f"BGM_CLI_CMD 缺少占位符 {placeholder}")
    filled = (
        template.replace("{out}", str(_absolute(out)))
        .replace("{prompt}", prompt.replace('"', " "))
        .replace("{duration}", f"{duration:.2f}")
        .replace("{seconds}", f"{duration:.0f}")
    )
    command = shlex.split(filled)
    extra = (settings.bgm_cli_args or "").strip()
    if extra:
        command += shlex.split(extra)
    return command


def music_prompt(plan: MusicPlan) -> str:
    """给外部音乐模型（或人工）的英文提示词。

    只写「导出音乐时要什么」，不写中文 —— MusicGen / Stable Audio 这类模型对
    中文提示词基本无效。段落能量写成 `[0:00-0:07] calm` 这种时间轴，方便可控生成。
    """
    from videogen.utils.music import COLORS

    color = COLORS.get(plan.color)
    palette = {
        "pad": "warm sustained pads",
        "piano": "soft piano",
        "strings": "string ensemble",
        "bass": "deep bass",
        "lead": "sparse melodic lead",
        "perc": "percussion and drums",
        "bells": "bell accents",
    }
    instruments = ", ".join(palette.get(name, name) for name in plan.layers) or "pads"
    parts = [
        "cinematic instrumental background score, no vocals, no lyrics",
        f"mood: {color.label if color else plan.color}",
        f"{plan.bpm} bpm, key of {plan.key_name}",
        f"instrumentation: {instruments}",
        "low dynamic range, sits under dialogue, seamless loop-friendly ending",
    ]
    timeline = "; ".join(
        f"{_mmss(section.start_sec)}-{_mmss(section.end_sec)} "
        f"{section.label} (energy {section.energy}/3"
        f"{', dialogue under: keep it sparse' if section.dialogue else ''})"
        for section in plan.sections
    )
    if timeline:
        parts.append(f"arrangement timeline: {timeline}")
    return ", ".join(parts)


def _mmss(seconds: float) -> str:
    minutes, rest = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{rest:02d}"


# ── 入口 ──────────────────────────────────────────────


async def resolve_music(
    *,
    project: Project,
    storyboard: Storyboard,
    video_manifest: VideoManifest | None = None,
    audio_manifest: AudioManifest | None = None,
    output_dir: Path,
    frames_dir: Path | None = None,
    settings: Settings | None = None,
    library: BgmLibrary | None = None,
    request: MusicRequest | None = None,
    llm: LLMProvider | None = None,
    durations: dict[str, float] | None = None,
) -> MusicOutcome:
    """按引擎决定这次用什么音乐（选曲 / 现写 / 外部模型）。

    `durations` 是成片里每个镜头的最终时长（`compose_service.plan_timeline`）：给了就按它写谱，
    不在里面的镜头（没有可用片段）不进配乐；不给就按分镜秒数（老行为）。
    永远不抛异常：失败就是 `ok=False` + `note`，让调用方决定是告警还是报错。
    """
    settings = settings or get_settings()
    request = request or MusicRequest()
    started = time.time()
    mode = _resolve_engine(project, settings, request)

    if not settings.bgm_enabled and not request.engine:
        # 「关掉配乐」是一种正常配置，不是失败。
        # 注意这里**只看本次请求**显式指定的引擎：项目里存着的 music_engine 不能
        # 把 BGM_ENABLED=false 顶掉 —— 否则「本次不要 BGM」会偷偷继续配乐
        return MusicOutcome(ok=True, engine="none", note="配乐已在设置里关闭（BGM_ENABLED=false）")

    if mode == "none":
        return MusicOutcome(ok=True, engine="none", note="配乐已关闭")

    problem = engine_problem(settings, mode)
    if problem:
        return MusicOutcome(engine=mode, note=problem)

    # 曲库优先（auto：有歌就用歌，没歌就现写）
    if mode in {"library", "auto"}:
        track = _pick_library(storyboard, library, settings)
        if track is not None:
            outcome = MusicOutcome(
                ok=True,
                engine="library",
                track=track,
                note=(
                    f"曲库选曲：{track.title or track.id}"
                    f"（匹配情绪 {'、'.join(track.moods) or '无'}）"
                ),
            )
            outcome.audio = Path(track.path) if track.path else None
            outcome.elapsed_sec = round(time.time() - started, 2)
            return outcome
        if mode == "library":
            # 曲库为空不是错误：只是这次没有音乐，明确把上一次的曲目清掉
            outcome = MusicOutcome(
                ok=True,
                engine="library",
                note=f"BGM 曲库为空或没有可用曲目（目录 {settings.bgm_path}），本次不配乐",
            )
            outcome.elapsed_sec = round(time.time() - started, 2)
            return outcome

    if durations is not None:
        storyboard = _timed_storyboard(storyboard, durations)

    if mode == "cli":
        outcome = await _generate_cli(
            project=project,
            storyboard=storyboard,
            video_manifest=video_manifest,
            audio_manifest=audio_manifest,
            output_dir=output_dir,
            frames_dir=frames_dir,
            settings=settings,
            request=request,
        )
        outcome.elapsed_sec = round(time.time() - started, 2)
        return outcome

    outcome = await _generate_local(
        project=project,
        storyboard=storyboard,
        video_manifest=video_manifest,
        audio_manifest=audio_manifest,
        output_dir=output_dir,
        frames_dir=frames_dir,
        settings=settings,
        request=request,
        llm=llm,
    )
    outcome.elapsed_sec = round(time.time() - started, 2)
    return outcome


def _timed_storyboard(storyboard: Storyboard, durations: dict[str, float]) -> Storyboard:
    """换成成片里的真实时长（镜头时长在分镜里是整数秒，合成会改）。只用来写谱，不落盘。"""
    shots = [
        shot.model_copy(update={"duration_sec": durations[shot.id]})
        for shot in storyboard.shots
        if shot.id in durations
    ]
    return storyboard.model_copy(update={"shots": shots})


def _resolve_engine(project: Project, settings: Settings, request: MusicRequest) -> str:
    for candidate in (request.engine, project.music_engine, settings.bgm_engine_mode):
        value = (candidate or "").strip().lower()
        if value in MUSIC_ENGINES:
            return value
    return "library"


def _pick_library(
    storyboard: Storyboard, library: BgmLibrary | None, settings: Settings
) -> BgmTrack | None:
    """按分镜里出现过的情绪挑一首曲库曲目。"""
    if library is None:
        return None
    moods: list[str] = []
    for shot in getattr(storyboard, "shots", []) or []:
        mood = str(getattr(shot, "mood", "") or "").strip()
        if mood and mood not in moods:
            moods.append(mood)
    return library.pick(moods, preferred_id=settings.bgm_id, fallback=True)


# ── 本地作曲 ──────────────────────────────────────────


async def _generate_local(
    *,
    project: Project,
    storyboard: Storyboard,
    video_manifest: VideoManifest | None,
    audio_manifest: AudioManifest | None,
    output_dir: Path,
    frames_dir: Path | None,
    settings: Settings,
    request: MusicRequest,
    llm: LLMProvider | None = None,
) -> MusicOutcome:
    shots = list(getattr(storyboard, "shots", []) or [])
    duration = round(
        sum(max(0.0, float(getattr(shot, "duration_sec", 0) or 0)) for shot in shots), 3
    )
    if duration <= 0:
        return MusicOutcome(engine="generate", note="分镜为空或时长为 0，无法配乐")

    directory = output_dir / MUSIC_DIR
    directory.mkdir(parents=True, exist_ok=True)
    note = (request.note or project.music_note or settings.music_note or "").strip()
    mood = (request.mood or project.music_mood or settings.music_mood or "").strip()
    seed = request.seed or _seed_for(project, storyboard)
    bpm = request.bpm or settings.music_bpm

    speech = _speech_windows(shots, audio_manifest)
    motions = await _collect_motions(
        shots,
        video_manifest,
        frames_dir,
        project_dir=output_dir.parent,
        fps=settings.audio_motion_rate,
    )

    brief: list[dict[str, Any]] | None = None
    source = "heuristic"
    brief_note = ""
    if (request.llm_brief or settings.music_llm_brief) and llm is not None:
        brief, brief_note = await _llm_brief(
            llm, project=project, shots=shots, moods=_moods(storyboard), note=note
        )
        if brief:
            source = "llm"

    fingerprint = _fingerprint(
        shots=shots,
        moods=_moods(storyboard),
        speech=speech,
        motions=motions,
        note=note,
        mood=mood,
        bpm=bpm,
        seed=seed,
        settings=settings,
        source=source,
    )
    plan_path = directory / "music_plan.json"
    audio_path = directory / "bgm.wav"
    if not request.force and _cache_valid(plan_path, audio_path, fingerprint):
        plan = MusicPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        track = _track_for(
            project,
            plan,
            audio_path,
            request,
            settings,
            plan_path=_relative_to(plan_path, output_dir.parent),
        )
        measure = plan.stats.get("measure", {})
        logger.info("配乐复用缓存：%s", plan_path.name)
        return MusicOutcome(
            ok=True,
            engine="generate",
            track=track,
            plan=plan,
            note=(
                f"复用已生成的配乐（{plan.color_label} / {plan.bpm} BPM，"
                f"{len(plan.sections)} 段）"
            ),
            cached=True,
            audio=audio_path,
            measure=measure if isinstance(measure, dict) else {},
        )

    plan = music_utils.build_plan(
        shots=shots,
        speech_windows=speech,
        motions=motions,
        duration_sec=duration,
        seed=seed,
        moods=_moods(storyboard),
        note=note,
        color=mood,
        bpm=bpm,
        section_hint=brief,
        source=source,
    )
    if brief_note:
        plan.notes.append(brief_note)
    plan.stats["fingerprint"] = fingerprint
    plan.stats["motions"] = len([1 for value in motions.values() if value is not None])

    dry = directory / "bgm_dry.wav"
    try:
        render_stats = music_utils.render_plan(plan, dry)
    except Exception as exc:  # 合成失败不能让整条流水线崩
        logger.exception("配乐合成失败")
        return MusicOutcome(engine="generate", note=f"配乐合成失败：{exc}")

    reverb = _reverb_for(plan, settings)
    ir_path = directory / "reverb_ir.wav"
    music_utils.make_impulse_response(
        ir_path, seconds=round(1.0 + 1.6 * reverb, 2), seed=seed % 100000
    )
    try:
        await _post_process(dry, ir_path, audio_path, reverb=reverb, duration=duration)
    except FFmpegError as exc:
        logger.warning("配乐后处理失败，改用干燥音轨：%s", exc)
        audio_path.write_bytes(dry.read_bytes())

    measure = _measure_wav(audio_path)
    dry_measure = _measure_wav(dry)
    if _post_process_broken(dry_measure, measure):
        # 真实事故：afir 的自动增益把整条音轨压到 -81dBFS，成片「有配乐」却听不见。
        # 这里量一遍再决定交付谁 —— 只听日志是不够的。
        logger.warning(
            "配乐后处理结果异常（峰值 %.4f / RMS %.1f dBFS，干燥音轨 %.1f dBFS），"
            "改用未加混响的干燥音轨",
            measure.get("peak", 0.0),
            measure.get("rms_db", -120.0),
            dry_measure.get("rms_db", -120.0),
        )
        audio_path.write_bytes(dry.read_bytes())
        measure = {**dry_measure, "post_fallback": True, "post_broken": measure}
        plan.notes.append("后处理结果异常（音量几乎归零），已退回未加混响的干燥音轨")
    plan.stats["render"] = render_stats
    plan.stats["measure"] = measure
    plan.stats["reverb"] = round(reverb, 3)
    plan.audio_path = _relative_to(audio_path, output_dir.parent)
    plan_rel = _relative_to(plan_path, output_dir.parent)
    try:
        plan_path.write_text(plan.model_dump_json(indent=1), encoding="utf-8")
    except OSError:
        logger.debug("配乐谱落盘失败", exc_info=True)

    track = _track_for(project, plan, audio_path, request, settings, plan_path=plan_rel)
    logger.info(
        "配乐生成完成：%s / %s / %.1fs / 峰值 %.3f / %.1f dBFS（%.1fs 计算）",
        plan.color_label,
        plan.key_name,
        plan.duration_sec,
        measure.get("peak", 0.0),
        measure.get("rms_db", 0.0),
        render_stats.get("elapsed_sec", 0.0),
    )
    return MusicOutcome(
        ok=True,
        engine="generate",
        track=track,
        plan=plan,
        note=(
            f"本机现写配乐：{plan.color_label} / {plan.key_name} / {plan.bpm} BPM / "
            f"{len(plan.sections)} 段 / {len(plan.accents)} 个重音（$0，本地合成）"
        ),
        audio=audio_path,
        measure=measure,
    )


def _seed_for(project: Project, storyboard: Storyboard) -> int:
    """稳定的随机种子：同一个项目 + 同一版分镜 → 同一首曲子。"""
    material = f"{project.id}:{len(getattr(storyboard, 'shots', []) or [])}"
    return int(hashlib.sha1(material.encode("utf-8")).hexdigest()[:8], 16)


def _moods(storyboard: Storyboard) -> list[str]:
    moods: list[str] = []
    for shot in getattr(storyboard, "shots", []) or []:
        mood = str(getattr(shot, "mood", "") or "").strip()
        if mood and mood not in moods:
            moods.append(mood)
    return moods


def _speech_windows(
    shots: list[Any], audio_manifest: AudioManifest | None
) -> list[tuple[float, float]]:
    """把「哪个镜头有人说话」变成时间轴上的说话窗口。

    用配音的**真实时长**（`AudioClip.duration_sec`）而不是台词字数估算 ——
    作曲时旋律要避开的就是这段真实的人声。
    """
    windows: list[tuple[float, float]] = []
    if audio_manifest is None:
        return windows
    clock = 0.0
    for shot in shots:
        duration = max(0.0, float(getattr(shot, "duration_sec", 0) or 0))
        clip = audio_manifest.for_shot(str(getattr(shot, "id", "")))
        if clip is not None and clip.duration_sec > 0:
            lead = 0.4
            start = clock + min(lead, max(0.0, duration - 0.3))
            end = min(clock + duration, start + float(clip.duration_sec) + 0.2)
            if end > start:
                windows.append((round(start, 3), round(end, 3)))
        clock += duration
    return windows


async def _collect_motions(
    shots: list[Any],
    video_manifest: VideoManifest | None,
    frames_dir: Path | None,
    *,
    project_dir: Path,
    fps: float = 6.0,
) -> dict[str, Any]:
    """每个镜头的画面运动实测（与配音文案共用同一份缓存，算过就不再算）。

    清单里的片段路径是相对项目目录的（`clips/x.mp4`），必须拼上项目目录再判断存在：
    以前直接 `Path(clip.path).is_file()`，按进程工作目录解析永远找不到，
    配乐从来没拿到过运动数据（「动作爆发给重音」一直没生效）。
    """
    result: dict[str, Any] = {}
    if not ffmpeg_available():
        return result
    clips: dict[str, Path] = {}
    for clip in getattr(video_manifest, "ready_clips", []) or []:
        shot_id = str(getattr(clip, "shot_id", ""))
        raw = str(getattr(clip, "path", "") or "")
        path = project_dir / raw if raw else None
        if shot_id and path is not None and path.is_file():
            clips[shot_id] = path
    if not clips:
        return result

    async def one(shot_id: str, path: Path) -> tuple[str, Any]:
        cache = (frames_dir / f"{shot_id}.motion.json") if frames_dir else None
        if cache is not None:
            cached = motion_utils.load_cached_motion(cache, path)
            if cached is not None:
                return shot_id, cached
        analysis = await motion_utils.analyze_clip(path, fps=fps)
        if analysis is not None and cache is not None:
            motion_utils.save_motion(cache, path, analysis)
        return shot_id, analysis

    targets = [
        (str(getattr(shot, "id", "")), clips[str(getattr(shot, "id", ""))])
        for shot in shots
        if str(getattr(shot, "id", "")) in clips
    ]
    semaphore = asyncio.Semaphore(2)

    async def guarded(shot_id: str, path: Path) -> tuple[str, Any]:
        async with semaphore:
            return await one(shot_id, path)

    for shot_id, analysis in await asyncio.gather(*(guarded(*item) for item in targets)):
        result[shot_id] = analysis
    logger.info(
        "配乐：%d/%d 个镜头有画面运动实测", len([v for v in result.values() if v]), len(shots)
    )
    return result


def _fingerprint(**parts: Any) -> str:
    """输入指纹：输入变了就重新生成，没变就复用（避免每次合成重算音乐）。"""
    payload = {
        "shots": [
            [
                str(getattr(shot, "id", "")),
                round(float(getattr(shot, "duration_sec", 0) or 0), 3),
                str(getattr(shot, "mood", "") or ""),
                bool(str(getattr(shot, "dialogue_text", "") or "").strip()),
            ]
            for shot in parts["shots"]
        ],
        "moods": parts["moods"],
        "speech": parts["speech"],
        "note": parts["note"],
        "mood": parts["mood"],
        "bpm": parts["bpm"],
        "seed": parts["seed"],
        "source": parts["source"],
        "reverb": parts["settings"].music_reverb,
        "motions": {
            key: (round(value.overall.mean(), 4) if value is not None else None)
            for key, value in (parts["motions"] or {}).items()
        },
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _cache_valid(plan_path: Path, audio_path: Path, fingerprint: str) -> bool:
    if not plan_path.is_file() or not audio_path.is_file() or audio_path.stat().st_size == 0:
        return False
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    stats = payload.get("stats") or {}
    if not isinstance(stats, dict):
        return False
    if stats.get("fingerprint") != fingerprint:
        logger.info("配乐输入已变（指纹不同），重新生成")
        return False
    return True


def _reverb_for(plan: MusicPlan, settings: Settings) -> float:
    forced = settings.music_reverb_ratio
    if forced >= 0:
        return forced
    from videogen.utils.music import COLORS

    color = COLORS.get(plan.color)
    return float(color.reverb) if color else 0.4


async def _post_process(
    dry: Path, ir_path: Path, output: Path, *, reverb: float, duration: float
) -> None:
    """混响（afir 卷积）→ 塑形 → 压缩 → 限幅 → 响度归一到目标 LUFS。

    作曲出来的干燥音轨频谱很干净但「贴脸」：这一步把它推到「音乐床」的位置，
    同时把响度固定下来 —— 否则不同情绪的曲子音量差很多，混音增益就没法配。

    两个踩过的坑（都在报告里有实测记录）：

    1. `afir` 的自动增益**不能用**：`irnorm=1`（默认值）会把湿音压掉约 80dB
       （2026-09-14 实测：整条音轨 RMS 掉到 -81dBFS，等于没配乐）。现在 IR 自己在
       Python 里定死峰值，混响用固定 `volume` 混，`afir` 只当卷积器。
    2. `afir` 是流式滤波器，**尾巴要自己喂**：不给输入 `apad`，卷积尾巴会被截掉，
       混响就只剩一层薄薄的早期反射。
    """
    wet = round(min(0.8, max(0.0, 0.25 + 0.32 * reverb)), 3)
    chain = (
        "[0:a]highpass=f=30,"
        "equalizer=f=240:width_type=q:w=1:g=-1.5,"
        "equalizer=f=3200:width_type=q:w=1.1:g=1.5[base];"
        "[base]asplit=2[dry][wetin];"
        # 卷积尾巴要喂出来（IR 最长 2.6s，这里多喂一点再截回去）
        "[wetin]apad=pad_dur=3[wetpad];"
        # irnorm=0 + gtype=-1：不用 afir 的自动增益（它会压掉 80dB），IR 峰值自己定
        "[wetpad][1:a]afir=dry=1:wet=1:gtype=-1:irnorm=0,"
        f"volume={wet}[wetsig];"
        "[dry][wetsig]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[mixed];"
        "[mixed]acompressor=threshold=-18dB:ratio=2.5:attack=20:release=320:makeup=1.2,"
        "alimiter=limit=0.95:level=disabled,"
        f"loudnorm=I={TARGET_LUFS:g}:TP=-2:LRA=9,"
        f"aresample=44100,aformat=channel_layouts=stereo,"
        f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[out]"
    )
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(dry),
            "-i", str(ir_path),
            "-filter_complex", chain,
            "-map", "[out]",
            "-c:a", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            str(output),
        ],
        timeout_sec=POST_TIMEOUT_SEC,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise FFmpegError("配乐后处理没有产出文件")


async def _generate_cli(
    *,
    project: Project,
    storyboard: Storyboard,
    video_manifest: VideoManifest | None,
    audio_manifest: AudioManifest | None,
    output_dir: Path,
    frames_dir: Path | None,
    settings: Settings,
    request: MusicRequest,
) -> MusicOutcome:
    """外部音乐模型：先用同一套逻辑写谱（只为了提示词与段落），再交给命令生成。"""
    shots = list(getattr(storyboard, "shots", []) or [])
    duration = round(
        sum(max(0.0, float(getattr(shot, "duration_sec", 0) or 0)) for shot in shots), 3
    )
    if duration <= 0:
        return MusicOutcome(engine="cli", note="分镜为空或时长为 0，无法配乐")

    directory = output_dir / MUSIC_DIR
    directory.mkdir(parents=True, exist_ok=True)
    seed = request.seed or _seed_for(project, storyboard)
    note = (request.note or project.music_note or settings.music_note or "").strip()
    mood = (request.mood or project.music_mood or settings.music_mood or "").strip()
    plan = music_utils.build_plan(
        shots=shots,
        speech_windows=_speech_windows(shots, audio_manifest),
        motions=await _collect_motions(
            shots,
            video_manifest,
            frames_dir,
            project_dir=output_dir.parent,
            fps=settings.audio_motion_rate,
        ),
        duration_sec=duration,
        seed=seed,
        moods=_moods(storyboard),
        note=note,
        color=mood,
        bpm=request.bpm or settings.music_bpm,
        source="cli",
    )
    plan.engine = "cli"
    prompt = music_prompt(plan)
    raw = directory / "bgm_cli_raw"
    try:
        command = command_for(settings, out=raw, duration=duration, prompt=prompt)
    except ValueError as exc:
        return MusicOutcome(engine="cli", note=str(exc))
    code, detail = await _run(
        command, timeout_sec=min(CLI_TIMEOUT_CAP_SEC, max(30.0, settings.bgm_cli_timeout_sec))
    )
    if code != 0:
        return MusicOutcome(engine="cli", note=f"外部音乐模型失败（退出码 {code}）：{detail}")

    produced = _find_output(raw, directory)
    if produced is None:
        return MusicOutcome(engine="cli", note=f"外部音乐模型没有产出音频文件（{raw.name}*）")

    audio_path = directory / "bgm.wav"
    ir_path = directory / "reverb_ir.wav"
    reverb = _reverb_for(plan, settings)
    music_utils.make_impulse_response(
        ir_path, seconds=round(1.0 + 1.6 * reverb, 2), seed=seed % 100000
    )
    try:
        # 外部模型的输出格式/采样率/响度都不可控，统一过一遍后处理
        await _post_process(
            produced, ir_path, audio_path, reverb=reverb * 0.6, duration=duration
        )
    except FFmpegError as exc:
        return MusicOutcome(engine="cli", note=f"外部音乐模型产出无法处理：{exc}")

    measure = _measure_wav(audio_path)
    plan.stats["measure"] = measure
    plan.stats["prompt"] = prompt
    plan.audio_path = _relative_to(audio_path, output_dir.parent)
    cli_plan_path = directory / "music_plan.json"
    cli_plan_path.write_text(plan.model_dump_json(indent=1), encoding="utf-8")
    _write_prompt(directory, prompt)
    track = _track_for(
        project,
        plan,
        audio_path,
        request,
        settings,
        plan_path=_relative_to(cli_plan_path, output_dir.parent),
    )
    return MusicOutcome(
        ok=True,
        engine="cli",
        track=track,
        plan=plan,
        note=f"外部音乐模型生成（{len(command)} 段命令）：{audio_path.name}",
        audio=audio_path,
        measure=measure,
    )


def _write_prompt(directory: Path, prompt: str) -> None:
    try:
        (directory / "music_prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    except OSError:
        logger.debug("提示词落盘失败", exc_info=True)


def _find_output(raw: Path, directory: Path) -> Path | None:
    """外部模型的产物可能没有扩展名，也可能自己加扩展名 —— 两种都认。"""
    candidates = [raw, *sorted(directory.glob(f"{raw.name}.*"))]
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


async def _run(command: list[str], *, timeout_sec: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"超时（{timeout_sec:.0f}s）"
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        text = (stderr or stdout or b"").decode("utf-8", "replace")
        tail = " | ".join(text.strip().splitlines()[-3:])
        return process.returncode or 1, tail[:300]
    return 0, ""


# ── LLM 配乐建议（可选，付费）────────────────────────────

_BRIEF_SYSTEM = """你是一位影视配乐指导。给你一部短剧的项目信息与分镜（镜头情绪、时长、
有没有台词），请给出**配乐设计**：

1. `color` 从这些里选一个最贴全片的：tense（紧张/悬疑）、sad（悲伤/压抑）、
   warm（温暖/日常）、hopeful（希望/坚定）、action（激烈/动作）、mystery（神秘/悬念）、
   playful（轻快/喜剧）、neutral（中性铺底）
2. `bpm` 给一个 40-180 的整数（快剪动作可以 110-130，抒情 60-80）
3. `sections` 按剧情走向给 3-6 段，每段 `mood` 用中文写这一段的情绪（简短），
   `energy` 给 0-3（0 = 只有铺底，3 = 配器顶满）
4. `note` 一句话说明你的配乐思路

只输出 JSON：{"color": "...", "bpm": 84, "note": "...",
"sections": [{"mood": "...", "energy": 1}]}"""


async def _llm_brief(
    llm: LLMProvider,
    *,
    project: Project,
    shots: list[Any],
    moods: list[str],
    note: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    """让 LLM 给一次配乐建议。失败就退回纯启发式（绝不因此不出片）。"""
    total = sum(float(getattr(shot, "duration_sec", 0) or 0) for shot in shots)
    lines = [
        f"标题：{project.title}",
        f"目标时长：{project.target_duration_sec} 秒",
        f"分镜情绪：{'、'.join(moods) or '未标注'}",
        f"镜头数：{len(shots)}，总时长 {total:.0f} 秒",
        "镜头明细（顺序、时长、情绪、有无台词）：",
    ]
    for shot in shots:
        dialogue = "有台词" if str(getattr(shot, "dialogue_text", "") or "").strip() else "无台词"
        lines.append(
            f"- {getattr(shot, 'id', '')} {float(getattr(shot, 'duration_sec', 0) or 0):.1f}s "
            f"{getattr(shot, 'mood', '') or '未标注'} {dialogue}"
        )
    if note:
        lines.append(f"用户的配乐要求：{note}")
    messages: list[LLMMessage] = [
        text_message("system", _BRIEF_SYSTEM),
        text_message("user", "\n".join(lines)),
    ]
    try:
        response = await llm.chat_json(
            messages, schema=MusicBrief, temperature=0.4, max_tokens=1200
        )
    except (LLMError, ValueError) as exc:  # LLM 失败不影响配乐
        logger.warning("LLM 配乐建议失败，改用启发式：%s", exc)
        return None, "LLM 配乐建议失败，本次按分镜情绪与画面运动自动配"
    if isinstance(response, MusicBrief):
        brief = response
    elif isinstance(response, dict):
        brief = MusicBrief.model_validate(response)
    else:
        return None, ""
    hint: list[dict[str, Any]] = []
    if brief.color:
        hint.append({"color": brief.color})
    for section in brief.sections:
        hint.append({"mood": section.mood, "energy": section.energy})
    note_text = f"LLM 配乐建议：{brief.note}" if brief.note else "配乐参考了 LLM 的建议"
    return (hint or None), note_text


# ── 收尾 ──────────────────────────────────────────────


def _track_for(
    project: Project,
    plan: MusicPlan,
    audio_path: Path,
    request: MusicRequest,
    settings: Settings,
    *,
    plan_path: str = "",
) -> BgmTrack:
    gain = request.gain_db
    if gain is None:
        gain = project.music_gain_db if project.music_gain_db is not None else None
    duck = request.duck
    if duck is None:
        duck = project.music_duck if project.music_duck is not None else None
    return BgmTrack(
        id=f"generated:{plan.seed}",
        title=f"{plan.color_label} · {plan.key_name} · {plan.bpm}BPM",
        path=str(audio_path),
        moods=list(plan.mood_tags),
        duration_sec=plan.duration_sec,
        gain_db=None if gain is None else float(gain),
        license="本机合成（无第三方素材，可自由使用）",
        score=99.0,
        engine="generate" if plan.engine == "local-synth" else plan.engine,
        plan_path=plan_path,
        fade_sec=(
            request.fade_sec
            if request.fade_sec is not None
            else round(min(3.0, max(1.5, plan.bar_sec)), 2)
        ),
        duck=None if duck is None else bool(duck),
        loop=False,
    )


def _post_process_broken(dry: dict[str, Any], post: dict[str, Any]) -> bool:
    """后处理是不是把音乐弄没了（静音/几乎静音）。

    判据刻意宽松：只要还有声音、响声在合理区间就放过 ——
    这条自检是为了拦「整条音轨归零」这种事故，不是给混音做审美判断。
    """
    if not post:
        return True
    peak = float(post.get("peak", 0.0) or 0.0)
    rms = float(post.get("rms_db", -120.0) or -120.0)
    if peak < 0.02 or rms < -35.0:
        return True
    dry_rms = float(dry.get("rms_db", -120.0) or -120.0)
    # 干燥音轨本来就小声时（编配很薄）不按「比干燥小多少」判，交给上面两条
    return bool(dry_rms > -40.0 and rms < dry_rms - 18.0)


def _measure_wav(path: Path) -> dict[str, Any]:
    """读回生成的 WAV，量峰值/RMS/时长 —— 报告里的数字必须是实测的。"""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 44100
            channels = handle.getnchannels() or 2
            raw = handle.readframes(frames)
    except (OSError, wave.Error):
        return {}
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    peak = 0
    squares = 0.0
    for value in samples:
        magnitude = value if value >= 0 else -value
        if magnitude > peak:
            peak = magnitude
        squares += float(value) * float(value)
    rms = math.sqrt(squares / len(samples)) if samples else 0.0
    return {
        "duration_sec": round(frames / rate, 3),
        "sample_rate": rate,
        "channels": channels,
        "peak": round(peak / 32768.0, 4),
        "rms_db": round(20.0 * math.log10(rms / 32768.0), 2) if rms > 0 else -120.0,
    }


def _relative_to(path: Path, project_dir: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(project_dir).resolve()))
    except ValueError:
        return str(path)
