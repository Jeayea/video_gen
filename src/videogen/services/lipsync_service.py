"""逐帧口型后处理：把画面里的嘴按配音逐帧重画（可插拔引擎）。

和「口型对齐」（`compose_align_dub_to_mouth`，把配音整体延后到画面开口的时刻）
不是一回事，两者解决的是不同层次的问题：

| 方案 | 做什么 | 代价 | 局限 |
|---|---|---|---|
| 口型对齐 | 把配音**整体延时**到画面开口 | 本地计算，$0 | 只是起点对齐，一句话内部的节奏对不上 |
| 逐帧口型（本模块） | 按配音**逐帧重画嘴部** | 本地 GPU/CPU，$0 | 96×96 的嘴
  放大到 1440 宽会偏软 |

引擎做成可插拔的，因为「本地能跑」和「质量最好」是两件事：

- `wav2lip-onnx`（内置，默认）：Wav2Lip 的 ONNX 版，**不需要 torch**，
  Apple Silicon 的 CPU 上 5 秒 1440×2560 的镜头约 4~5 秒。装在独立的
  `.venv-lipsync`（`tools/lipsync/install.sh`），主环境一个依赖都不加。
- `cli`：任意外部命令，模板里带 `{video} {audio} {out}` 占位符。
  LatentSync / MuseTalk / SadTalker 这类需要 CUDA + torch 的引擎走这条路，
  一行配置就能换上（它们的权重和显存要求不适合塞进这个仓库）。

失败**绝不阻断合成**：任何一步出问题都退回原片段并记一条 warning，
宁可口型差一点，也不能因为一个可选后处理把整条流水线打死。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from videogen.config import Settings, get_settings
from videogen.utils import lip_sync as lip_sync_utils
from videogen.utils.audio_align import speech_span
from videogen.utils.ffmpeg import FFmpegError, probe_streams, run_ffmpeg
from videogen.utils.text import clean_spoken_text

logger = logging.getLogger(__name__)

__all__ = [
    "LIPSYNC_ENGINES",
    "LipsyncOutcome",
    "command_for",
    "engine_problem",
    "shot_eligible",
    "sync_shot",
]

#: 支持的引擎名
LIPSYNC_ENGINES = ("none", "wav2lip-onnx", "cli")

#: 内置引擎的脚本位置（相对仓库根）
WAV2LIP_SCRIPT = Path("tools") / "lipsync" / "wav2lip_onnx.py"
#: 拼接原片段音轨时的超时
MUX_TIMEOUT_SEC = 120.0


def _absolute(value: str | Path) -> Path:
    """配置里的相对路径按**当前工作目录**解析成绝对路径。

    踩过两个坑，都是真实跑才暴露的：
    1. 命令带着 `cwd=项目目录` 执行时，`.venv-lipsync/bin/python` 被当成「项目目录下
       的 .venv-lipsync/bin/python」→ FileNotFoundError。
    2. 用 `Path.resolve()` 转绝对路径会**跟随符号链接**：venv 里的 `bin/python` 是指向
       系统 python 的软链，resolve 之后就变成了系统解释器，venv 直接被绕过
       → `ModuleNotFoundError: No module named 'cv2'`。
    所以这里用 `os.path.abspath`：只补全目录、**不解析软链**。
    """
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(os.path.abspath(path))
#: 内置引擎「输出与输入几乎一样」的告警门槛（对应脚本里的 mean_patch_change）
MIN_PATCH_CHANGE = 0.01


@dataclass
class LipsyncOutcome:
    """一个镜头的口型结果。"""

    shot_id: str
    ok: bool = False
    engine: str = ""
    output: Path | None = None
    note: str = ""
    elapsed_sec: float = 0.0
    frames: int = 0
    #: 引擎自报的统计（mean_patch_change / detect_ratio / …）
    report: dict = field(default_factory=dict)
    #: 配音区间 / 嘴部改写区间（秒），量不出来就是 None
    dub_span: tuple[float, float] | None = None
    rewrite_span: tuple[float, float] | None = None

    def summary(self) -> dict:
        return {
            "shot_id": self.shot_id,
            "ok": self.ok,
            "engine": self.engine,
            "output": self.output.name if self.output else "",
            "note": self.note,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "frames": self.frames,
            "dub_span": list(self.dub_span) if self.dub_span else None,
            "rewrite_span": list(self.rewrite_span) if self.rewrite_span else None,
            "report": self.report,
        }


def shot_eligible(shot, *, scope: str = "dialogue") -> bool:
    """这个镜头该不该做逐帧口型。

    默认只做「说话人就在画面里」的镜头：旁白镜头的画面里那个人本来就不该跟着
    旁白动嘴；说话人不在画面里（过肩/听者反应）时替他动嘴更是穿帮。
    """
    if shot is None:
        return False
    if scope == "all":
        return True
    if not clean_spoken_text(getattr(shot, "dialogue_text", "")):
        return False
    if getattr(shot, "voice_offscreen", False):
        return False  # 画外音：画面里没人在说这句
    speaker = (getattr(shot, "dialogue_speaker", "") or "").strip()
    in_frame = [str(item) for item in (getattr(shot, "character_in_frame", None) or [])]
    return not (speaker and in_frame and speaker not in in_frame)


def command_for(
    settings: Settings, *, video: Path, audio: Path, out: Path, report: Path
) -> list[str]:
    """拼出要执行的命令（内置引擎或 cli 模板）。"""
    engine = settings.lipsync
    if engine == "cli":
        template = (settings.lipsync_cmd or "").strip()
        if not template:
            raise ValueError("lipsync_engine=cli 但没有配置 LIPSYNC_CMD")
        for placeholder in ("{video}", "{audio}", "{out}"):
            if placeholder not in template:
                raise ValueError(f"LIPSYNC_CMD 缺少占位符 {placeholder}")
        filled = (
            template.replace("{video}", str(_absolute(video)))
            .replace("{audio}", str(_absolute(audio)))
            .replace("{out}", str(_absolute(out)))
            .replace("{report}", str(_absolute(report)))
        )
        return shlex.split(filled)

    model_dir = _absolute(settings.lipsync_model_dir)
    command = [
        str(_absolute(settings.lipsync_python)),
        str(WAV2LIP_SCRIPT),
        "--video", str(_absolute(video)),
        "--audio", str(_absolute(audio)),
        "--out", str(_absolute(out)),
        "--model", str(model_dir / settings.lipsync_model),
        "--detector", str(model_dir / settings.lipsync_detector),
        "--report", str(_absolute(report)),
    ]
    extra = (settings.lipsync_extra_args or "").strip()
    if extra:
        command += shlex.split(extra)
    return command


def engine_problem(settings: Settings) -> str:
    """引擎不可用时返回原因（可用返回空串）—— 配置阶段就能提前发现。"""
    engine = settings.lipsync
    if engine == "none":
        return ""
    if engine == "cli":
        if not (settings.lipsync_cmd or "").strip():
            return "lipsync_engine=cli 但没有配置 LIPSYNC_CMD"
        return ""
    model_dir = _absolute(settings.lipsync_model_dir)
    python = _absolute(settings.lipsync_python)
    if not python.is_file():
        return (
            f"找不到逐帧口型的解释器 {python}（先跑 tools/lipsync/install.sh 建独立环境）"
        )
    script = _absolute(WAV2LIP_SCRIPT)
    if not script.is_file():
        return f"找不到逐帧口型脚本 {script}"
    for name in (settings.lipsync_model, settings.lipsync_detector):
        if not (model_dir / name).is_file():
            return f"找不到模型文件 {model_dir / name}（先跑 tools/lipsync/install.sh 下载）"
    return ""


async def _run(command: list[str], *, timeout_sec: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
    text = (stdout or b"").decode("utf-8", "replace")
    err = (stderr or b"").decode("utf-8", "replace")
    if process.returncode != 0:
        tail = " | ".join((err or text).strip().splitlines()[-3:])
        return process.returncode or 1, tail[:300]
    return 0, text


def _read_report(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def sync_shot(
    video_path: Path,
    audio_path: Path,
    output: Path,
    *,
    shot_id: str = "",
    settings: Settings | None = None,
) -> LipsyncOutcome:
    """给一个镜头做逐帧口型；失败返回 `ok=False` 的 outcome（不抛异常）。

    `output` 写出来的是**没有音轨**的 mp4（引擎只管画面），这里会把原片段的
    音轨接回去 —— 这样 `compose_ambient_mode` 那条链路完全不受影响。
    """
    settings = settings or get_settings()
    engine = settings.lipsync
    outcome = LipsyncOutcome(shot_id=shot_id, engine=engine)
    started = time.time()

    problem = engine_problem(settings)
    if problem:
        outcome.note = problem
        return outcome

    output.parent.mkdir(parents=True, exist_ok=True)
    report_path = output.with_suffix(".report.json")
    try:
        command = command_for(
            settings, video=video_path, audio=audio_path, out=output, report=report_path
        )
    except ValueError as exc:
        outcome.note = str(exc)
        return outcome

    code, detail = await _run(command, timeout_sec=settings.lipsync_timeout_sec)
    outcome.elapsed_sec = round(time.time() - started, 2)
    if code != 0 or not output.is_file() or output.stat().st_size == 0:
        outcome.note = f"引擎失败（退出码 {code}）：{detail}"
        output.unlink(missing_ok=True)
        return outcome

    report = _read_report(report_path)
    outcome.report = report
    outcome.frames = int(report.get("frames") or 0)
    change = float(report.get("mean_patch_change") or 0.0)
    if report and change < MIN_PATCH_CHANGE:
        outcome.note = (
            f"引擎输出与输入几乎相同（mean_patch_change={change:.4f}），口型可能没生效"
        )
        output.unlink(missing_ok=True)
        return outcome

    muxed = await _mux_original_audio(video_path, output)
    if not muxed:
        outcome.note = "口型已生成，但原片段音轨接回失败（已放弃这个镜头）"
        output.unlink(missing_ok=True)
        return outcome

    if settings.lipsync_measure:
        await _measure(
            outcome,
            original=video_path,
            synced=output,
            audio=audio_path,
            fps=float(max(1, settings.video_fps)),
        )
    outcome.ok = True
    outcome.output = output
    logger.info(
        "逐帧口型 OK：%s（%s，%d 帧，%.1fs）", shot_id or output.name, engine,
        outcome.frames, outcome.elapsed_sec,
    )
    return outcome


async def _mux_original_audio(video_path: Path, output: Path) -> bool:
    """把原片段的音轨接回口型结果（引擎只输出画面）。

    直接 `-map 1:a? -c copy` 会在目标已有音轨时冲突，所以先看引擎有没有给音轨：
    没有就补原片段的，有就保持原样。
    """
    streams = await probe_streams(output)
    if streams and "audio" in streams:
        return True
    temp = output.with_name(f"{output.stem}.mux{output.suffix}")
    try:
        await run_ffmpeg(
            [
                "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(output),
                "-i", str(video_path),
                "-map", "0:v:0", "-map", "1:a:0?",
                "-c", "copy", "-movflags", "+faststart", str(temp),
            ],
            timeout_sec=MUX_TIMEOUT_SEC,
        )
    except FFmpegError as exc:
        logger.warning("接回原音轨失败：%s", exc)
        temp.unlink(missing_ok=True)
        return False
    temp.replace(output)
    return True


async def _measure(
    outcome: LipsyncOutcome, *, original: Path, synced: Path, audio: Path, fps: float
):
    """量「配音区间」和「嘴部改写区间」，写进报告（纯本地、免费）。"""
    try:
        dub = await speech_span(audio, audio.parent)
        if dub:
            outcome.dub_span = (round(dub[0], 3), round(dub[1], 3))
        series = await lip_sync_utils.rewrite_activity(synced, original)
        span = lip_sync_utils.activity_span(series, fps=fps, ratio=0.25)
        if span:
            outcome.rewrite_span = (span.start, span.end)
    except Exception:
        # 诊断量不出来不影响口型本身（BLE001 的常规例外：这条路失败无需上报）
        logger.debug("口型诊断测量失败：%s", outcome.shot_id, exc_info=True)
