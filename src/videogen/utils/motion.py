"""画面运动实测：把「这个镜头里画面怎么动」变成可以写进配音文案的信息。

为什么要有它：现在给配音导演（LLM）看的只有**静态抽帧**，它能知道画面里是谁、
在哪、什么景别，但完全不知道**什么时候动、动得多猛**。于是念法和画面的节奏对不上 ——
镜头里人先愣两秒、再猛地转身，念法却是从头到尾一个速度。

做法刻意选最朴素的：把片段抽成 6fps / 64×64 灰度原始帧（`ffmpeg -f rawvideo`），
相邻帧逐像素求平均绝对差，得到一条运动曲线。**纯标准库**，不引 numpy、不调模型 ——
它必须能在没有 GPU、没有任何额外依赖的机器上跑。

两级判读（阈值是在 8 个真实片段上量出来的，见 `tests/test_motion.py`）：
1. **整段活跃度**：用曲线均值分档（真实片段实测 0.008 ~ 0.090）——
   「几乎静止 / 轻微 / 明显 / 剧烈」，回答「这个镜头本来就一直在动吗」
2. **起伏窗口**：以中位数为基线，找 `>= 1.6×` 基线的连续窗口 ——
   回答「哪几秒突然动起来了」（真实片段峰均比 1.36 ~ 4.07）

只看相对峰值是不够的：说话的人整段都在动，峰均比只有 1.5，按相对峰值分档会把
一整段全标成「剧烈运动」，等于没说。所以要绝对档位 + 相对起伏一起看。
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from videogen.utils import ffmpeg as ffmpeg_utils

logger = logging.getLogger(__name__)

__all__ = [
    "MOTION_FPS",
    "MOTION_SIZE",
    "SUBJECT_REGION",
    "MotionAnalysis",
    "MotionCurve",
    "MotionSpan",
    "analyze_clip",
    "curve_from_frames",
    "load_cached_motion",
    "save_motion",
]

#: 采样帧率：够看出「先静后动」，又便宜（5 秒片段只抽 30 帧）
MOTION_FPS = 6
#: 采样边长：64×64 灰度 = 4KB/帧
MOTION_SIZE = 64
#: 说话人区域（画面 x0,y0,x1,y1 的比例）：下半部中央，用来近似「嘴和上半身怎么动」
SUBJECT_REGION = (0.20, 0.40, 0.80, 0.95)

#: 整段活跃度分档：曲线均值落在哪一档（真实片段实测 0.008~0.090）
_ACTIVITY_BANDS: tuple[tuple[float, str], ...] = (
    (0.012, "几乎静止"),
    (0.030, "轻微"),
    (0.060, "明显"),
    (float("inf"), "剧烈"),
)
#: 低于这个峰值就当成「整段没动」，后面的分档全部失去意义
_DEAD_PEAK = 0.004
#: 起伏判据：窗口强度 >= 中位数 × 这个倍数
_BURST_RATIO = 1.6
#: 起伏窗口至少这么长（秒），避免一帧噪声被当成一次动作
_BURST_MIN_SEC = 0.2
#: 开头这么长一段时间明显更静 → 「先静后动」
_QUIET_OPENING_SEC = 0.6


@dataclass(slots=True)
class MotionSpan:
    """一段「动起来」的窗口。"""

    start: float
    end: float
    level: str
    ratio: float
    #: True = 强度是「平时的 N 倍」；False = 整段基线为 0，只能说「此前几乎不动」
    relative: bool = True

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def text(self) -> str:
        if not self.relative:
            return f"{self.start:.2f}–{self.end:.2f}s 有一次{self.level}"
        return f"{self.start:.2f}–{self.end:.2f}s {self.level}（平时的 {self.ratio:.1f} 倍）"


@dataclass(slots=True)
class MotionCurve:
    """一条运动曲线：`energy[i]` 是第 i 个采样窗口（1/fps 秒）内的平均帧间变化。

    口径统一为 0..1 的「相邻帧平均绝对差 / 255」，所以不同片段之间可比。
    """

    fps: float
    size: int
    energy: list[float] = field(default_factory=list)
    region: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)

    @property
    def duration_sec(self) -> float:
        return round(len(self.energy) / self.fps, 3) if self.fps else 0.0

    @property
    def peak(self) -> float:
        return max(self.energy) if self.energy else 0.0

    @property
    def dead(self) -> bool:
        """整段几乎不动（也包括「抽不到帧」）。"""
        return self.peak < _DEAD_PEAK

    def mean(self) -> float:
        return round(sum(self.energy) / len(self.energy), 4) if self.energy else 0.0

    def baseline(self) -> float:
        """平时的运动强度（中位数）：起伏判据的基准，比均值抗尖峰。"""
        return round(statistics.median(self.energy), 4) if self.energy else 0.0

    def peak_time(self) -> float:
        """动作最强的时刻（秒）。"""
        if not self.energy:
            return 0.0
        index = max(range(len(self.energy)), key=lambda i: self.energy[i])
        return round((index + 1) / self.fps, 2)

    def activity(self) -> str:
        """整段活跃度档位。"""
        if not self.energy:
            return "未知"
        if self.dead:
            return "几乎静止"
        value = self.mean()
        for limit, label in _ACTIVITY_BANDS:
            if value < limit:
                return label
        return _ACTIVITY_BANDS[-1][1]

    def bursts(
        self,
        *,
        limit: int = 3,
        min_ratio: float = _BURST_RATIO,
        min_sec: float = _BURST_MIN_SEC,
    ) -> list[MotionSpan]:
        """找出「明显比平时动得厉害」的窗口，按发生时间排序（最多 `limit` 个）。"""
        if self.dead or len(self.energy) < 2:
            return []
        # 基线为 0（整段冻住、只有一下动作）时不能比倍数 —— 改用「峰值时刻」口径，
        # 否则一次快速转头会被整条曲线吞掉
        base = self.baseline()
        relative = base >= _DEAD_PEAK
        if relative:
            threshold = base * min_ratio
        else:
            base = max(self.peak, _DEAD_PEAK)
            threshold = self.peak * 0.6
        runs: list[list[int]] = []
        current: list[int] = []
        for index, value in enumerate(self.energy):
            if value >= threshold:
                current.append(index)
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)

        min_windows = max(1, round(min_sec * self.fps))
        found: list[MotionSpan] = []
        for run in runs:
            if len(run) < min_windows:
                continue
            ratio = max(self.energy[i] for i in run) / base
            found.append(
                MotionSpan(
                    start=round(run[0] / self.fps, 2),
                    end=round((run[-1] + 1) / self.fps, 2),
                    level=_burst_level(ratio) if relative else f"{self.activity()}动作",
                    ratio=round(ratio, 2),
                    relative=relative,
                )
            )
        if not found:
            return []
        top = sorted(found, key=lambda span: span.ratio, reverse=True)[: max(1, limit)]
        return sorted(top, key=lambda span: span.start)

    def quiet_opening(self, *, sec: float = _QUIET_OPENING_SEC) -> bool:
        """开头是不是明显更静（「先静后动」）。"""
        if not self.energy or self.dead:
            return False
        count = max(1, min(len(self.energy), round(sec * self.fps)))
        opening = sum(self.energy[:count]) / count
        if opening >= self.peak * 0.6:
            return False
        base = self.baseline()
        # 基线为 0（整段冻住、只有一下动作）时开头本来就是静的
        return opening < base * 0.8 if base > 0 else True

    def describe(self, *, label: str = "整体画面") -> str:
        """一句话说清这段画面怎么动。"""
        if not self.energy:
            return f"{label}：抽不到帧，运动未知"
        if self.dead:
            return f"{label}：整段几乎静止（{self.duration_sec:.2f}s）"
        head = (
            f"{label}：整段{self.activity()}（平均强度 {self.mean():.4f}，"
            f"峰值 {self.peak_time():.2f}s）"
        )
        bursts = self.bursts()
        if not bursts:
            return f"{head}，运动比较均匀"
        return f"{head}；起伏：" + "；".join(span.text() for span in bursts)

    def summary(self) -> dict[str, Any]:
        return {
            "fps": self.fps,
            "size": self.size,
            "region": list(self.region),
            "duration_sec": self.duration_sec,
            "mean": self.mean(),
            "baseline": self.baseline(),
            "peak": round(self.peak, 4),
            "peak_time": self.peak_time(),
            "activity": self.activity(),
            "quiet_opening": self.quiet_opening(),
            "energy": [round(v, 5) for v in self.energy],
            "bursts": [
                {
                    "start": span.start,
                    "end": span.end,
                    "level": span.level,
                    "ratio": span.ratio,
                    "relative": span.relative,
                }
                for span in self.bursts()
            ],
        }


@dataclass(slots=True)
class MotionAnalysis:
    """一个片段的运动实测结果（整体 + 说话人区域）。"""

    fps: float
    size: int
    frames: int
    overall: MotionCurve
    subject: MotionCurve

    def describe(self) -> str:
        """给配音导演看的运动说明（含念法建议），全中文。"""
        if self.frames < 2:
            return "画面运动：片段抽不到帧，运动未知"
        lines = [
            f"画面运动（本地实测：{self.fps:g}fps 采样 {self.size}×{self.size} 灰度，"
            f"相邻帧平均变化，共 {self.frames} 帧 / {self.overall.duration_sec:.2f}s）：",
            f"- {self.overall.describe(label='整体画面')}",
            f"- {self.subject.describe(label='说话人区域（画面下半部中央）')}",
        ]
        advice = self.advice()
        if advice:
            lines.append(f"- 念法建议：{advice}")
        return "\n".join(lines)

    def advice(self) -> str:
        """把运动曲线翻译成「这句话该怎么念」。"""
        curve = self.subject if not self.subject.dead else self.overall
        if curve.dead or not curve.energy:
            return "整段没有明显动作，念法可以稳、匀，不必刻意配合动作"

        parts: list[str] = []
        activity = curve.activity()
        if activity in {"明显", "剧烈"}:
            parts.append(f"画面整段在动（{activity}），念法别拖，跟上画面的节奏")
        elif activity == "几乎静止":
            parts.append("画面几乎不动，念法可以慢、可以留白")
        if curve.quiet_opening():
            parts.append("开头是静的，先压住、慢一点，别一上来就顶满")
        bursts = curve.bursts(limit=1)
        if bursts:
            span = bursts[0]
            parts.append(
                f"动作最猛在 {span.start:.2f}–{span.end:.2f}s（{span.level}），"
                "这段把音量和语速顶上去，让声音跟着动作走"
            )
        if not parts:
            parts.append("整段运动比较均匀，念法平稳即可")
        return "；".join(parts)

    def summary(self) -> dict[str, Any]:
        return {
            "fps": self.fps,
            "size": self.size,
            "frames": self.frames,
            "overall": self.overall.summary(),
            "subject": self.subject.summary(),
        }


def _burst_level(ratio: float) -> str:
    if ratio <= 2.0:
        return "明显起伏"
    if ratio <= 3.2:
        return "强烈起伏"
    return "剧烈起伏"


def _region_bounds(
    size: int, region: tuple[float, float, float, float]
) -> tuple[int, int, int, int]:
    x0 = max(0, min(size - 1, round(region[0] * size)))
    y0 = max(0, min(size - 1, round(region[1] * size)))
    x1 = max(x0 + 1, min(size, round(region[2] * size)))
    y1 = max(y0 + 1, min(size, round(region[3] * size)))
    return x0, y0, x1, y1


def curve_from_frames(
    frames: Iterable[bytes],
    *,
    fps: float = MOTION_FPS,
    size: int = MOTION_SIZE,
    region: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
) -> MotionCurve:
    """从原始灰度帧算运动曲线（纯函数，测试不需要 ffmpeg）。"""
    buffers = [buf for buf in frames if len(buf) >= size * size] if size > 0 else []
    if len(buffers) < 2:
        return MotionCurve(fps=fps, size=size, energy=[], region=region)

    x0, y0, x1, y1 = _region_bounds(size, region)
    pixels = (y1 - y0) * (x1 - x0)
    energy: list[float] = []
    for index in range(len(buffers) - 1):
        first, second = buffers[index], buffers[index + 1]
        total = 0
        for row in range(y0, y1):
            base = row * size
            total += sum(
                abs(first[base + col] - second[base + col]) for col in range(x0, x1)
            )
        energy.append(round(total / (pixels * 255.0), 6) if pixels else 0.0)
    return MotionCurve(fps=fps, size=size, energy=energy, region=region)


async def frame_bytes(
    video_path: Path,
    *,
    fps: float = MOTION_FPS,
    size: int = MOTION_SIZE,
    timeout_sec: float = 60.0,
) -> list[bytes]:
    """抽 `fps`×`size`×`size` 的灰度原始帧；失败返回空列表。"""
    if not ffmpeg_utils.ffmpeg_available():
        return []
    try:
        raw = await ffmpeg_utils.run_ffmpeg_bytes(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-vf",
                f"fps={fps:g},scale={size}:{size}:flags=area,format=gray",
                "-an",
                "-f",
                "rawvideo",
                "-",
            ],
            timeout_sec=timeout_sec,
        )
    except ffmpeg_utils.FFmpegError as exc:
        logger.debug("抽帧算运动失败 %s: %s", video_path, exc)
        return []
    stride = size * size
    return [raw[i : i + stride] for i in range(0, len(raw) - stride + 1, stride)]


async def analyze_clip(
    video_path: Path,
    *,
    fps: float = MOTION_FPS,
    size: int = MOTION_SIZE,
    subject_region: tuple[float, float, float, float] = SUBJECT_REGION,
    timeout_sec: float = 60.0,
) -> MotionAnalysis | None:
    """实测一个片段的运动；抽不到帧时返回 None（调用方按「运动未知」处理）。"""
    frames = await frame_bytes(video_path, fps=fps, size=size, timeout_sec=timeout_sec)
    if len(frames) < 2:
        return None
    return MotionAnalysis(
        fps=fps,
        size=size,
        frames=len(frames),
        overall=curve_from_frames(frames, fps=fps, size=size),
        subject=curve_from_frames(frames, fps=fps, size=size, region=subject_region),
    )


def load_cached_motion(cache_path: Path, video_path: Path) -> MotionAnalysis | None:
    """读回缓存的运动实测（片段没被改过才算数）。"""
    try:
        if not cache_path.is_file() or not video_path.is_file():
            return None
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stat = video_path.stat()
    if payload.get("source_size") != stat.st_size:
        return None
    if abs(float(payload.get("source_mtime", 0.0)) - stat.st_mtime) > 1e-6:
        return None
    return _analysis_from_summary(payload.get("motion") or {})


def save_motion(cache_path: Path, video_path: Path, analysis: MotionAnalysis) -> None:
    """把运动实测落盘（附源文件指纹，片段一变缓存自动失效）。"""
    try:
        stat = video_path.stat()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "source": video_path.name,
                    "source_size": stat.st_size,
                    "source_mtime": stat.st_mtime,
                    "description": analysis.describe(),
                    "motion": analysis.summary(),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("运动实测落盘失败: %s", cache_path, exc_info=True)


def _curve_from_summary(data: Any) -> MotionCurve:
    if not isinstance(data, dict):
        return MotionCurve(fps=MOTION_FPS, size=MOTION_SIZE)
    return MotionCurve(
        fps=float(data.get("fps", MOTION_FPS)),
        size=int(data.get("size", MOTION_SIZE)),
        energy=[float(v) for v in data.get("energy", [])],
    )


def _analysis_from_summary(data: Any) -> MotionAnalysis | None:
    if not isinstance(data, dict) or "overall" not in data or "subject" not in data:
        return None
    return MotionAnalysis(
        fps=float(data.get("fps", MOTION_FPS)),
        size=int(data.get("size", MOTION_SIZE)),
        frames=int(data.get("frames", 0)),
        overall=_curve_from_summary(data["overall"]),
        subject=_curve_from_summary(data["subject"]),
    )
