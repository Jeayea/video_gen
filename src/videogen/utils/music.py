"""本地配乐作曲与合成：把「这条片子该怎么配乐」写成谱，再合成成 WAV。

为什么自己写而不接一个音乐生成模型：

- MusicGen / Stable Audio 要 torch + GPU（和逐帧口型同一个理由，不进主环境）；
- 更关键的是我们要的不是「随机好听」，而是**跟着这条片子走**：
  段落边界对齐镜头切换、画面动作最猛的时刻给重音、有人说话的地方主动让路
  （去鼓、旋律避开说话窗口，混音时再叠一层人声闪避）。这些信息只存在于项目里，
  任何「给一句文字提示、等模型返回 30 秒音频」的接口都拿不到。
- 外部模型仍然可以接：`BGM_ENGINE=cli` + `BGM_CLI_CMD` 模板（见 services/music_service.py）。

这一层的产物是**干燥的**立体声 WAV（纯标准库合成，44.1kHz/16bit），
混响/动态/响度统一交给 ffmpeg（`afir` 卷积 + 压缩 + 限幅，都是 C 速度），
所以主环境一个第三方依赖都不加。
"""

from __future__ import annotations

import array
import logging
import math
import random
import time
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from videogen.models.music import MusicAccent, MusicChord, MusicPlan, MusicSection

logger = logging.getLogger(__name__)

__all__ = [
    "COLORS",
    "SR",
    "build_plan",
    "make_impulse_response",
    "note_to_freq",
    "pick_color",
    "render_plan",
    "write_wav",
]

#: 合成采样率（混音链路里统一重采样到 44100，这里保持一致）
SR = 44100
#: 音频尾巴：终止和弦自然衰减一点，ffmpeg 那一层还会做淡出
TAIL_SEC = 0.8
#: 混响脉冲响应的峰值（固定值：湿音增益要靠它可预期，见 make_impulse_response）
IR_PEAK = 0.6

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
#: 调式主音所在八度的 MIDI 号（C3 = 48）
_ROOT_MIDI = {"C": 48, "C#": 49, "D": 50, "D#": 51, "E": 52, "F": 53, "F#": 54, "G": 55,
              "G#": 56, "A": 57, "A#": 58, "B": 59}

#: 自然小调 / 大调 / 小调五声 / 大调五声（相对主音的半音）
_SCALES = {
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "major": (0, 2, 4, 5, 7, 9, 11),
    "penta_minor": (0, 3, 5, 7, 10),
    "penta_major": (0, 2, 4, 7, 9),
}


@dataclass(frozen=True, slots=True)
class Color:
    """一种情绪色调：调式 + 速度 + 配器 + 和弦走向。"""

    key: str
    label: str
    scale: str
    root: str
    bpm: int
    #: 基准能量 0..1（决定段落配器层次的起点）
    intensity: float
    #: 可用配器（从少到多）
    palette: tuple[str, ...]
    #: 和弦走向：相对主音的半音 + 性质
    progression: tuple[tuple[int, str], ...]
    #: 是否写主旋律
    lead: bool = True
    #: 混响湿量 0..1（空间感：悲伤/神秘更大，动作片更干）
    reverb: float = 0.4
    note: str = ""


#: 情绪 → 色调。关键词按「包含」匹配（「紧张的对峙」也能命中「紧张」）
_HINTS: dict[str, tuple[str, ...]] = {
    "tense": (
        "紧张", "压迫", "危险", "对峙", "冲突", "惊", "恐", "不安", "威胁",
        "追杀", "阴谋", "暗涌", "窒息", "焦虑", "危机",
    ),
    "sad": (
        "悲", "伤", "哭", "泪", "离别", "孤独", "失落", "压抑", "绝望", "痛", "思念",
        "遗憾", "忧郁", "沉重", "凄", "寂", "失去",
    ),
    "warm": (
        "温", "暖", "治愈", "日常", "家庭", "亲情", "甜蜜", "爱", "温柔", "幸福",
        "怀旧", "安", "平静", "轻松", "陪伴", "回忆",
    ),
    "hopeful": (
        "希望", "励志", "坚定", "成长", "释怀", "重逢", "勇", "光明", "振作", "觉醒",
        "决意", "向上", "挣脱", "和解",
    ),
    "action": (
        "激烈", "动作", "追逐", "打斗", "战斗", "爆发", "愤怒", "厮杀", "奔", "冲",
        "对抗", "反击", "爆炸", "拳", "逃", "拼命",
    ),
    "mystery": (
        "神秘", "诡异", "悬念", "悬疑", "未知", "梦", "幻", "秘密", "谜", "窥", "潜伏",
        "暗处", "低语", "仪式",
    ),
    "playful": (
        "轻快", "喜剧", "幽默", "活泼", "俏皮", "欢", "搞笑", "调侃", "打趣", "明快",
    ),
}

COLORS: dict[str, Color] = {
    "tense": Color(
        "tense", "紧张 / 悬疑", "minor", "D", 96, 0.62,
        ("pad", "piano", "bass", "perc", "strings", "lead"),
        ((0, "m"), (8, "M"), (5, "m"), (7, "M")),
        lead=True, reverb=0.35, note="小调 + 低音推进，鼓只给心跳式重音",
    ),
    "sad": Color(
        "sad", "悲伤 / 压抑", "minor", "A", 66, 0.30,
        ("pad", "piano", "strings", "bass", "bells", "lead"),
        ((0, "m"), (8, "M"), (3, "M"), (10, "M")),
        lead=True, reverb=0.62, note="慢速小调，钢琴与弦乐长音，留白多",
    ),
    "warm": Color(
        "warm", "温暖 / 日常", "major", "F", 82, 0.34,
        ("piano", "pad", "bells", "bass", "strings", "lead"),
        ((0, "M"), (7, "M"), (9, "m"), (5, "M")),
        lead=True, reverb=0.42, note="大调 I-V-vi-IV，钢琴为主，配器干净",
    ),
    "hopeful": Color(
        "hopeful", "希望 / 坚定", "major", "C", 92, 0.52,
        ("piano", "pad", "bass", "strings", "bells", "perc", "lead"),
        ((9, "m"), (5, "M"), (0, "M"), (7, "M")),
        lead=True, reverb=0.5, note="大调 vi-IV-I-V，弦乐渐强，结尾落在主和弦",
    ),
    "action": Color(
        "action", "激烈 / 动作", "minor", "E", 124, 0.86,
        ("perc", "bass", "pad", "strings", "piano", "lead"),
        ((0, "m"), (10, "M"), (8, "M"), (7, "M")),
        lead=True, reverb=0.2, note="快板小调，鼓与低音推进，配器顶满",
    ),
    "mystery": Color(
        "mystery", "神秘 / 悬念", "penta_minor", "B", 74, 0.28,
        ("pad", "bells", "piano", "bass", "strings", "lead"),
        ((0, "m"), (7, "m"), (5, "m"), (0, "m")),
        lead=True, reverb=0.7, note="五声 + 长混响，稀疏单音，尽量空",
    ),
    "playful": Color(
        "playful", "轻快 / 喜剧", "penta_major", "G", 112, 0.46,
        ("piano", "bells", "bass", "perc", "pad", "lead"),
        ((0, "M"), (9, "m"), (5, "M"), (7, "M")),
        lead=True, reverb=0.3, note="大调五声，跳音钢琴 + 轻打击",
    ),
    "neutral": Color(
        "neutral", "中性 / 铺底", "minor", "A", 84, 0.38,
        ("pad", "piano", "bass", "lead", "strings", "bells"),
        ((0, "m"), (10, "M"), (8, "M"), (10, "M")),
        lead=True, reverb=0.4, note="小调铺底，不抢戏",
    ),
}

#: 段落配器阶梯：能量 0..3 依次加层（再与色调的 palette 求交集）
_LADDER: dict[int, tuple[str, ...]] = {
    0: ("pad",),
    1: ("pad", "piano"),
    2: ("pad", "piano", "bass", "lead", "perc"),
    3: ("pad", "piano", "bass", "lead", "perc", "strings", "bells"),
}

_LAYER_LABELS = {
    "pad": "铺底 pad",
    "piano": "钢琴",
    "bass": "低音",
    "lead": "旋律",
    "perc": "鼓组",
    "strings": "弦乐",
    "bells": "铃/点音",
}


# ── 情绪 → 色调 ──────────────────────────────────────────


def _match_score(words: Sequence[str], moods: Sequence[str], note: str) -> float:
    """打分口径：**每个情绪标签最多贡献 1 分**，用户要求里的命中再补 0.5 分。

    早先按「命中关键词个数」打分，结果是「悲伤」同时命中「悲」和「伤」拿 2 分，
    压过了「紧张 + 激烈」各 1 分 —— 两个字的关键词被当成两个情绪，这是错的。
    """
    score = 0.0
    for mood in moods:
        text = str(mood).strip().lower()
        if text and any(word in text for word in words):
            score += 1.0
    if note and any(word in note.lower() for word in words):
        score += 0.5
    return score


def pick_color(moods: Sequence[str], *, note: str = "", override: str = "") -> Color:
    """按分镜情绪（+ 用户的配乐要求）挑一种色调。

    用户的自由文本也算进来：写「压抑但结尾要有希望」时，`sad` 与 `hopeful`
    都会得分，最后取分高的那个 —— 至少不会把「希望」两个字当没看见。
    """
    wanted = (override or "").strip().lower()
    if wanted in COLORS:
        return COLORS[wanted]
    # 允许 override 写中文关键词（例如「悬疑」）
    if wanted:
        for key, words in _HINTS.items():
            if any(word in wanted for word in words):
                return COLORS[key]

    unique: list[str] = []
    for mood in moods:
        text = str(mood).strip()
        if text and text not in unique:
            unique.append(text)
    if not unique and not (note or "").strip():
        return COLORS["neutral"]

    scored: list[tuple[float, str]] = []
    for key, words in _HINTS.items():
        score = _match_score(words, unique, note)
        if score > 0:
            scored.append((score, key))
    if not scored:
        return COLORS["neutral"]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return COLORS[scored[0][1]]


# ── 乐理小工具 ──────────────────────────────────────────


def note_to_freq(midi: float) -> float:
    """MIDI 号 → 频率（A4 = 440Hz = 69）。"""
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def _note_name(semitone: int) -> str:
    return _NOTE_NAMES[semitone % 12]


def chord_name(root_semitone: int, quality: str) -> str:
    name = _note_name(root_semitone)
    if quality == "M":
        return name
    if quality == "dim":
        return f"{name}dim"
    return f"{name}m"


def _chords(color: Color, root_midi: int, *, tonic_first: bool) -> list[MusicChord]:
    """把色调的走向展开成和弦对象（带人话名）。"""
    degrees = color.progression
    if tonic_first:
        degrees = tuple(sorted(degrees, key=lambda item: item[0] != 0))
    return [
        MusicChord(
            root=semitone,
            quality=quality,
            name=chord_name(root_midi + semitone, quality),
        )
        for semitone, quality in degrees
    ]


# ── 分镜 → 段落 ──────────────────────────────────────────


@dataclass(slots=True)
class _Shot:
    id: str
    scene_id: str
    start: float
    duration: float
    mood: str
    dialogue: bool
    energy: float
    burst_at: float | None = None
    burst_note: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration


def _curve_of(motion: Any) -> Any | None:
    if motion is None:
        return None
    subject = getattr(motion, "subject", None)
    overall = getattr(motion, "overall", None)
    if subject is not None and not getattr(subject, "dead", True):
        return subject
    return overall


def _shot_energy(motion: Any, base: float, shot_color: Color) -> float:
    """一个镜头的能量：画面运动为主，情绪色调为辅。

    运动口径沿用 `utils/motion.py`：0..1 的「相邻帧平均绝对差 / 255」，
    实测常见区间是 0.01（几乎静止）~0.08（剧烈）。
    """
    level = (base + shot_color.intensity) / 2.0
    curve = _curve_of(motion)
    if curve is None or not getattr(curve, "energy", None):
        return min(1.0, level)
    moving = min(1.0, (curve.mean() / 0.06) * 0.7 + (curve.peak / 0.12) * 0.3)
    return min(1.0, 0.35 * level + 0.65 * moving)


def _collect_shots(
    shots: Sequence[Any],
    motions: Mapping[str, Any] | None,
    speech_windows: Sequence[tuple[float, float]],
    base_color: Color,
) -> list[_Shot]:
    collected: list[_Shot] = []
    clock = 0.0
    for shot in shots:
        duration = max(0.2, float(getattr(shot, "duration_sec", 0) or 0))
        mood = str(getattr(shot, "mood", "") or "")
        dialogue = bool(str(getattr(shot, "dialogue_text", "") or "").strip())
        shot_color = pick_color([mood]) if mood else base_color
        motion = (motions or {}).get(str(getattr(shot, "id", "")))
        item = _Shot(
            id=str(getattr(shot, "id", "")),
            scene_id=str(getattr(shot, "scene_id", "") or ""),
            start=round(clock, 3),
            duration=duration,
            mood=mood,
            dialogue=dialogue,
            energy=_shot_energy(motion, base_color.intensity, shot_color),
        )
        curve = _curve_of(motion)
        if curve is not None:
            bursts = curve.bursts(limit=1)
            if bursts:
                span = bursts[0]
                # 爆发时刻落在镜头内（曲线从第 1 帧开始算，加回镜头起点）
                if span.start < duration:
                    item.burst_at = round(clock + span.start, 3)
                    item.burst_note = span.level
        collected.append(item)
        clock += duration

    # 说话窗口也要影响「这段是不是在说话」
    for item in collected:
        for start, end in speech_windows:
            if start < item.end - 0.15 and end > item.start + 0.15:
                item.dialogue = True
                break
    return collected


def _group_sections(shots: list[_Shot]) -> list[list[_Shot]]:
    """把镜头分组成段落：场景边界、能量跳变、长度上限都要切。"""
    if not shots:
        return []
    groups: list[list[_Shot]] = [[shots[0]]]
    for shot in shots[1:]:
        current = groups[-1]
        length = sum(item.duration for item in current)
        same_scene = not shot.scene_id or shot.scene_id == current[-1].scene_id
        average = sum(item.energy for item in current) / len(current)
        jump = abs(shot.energy - average) >= 0.26
        if (not same_scene) or (length >= 6.0 and jump) or length >= 26.0:
            groups.append([shot])
        else:
            current.append(shot)

    # 太短的尾巴并回上一段（单独 3 秒一个段落听起来像故障）
    merged: list[list[_Shot]] = []
    for group in groups:
        length = sum(item.duration for item in group)
        if merged and length < 3.5:
            merged[-1].extend(group)
        else:
            merged.append(group)
    return merged


def _regroup(items: list[_Shot], count: int) -> list[list[_Shot]]:
    """把镜头按总时长尽量等分地切成 `count` 段（切点落在镜头边界上）。

    LLM 给的段落建议是「3-6 段」这种叙事分段，时间轴仍然由镜头决定。
    """
    if count <= 1 or not items:
        return [items] if items else []
    total = sum(item.duration for item in items)
    groups: list[list[_Shot]] = []
    current: list[_Shot] = []
    clock = 0.0
    target = total / count
    for index, item in enumerate(items):
        current.append(item)
        clock += item.duration
        remaining_groups = count - len(groups) - 1
        remaining_shots = len(items) - index - 1
        if remaining_groups <= 0:
            continue
        # 到目标时切一刀，但必须给后面的段留够镜头
        if clock >= target * (len(groups) + 1) and remaining_shots >= remaining_groups:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _split_for_shape(groups: list[list[_Shot]], total: float) -> list[list[_Shot]]:
    """段落太少时继续切：再短的片子也该有「起 → 落」两个形状。

    只切不并：场景边界切出来的段落不会被这里合回去。
    """
    target = 1 if total < 12 else (2 if total < 30 else 3)
    result = [list(group) for group in groups]
    while len(result) < target:
        candidates = [index for index, group in enumerate(result) if len(group) > 1]
        if not candidates:
            break
        longest = max(candidates, key=lambda index: sum(item.duration for item in result[index]))
        group = result[longest]
        half = sum(item.duration for item in group) / 2
        clock = 0.0
        best = 1
        best_gap = None
        for index in range(1, len(group)):
            clock += group[index - 1].duration
            gap = abs(clock - half)
            if best_gap is None or gap < best_gap:
                best_gap, best = gap, index
        result[longest : longest + 1] = [group[:best], group[best:]]
    return result


def _section_label(index: int, total: int, energy: int, peak_index: int) -> str:
    if total > 1 and index == 0:
        return "intro"
    if total > 2 and index == total - 1 and energy <= 1:
        return "resolve"
    if index == peak_index and energy >= 2:
        return "climax"
    if energy >= 2:
        return "build"
    return "bed"


def _layers_for(color: Color, energy: int, *, dialogue: bool) -> list[str]:
    """能量 → 配器层次；有人说话就自动让路（去鼓、去点音）。"""
    palette = set(color.palette)
    layers = [name for name in _LADDER[max(0, min(3, energy))] if name in palette]
    if dialogue:
        layers = [name for name in layers if name not in {"perc", "bells"}]
    return layers


# ── 计划 ──────────────────────────────────────────────


def _read_hints(section_hint: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """把 LLM 的配乐建议收拾成能用的小字段（脏数据一律忽略，绝不因此崩）。"""
    result: dict[str, Any] = {
        "color": "",
        "moods": [],
        "levels": {},
        "source": "",
        "engine": "",
    }
    if not section_hint:
        return result
    result["source"] = "llm"
    for index, item in enumerate(section_hint):
        if not isinstance(item, Mapping):
            continue
        color = str(item.get("color", "") or "").strip().lower()
        if color in COLORS and not result["color"]:
            result["color"] = color
        mood = str(item.get("mood", "") or "").strip()
        if mood:
            result["moods"].append(mood)
        energy = item.get("energy")
        if isinstance(energy, (int, float)) and not isinstance(energy, bool):
            value = float(energy)
            if value <= 1.0:
                value *= 3.0  # LLM 常给 0..1，也允许 0..3
            result["levels"][index] = round(max(0.0, min(3.0, value)))
    return result


#: 能合成的速度范围（太慢拍子拖成一片，太快音符糊在一起）
_MIN_BPM = 40
_MAX_BPM = 180


def build_plan(
    *,
    shots: Sequence[Any] = (),
    speech_windows: Sequence[tuple[float, float]] = (),
    motions: Mapping[str, Any] | None = None,
    duration_sec: float = 0.0,
    seed: int = 0,
    moods: Sequence[str] = (),
    note: str = "",
    color: str = "",
    bpm: int = 0,
    key: str = "",
    section_hint: Sequence[Mapping[str, Any]] | None = None,
    source: str = "heuristic",
) -> MusicPlan:
    """从分镜/运动/台词窗口写出配乐谱。

    全部是确定性的（`seed` 固定 → 谱固定），不联网、不花钱。
    `section_hint` 是 LLM 给的段落建议（可选）：只借用它的色调/情绪/能量，
    时间轴仍然由镜头决定 —— 音乐必须贴着画面，不能贴着 LLM 的想象。
    """
    hints = _read_hints(section_hint)
    all_moods = [*moods, *hints["moods"]]
    base_color = pick_color(all_moods, note=note, override=color or hints["color"])
    input_notes: list[str] = []
    key_root = key.strip().upper() or base_color.root
    if key_root not in _ROOT_MIDI:
        input_notes.append(f"不认识的调「{key}」，改用色调默认的 {base_color.root}")
        key_root = base_color.root
    root_midi = _ROOT_MIDI[key_root]
    tempo = base_color.bpm
    if bpm:
        # 超出能合成的范围时夹到边上并说明（以前是静默忽略，用户以为生效了）
        tempo = min(_MAX_BPM, max(_MIN_BPM, int(bpm)))
        if tempo != int(bpm):
            input_notes.append(f"BPM {bpm} 超出 {_MIN_BPM}~{_MAX_BPM}，按 {tempo} 写")
    bar_sec = round(4 * 60.0 / tempo, 3)

    items = _collect_shots(shots, motions, speech_windows, base_color)
    total = duration_sec or (items[-1].end if items else 0.0)
    if total <= 0:
        total = 30.0

    groups = _group_sections(items)
    if hints["levels"] and max(hints["levels"]) + 1 > 1 and items:
        groups = _regroup(items, max(hints["levels"]) + 1)
    elif groups:
        groups = _split_for_shape(groups, total)
    if not groups:
        # 没有分镜（例如手动指定时长）：给一段同色调的铺底
        groups = [[]]

    energies = [
        (sum(item.energy for item in group) / len(group)) if group else 0.4 for group in groups
    ]
    levels = [max(0, min(3, round(value * 3))) for value in energies]
    peak_index = levels.index(max(levels))

    sections: list[MusicSection] = []
    accents: list[MusicAccent] = []
    for index, group in enumerate(groups):
        start = group[0].start if group else 0.0
        end = group[-1].end if group else total
        if index == len(groups) - 1:
            end = max(end, total)
        level = levels[index]
        if index in hints["levels"]:
            level = max(0, min(3, hints["levels"][index]))
        # 真实能量（重音用）与配器层次（开场要薄）是两回事：开场把配器压薄，
        # 但画面动作爆发的重音照给 —— 否则「开场 3 秒就有一拳」会被吃掉
        accent_level = level
        layer_level = min(level, 1) if index == 0 else level
        label = _section_label(index, len(groups), level, peak_index)
        if label == "resolve":
            layer_level = max(1, layer_level)
        dialogue = any(item.dialogue for item in group)
        layers = _layers_for(base_color, layer_level, dialogue=dialogue)
        tonic_first = label in {"intro", "resolve"}
        progression = _chords(base_color, root_midi, tonic_first=tonic_first)
        reasons: list[str] = []
        if label == "intro":
            reasons.append("开场，配器从薄开始")
        if label == "climax":
            reasons.append("全片能量最高")
        if index in hints["levels"]:
            reasons.append("能量参考了 LLM 的配乐建议")
        if dialogue:
            reasons.append("这段有人说话，去掉鼓与点音让路")
        if group and group[0].energy >= 0.7:
            reasons.append("画面运动强")
        sections.append(
            MusicSection(
                index=index,
                label=label,
                start_sec=round(start, 3),
                end_sec=round(end, 3),
                energy=layer_level,
                layers=layers,
                progression=progression,
                dialogue=dialogue,
                shot_ids=[item.id for item in group],
                reason="；".join(reasons),
            )
        )

        if index > 0:
            accents.append(
                MusicAccent(
                    at_sec=round(start, 3),
                    kind="crash" if level >= 2 else "stab",
                    reason=f"段落「{label}」开始（镜头切换）",
                )
            )
        for item in group:
            if accent_level >= 2 and item.burst_at is not None:
                accents.append(
                    MusicAccent(
                        at_sec=item.burst_at,
                        kind="hit",
                        reason=f"镜头 {item.id} 画面动作{item.burst_note}最猛的一刻",
                    )
                )

    # 重音去重：0.4 秒内只留第一个（密集重音听起来像故障）
    kept: list[MusicAccent] = []
    for accent in sorted(accents, key=lambda item: item.at_sec):
        if kept and accent.at_sec - kept[-1].at_sec < 0.4:
            continue
        if any(start <= accent.at_sec <= end for start, end in speech_windows):
            continue  # 说话的时候别砸鼓
        kept.append(accent)

    plan = MusicPlan(
        seed=seed,
        engine=hints["engine"] or "local-synth",
        source=hints["source"] or source,
        color=base_color.key,
        color_label=base_color.label,
        key_name=f"{key_root} {_scale_label(base_color.scale)}",
        bpm=tempo,
        bar_sec=bar_sec,
        duration_sec=round(total, 3),
        mood_tags=[str(mood) for mood in all_moods if str(mood).strip()],
        layers=sorted({name for section in sections for name in section.layers}),
        sections=sections,
        accents=kept,
        speech_windows=[(round(start, 3), round(end, 3)) for start, end in speech_windows],
        notes=[
            *input_notes,
            *_plan_notes(base_color, sections, kept, speech_windows, note, motions),
        ],
    )
    if section_hint:
        plan.notes.append("段落情绪参考了 LLM 的配乐建议（时间轴仍按镜头切）")
    logger.info(
        "配乐谱：%s / %s / %d BPM / %d 段 / %d 个重音（%.1fs）",
        base_color.label,
        plan.key_name,
        plan.bpm,
        len(sections),
        len(kept),
        plan.duration_sec,
    )
    return plan


def _scale_label(scale: str) -> str:
    return {
        "minor": "小调",
        "major": "大调",
        "penta_minor": "小调五声",
        "penta_major": "大调五声",
    }.get(scale, scale)


def _plan_notes(
    color: Color,
    sections: list[MusicSection],
    accents: list[MusicAccent],
    speech_windows: Sequence[tuple[float, float]],
    note: str,
    motions: Mapping[str, Any] | None,
) -> list[str]:
    """给 UI 看的人话说明：为什么这样配。"""
    lines = [f"情绪：{color.label} → {color.note}（{color.bpm} BPM 基准）"]
    if note.strip():
        lines.append(f"用户的配乐要求：{note.strip()}")
    plan_text = "；".join(
        f"{section.label} {section.start_sec:.0f}–{section.end_sec:.0f}s"
        f"（能量 {section.energy}，{'、'.join(_LAYER_LABELS[x] for x in section.layers)}）"
        for section in sections
    )
    lines.append(f"段落：{plan_text}")
    hit_count = sum(1 for accent in accents if accent.kind == "hit")
    lines.append(
        f"重音 {len(accents)} 处，其中 {hit_count} 处落在画面动作最猛的时刻"
        "（对齐镜头切换与运动爆发，不是均匀打拍子）"
    )
    if speech_windows:
        lines.append(
            f"{len(speech_windows)} 段台词：这些段落自动去掉鼓与点音，"
            "旋律避开说话窗口，混音时人声再压一层音乐（闪避）"
        )
    measured = sum(1 for motion in (motions or {}).values() if motion is not None)
    if measured:
        lines.append(f"依据 {measured} 个镜头的画面运动实测（本地计算，免费）")
    else:
        lines.append("没有画面运动实测，本次只按情绪与镜头时长配（装了 ffmpeg 后会更贴画面）")
    return lines


# ── 合成：音色 ──────────────────────────────────────────

_TABLE_BITS = 12
_TABLE_SIZE = 1 << _TABLE_BITS
_TABLE_MASK = _TABLE_SIZE - 1


@dataclass(frozen=True, slots=True)
class Instrument:
    """一个合成音色：谐波表 + 包络。"""

    name: str
    harmonics: tuple[float, ...]
    attack: float = 0.01
    #: >0 = 指数衰减（秒，约 -26dB），用于钢琴/低音/铃
    decay: float = 0.0
    #: decay=0 时的延音电平
    sustain: float = 1.0
    release: float = 0.12
    #: 双声部失谐比例（做宽 pad / 弦乐）
    detune: float = 0.0
    #: 颤音深度（比例）与频率
    vibrato: float = 0.0
    vibrato_hz: float = 5.2


INSTRUMENTS: dict[str, Instrument] = {
    "pad": Instrument(
        "pad", (1.0, 0.5, 0.24, 0.11, 0.06, 0.03), attack=0.85, sustain=0.8,
        release=1.5, detune=0.0016,
    ),
    "strings": Instrument(
        "strings", (1.0, 0.62, 0.4, 0.26, 0.17, 0.11, 0.07), attack=0.6, sustain=0.85,
        release=1.1, detune=0.0022, vibrato=0.0035,
    ),
    "piano": Instrument(
        "piano", (1.0, 0.5, 0.28, 0.14, 0.07, 0.03), attack=0.005, decay=1.5,
        release=0.3,
    ),
    "bass": Instrument("bass", (1.0, 0.3, 0.12), attack=0.012, decay=1.0, release=0.25),
    "bells": Instrument(
        "bells", (1.0, 0.12, 0.5, 0.1, 0.3, 0.06), attack=0.003, decay=1.4, release=0.6,
    ),
    "lead": Instrument(
        "lead", (1.0, 0.42, 0.2, 0.09, 0.05), attack=0.05, sustain=0.62, release=0.5,
        detune=0.001, vibrato=0.004,
    ),
    "stab": Instrument("stab", (1.0, 0.6, 0.34, 0.18), attack=0.004, decay=0.3, release=0.15),
}

_tables: dict[str, array.array] = {}


def _table(inst: Instrument) -> array.array:
    """音色波表（一个周期），缓存复用。"""
    cached = _tables.get(inst.name)
    if cached is not None:
        return cached
    total = sum(abs(value) for value in inst.harmonics) or 1.0
    table = array.array("d", [0.0] * _TABLE_SIZE)
    for harmonic, amplitude in enumerate(inst.harmonics, start=1):
        if amplitude == 0:
            continue
        step = 2.0 * math.pi * harmonic / _TABLE_SIZE
        for index in range(_TABLE_SIZE):
            table[index] += amplitude * math.sin(step * index)
    for index in range(_TABLE_SIZE):
        table[index] /= total
    _tables[inst.name] = table
    return table


def _envelope(inst: Instrument, count: int) -> list[float]:
    """包络：起音 → （指数衰减 | 延音）→ 尾部淡出。"""
    attack = max(1, int(inst.attack * SR))
    release = max(0, int(inst.release * SR))
    env = [0.0] * count
    if inst.decay > 0:
        k = 3.0 / max(1e-3, inst.decay * SR)
        for index in range(count):
            value = math.exp(-k * index)
            if index < attack:
                value *= index / attack
            env[index] = value
    else:
        for index in range(count):
            env[index] = (index / attack) if index < attack else inst.sustain
    span = min(release, count)
    for offset in range(span):
        env[count - 1 - offset] *= offset / span
    return env


def _pan_gains(pan: float) -> tuple[float, float]:
    angle = (max(-1.0, min(1.0, pan)) + 1.0) * math.pi / 4.0
    return math.cos(angle), math.sin(angle)


def _add_tone(
    left: array.array,
    right: array.array,
    *,
    start_sec: float,
    dur_sec: float,
    freq: float,
    amp: float,
    inst: Instrument,
    pan: float = 0.0,
) -> None:
    """把一段音加进左右声道缓冲（相位累加 + 波表，避免每采样算 sin）。"""
    total = len(left)
    start = int(start_sec * SR)
    if start >= total or start < 0 or dur_sec <= 0 or freq <= 0 or amp <= 0:
        return
    count = min(int(dur_sec * SR), total - start)
    if count < 16:
        return

    table = _table(inst)
    env = _envelope(inst, count)
    step = freq * _TABLE_SIZE / SR
    step_b = step * (1.0 + inst.detune) if inst.detune else 0.0
    phase = 0.0
    phase_b = 0.0
    gain_l, gain_r = _pan_gains(pan)
    amp_l = amp * gain_l
    amp_r = amp * gain_r
    vibrato = inst.vibrato
    vib_w = 2.0 * math.pi * inst.vibrato_hz / SR

    for index in range(count):
        sample = table[int(phase) & _TABLE_MASK]
        if step_b:
            sample = (sample + table[int(phase_b) & _TABLE_MASK]) * 0.5
        level = sample * env[index]
        left[start + index] += level * amp_l
        right[start + index] += level * amp_r
        if vibrato:
            rate = 1.0 + vibrato * math.sin(vib_w * index)
            phase += step * rate
            if step_b:
                phase_b += step_b * rate
        else:
            phase += step
            if step_b:
                phase_b += step_b


#: 打击乐用独立随机流（不与作曲的随机流相互影响，保证可复现）
_RNG = random.Random(20260912)


def _add_kick(left: array.array, right: array.array, at: float, amp: float = 0.34) -> None:
    """底鼓：频率从 96Hz 扫到 44Hz（动作爆发的重音）。"""
    total = len(left)
    start = int(at * SR)
    count = min(int(0.34 * SR), total - start)
    if count <= 0 or start < 0:
        return
    phase = 0.0
    for index in range(count):
        time = index / SR
        freq = 44.0 + (96.0 - 44.0) * math.exp(-time / 0.03)
        value = math.sin(phase) * math.exp(-time / 0.11) * amp
        phase += 2.0 * math.pi * freq / SR
        left[start + index] += value
        right[start + index] += value


def _add_hit(left: array.array, right: array.array, at: float, amp: float = 0.26) -> None:
    """画面动作爆发的重音：低频冲击 + 一点噪声。"""
    _add_kick(left, right, at, amp=amp)
    total = len(left)
    start = int(at * SR)
    count = min(int(0.5 * SR), total - start)
    if count <= 0:
        return
    for index in range(count):
        time = index / SR
        noise = (_RNG.random() * 2.0 - 1.0) * math.exp(-time / 0.06) * amp * 0.5
        left[start + index] += noise
        right[start + index] += noise


def _add_noise_perc(
    left: array.array,
    right: array.array,
    at: float,
    *,
    dur: float,
    amp: float,
    decay: float,
    bright: float,
) -> None:
    """噪声类打击（军鼓/踩镲/镲片）：`bright` 越大高频越多。"""
    total = len(left)
    start = int(at * SR)
    count = min(int(dur * SR), total - start)
    if count <= 0 or start < 0:
        return
    lowpass = 0.0
    for index in range(count):
        time = index / SR
        raw = _RNG.random() * 2.0 - 1.0
        lowpass += bright * (raw - lowpass)
        value = (raw - lowpass * 0.7) * math.exp(-time / decay) * amp
        left[start + index] += value
        right[start + index] += value


def _add_snare(left: array.array, right: array.array, at: float, amp: float = 0.16) -> None:
    _add_noise_perc(left, right, at, dur=0.22, amp=amp, decay=0.07, bright=0.45)
    total = len(left)
    start = int(at * SR)
    count = min(int(0.12 * SR), total - start)
    if count <= 0:
        return
    phase = 0.0
    for index in range(count):
        time = index / SR
        value = math.sin(phase) * math.exp(-time / 0.045) * amp * 0.7
        phase += 2.0 * math.pi * 190.0 / SR
        left[start + index] += value
        right[start + index] += value


def _add_hat(left: array.array, right: array.array, at: float, amp: float = 0.05) -> None:
    _add_noise_perc(left, right, at, dur=0.09, amp=amp, decay=0.02, bright=0.75)


def _add_crash(left: array.array, right: array.array, at: float, amp: float = 0.16) -> None:
    _add_noise_perc(left, right, at, dur=1.6, amp=amp, decay=0.5, bright=0.8)


def _add_riser(left: array.array, right: array.array, at: float, dur: float = 1.8) -> None:
    """推进器：噪声渐强 + 越来越亮（进高潮前的抬升）。"""
    total = len(left)
    start = int(at * SR)
    count = min(int(dur * SR), total - start)
    if count <= 0 or start < 0:
        return
    lowpass = 0.0
    for index in range(count):
        progress = index / count
        raw = _RNG.random() * 2.0 - 1.0
        coefficient = 0.02 + 0.5 * progress
        lowpass += coefficient * (raw - lowpass)
        value = (raw - lowpass) * (progress**2) * 0.13
        left[start + index] += value
        right[start + index] += value


# ── 合成：段落排版 ──────────────────────────────────────


def _pad_voicing(root_midi: int, chord: MusicChord, energy: int) -> list[int]:
    """开放排列：根音 + 五度 + 三度（高八度），能量高时再补根音八度。"""
    root, third, fifth = chord.intervals()
    notes = [root_midi + root, root_midi + root + fifth, root_midi + root + third + 12]
    if energy >= 3:
        notes.append(root_midi + root + 12)
    return notes


def _render_section(
    left: array.array,
    right: array.array,
    plan: MusicPlan,
    section: MusicSection,
    color: Color,
    root_midi: int,
    rng: random.Random,
) -> int:
    """渲染一个段落，返回播放的音符数。"""
    beat = 60.0 / plan.bpm
    bar = beat * 4
    start = section.start_sec
    end = min(section.end_sec, plan.duration_sec + TAIL_SEC)
    length = max(beat, end - start)
    progression = section.progression or _chords(color, root_midi, tonic_first=True)
    bars_per_chord = 2 if plan.bpm <= 84 else 1
    bars = max(1, math.ceil(length / bar))
    layers = set(section.layers)
    played = 0

    def chord_at(bar_index: int) -> MusicChord:
        return progression[(bar_index // bars_per_chord) % len(progression)]

    # 铺底：每个和弦一个长音（含前面一段的尾巴，让和弦之间连起来）
    if "pad" in layers:
        for bar_index in range(bars):
            if bar_index % bars_per_chord:
                continue
            chord = chord_at(bar_index)
            at = start + bar_index * bar
            dur = min(bar * bars_per_chord + 0.35, plan.duration_sec + TAIL_SEC - at)
            amp = 0.085 if section.energy <= 1 else 0.1
            for note in _pad_voicing(root_midi, chord, section.energy):
                _add_tone(
                    left, right,
                    start_sec=at, dur_sec=dur, freq=note_to_freq(note), amp=amp,
                    inst=INSTRUMENTS["pad"], pan=rng.uniform(-0.35, 0.35),
                )
                played += 1

    # 钢琴：分解和弦（能量越高越密）
    if "piano" in layers:
        pattern = (0.0, 2.0) if section.energy <= 1 else (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5)
        for bar_index in range(bars):
            chord = chord_at(bar_index)
            voicing = _pad_voicing(root_midi + 12, chord, 1)
            for step_index, offset in enumerate(pattern):
                at = start + bar_index * bar + offset * beat
                if at >= plan.duration_sec + TAIL_SEC:
                    break
                note = voicing[(step_index + bar_index) % len(voicing)]
                if step_index % 4 == 3:
                    note += 12 if section.energy >= 2 else 0
                _add_tone(
                    left, right,
                    start_sec=at, dur_sec=min(beat * 1.6, plan.duration_sec + TAIL_SEC - at),
                    freq=note_to_freq(note),
                    amp=0.062 if section.energy <= 1 else 0.052,
                    inst=INSTRUMENTS["piano"], pan=-0.16,
                )
                played += 1

    # 低音：根音
    if "bass" in layers:
        offsets = (0.0,) if plan.bpm <= 84 else (0.0, 2.5)
        if section.energy >= 3:
            offsets = (0.0, 1.5, 2.0, 3.5)
        for bar_index in range(bars):
            chord = chord_at(bar_index)
            note = root_midi - 24 + chord.root
            for offset in offsets:
                at = start + bar_index * bar + offset * beat
                if at >= plan.duration_sec + TAIL_SEC:
                    break
                _add_tone(
                    left, right,
                    start_sec=at,
                    dur_sec=min(beat * (1.8 if plan.bpm <= 84 else 0.9),
                                plan.duration_sec + TAIL_SEC - at),
                    freq=note_to_freq(note), amp=0.14, inst=INSTRUMENTS["bass"],
                )
                played += 1

    # 弦乐：整段长音铺在底下（只有高潮/大场景才有）
    if "strings" in layers:
        chord = progression[0]
        for note in _pad_voicing(root_midi, chord, 1)[:2]:
            _add_tone(
                left, right,
                start_sec=start, dur_sec=length, freq=note_to_freq(note + 12),
                amp=0.045, inst=INSTRUMENTS["strings"], pan=rng.uniform(-0.5, 0.5),
            )
            played += 1

    # 铃/点音：两小节一次的高音点
    if "bells" in layers:
        for bar_index in range(0, bars, 2):
            chord = chord_at(bar_index)
            note = root_midi + 24 + chord.root + chord.intervals()[2]
            at = start + bar_index * bar
            _add_tone(
                left, right,
                start_sec=at, dur_sec=min(2.0, plan.duration_sec + TAIL_SEC - at),
                freq=note_to_freq(note), amp=0.05, inst=INSTRUMENTS["bells"], pan=0.3,
            )
            played += 1

    # 主旋律
    if "lead" in layers and color.lead:
        played += _render_melody(
            left, right, plan, section, color, root_midi, rng,
            bars=bars, bar=bar, beat=beat, start=start, chord_at=chord_at,
        )

    # 鼓组
    if "perc" in layers:
        for bar_index in range(bars):
            bar_start = start + bar_index * bar
            kicks = (0.0, 2.0) if section.energy == 2 else (0.0, 1.5, 2.0)
            snares = (2.0,) if section.energy == 2 else (1.0, 3.0)
            for offset in kicks:
                _add_kick(left, right, bar_start + offset * beat, amp=0.3)
            if section.energy >= 3:
                for offset in snares:
                    _add_snare(left, right, bar_start + offset * beat, amp=0.15)
            for step in range(8):
                at = bar_start + step * beat / 2
                if at >= plan.duration_sec + TAIL_SEC:
                    break
                _add_hat(left, right, at, amp=0.055 if step % 2 else 0.035)
            played += 1

    return played


def _render_melody(
    left: array.array,
    right: array.array,
    plan: MusicPlan,
    section: MusicSection,
    color: Color,
    root_midi: int,
    rng: random.Random,
    *,
    bars: int,
    bar: float,
    beat: float,
    start: float,
    chord_at: Any,
) -> int:
    """写一条主旋律：动机重复（AABA），说话窗口自动避开。"""
    scale = _SCALES.get(color.scale, _SCALES["minor"])
    base = root_midi + 12
    patterns = (
        (0.0, 1.0, 1.5, 2.0, 2.5, 3.0),
        (0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.5),
        (0.0, 1.0, 2.0, 3.0),
        (0.5, 1.0, 2.0, 2.5, 3.5),
    )
    speech = plan.speech_windows
    played = 0
    previous = len(scale) // 2
    for phrase in range(0, bars, 2):
        pattern = patterns[rng.randrange(len(patterns))]
        transpose = 0 if phrase % 4 == 0 else rng.choice((-1, 0, 1))
        for step_index, offset in enumerate(pattern):
            at = start + phrase * bar + offset * beat
            if at >= plan.duration_sec + TAIL_SEC:
                break
            if any(
                window_start - 0.2 <= at <= window_end + 0.05
                for window_start, window_end in speech
            ):
                continue
            chord = chord_at(phrase)
            candidates = _melody_candidates(scale, chord, previous)
            degree = candidates[rng.randrange(len(candidates))]
            previous = degree
            shifted = degree + transpose
            semitone = scale[shifted % len(scale)] + 12 * (shifted // len(scale))
            note = base + semitone
            length = beat * (1.0 if step_index % 3 else 1.8)
            _add_tone(
                left, right,
                start_sec=at, dur_sec=min(length, plan.duration_sec + TAIL_SEC - at),
                freq=note_to_freq(note), amp=0.075 if section.energy >= 2 else 0.055,
                inst=INSTRUMENTS["lead"], pan=0.18,
            )
            played += 1
    return played


def _melody_candidates(scale: tuple[int, ...], chord: MusicChord, previous: int) -> list[int]:
    """候选音级：优先和弦音，其次就近级进。"""
    _, third, fifth = chord.intervals()
    chord_semitones = {chord.root % 12, (chord.root + third) % 12, (chord.root + fifth) % 12}
    ordered = [degree for degree in range(len(scale)) if scale[degree] in chord_semitones]
    near = [degree for degree in range(len(scale)) if abs(degree - previous) <= 2]
    unique: list[int] = []
    for degree in [*ordered, *near, previous]:
        if degree not in unique:
            unique.append(degree)
    return unique or [previous]


def render_plan(
    plan: MusicPlan,
    path: Path,
    *,
    sr: int = SR,
    color: Color | None = None,
) -> dict[str, Any]:
    """把配乐谱合成成 16bit 立体声 WAV（干燥，混响在 ffmpeg 那层加）。"""
    if sr != SR:
        raise ValueError(f"只支持 {SR}Hz 渲染（当前 {sr}）")
    started = time.time()
    color = color or COLORS.get(plan.color, COLORS["neutral"])
    root_midi = _ROOT_MIDI.get(plan.key_name.split(" ")[0].strip(), _ROOT_MIDI[color.root])
    total = max(1.0, plan.duration_sec + TAIL_SEC)
    left = array.array("d", bytes(8 * int(total * sr)))
    right = array.array("d", bytes(8 * int(total * sr)))

    rng = random.Random(f"{plan.seed}:render")
    played = 0
    for section in plan.sections:
        played += _render_section(left, right, plan, section, color, root_midi, rng)

    for accent in plan.accents:
        if accent.kind == "crash":
            _add_crash(left, right, accent.at_sec)
        elif accent.kind == "stab":
            section = _section_at(plan, accent.at_sec)
            chord = (section.progression or _chords(color, root_midi, tonic_first=True))[0]
            for note in _pad_voicing(root_midi + 12, chord, 1):
                _add_tone(
                    left, right,
                    start_sec=accent.at_sec, dur_sec=0.8, freq=note_to_freq(note),
                    amp=0.09, inst=INSTRUMENTS["stab"], pan=rng.uniform(-0.15, 0.15),
                )
                played += 1
        else:  # hit / kick
            _add_hit(left, right, accent.at_sec)

    # 进高潮前的抬升
    for index, section in enumerate(plan.sections):
        if section.label != "climax" or index == 0:
            continue
        _add_riser(left, right, max(0.0, section.start_sec - 1.8), dur=1.8)

    peaks = _master(left, right, path)
    elapsed = round(time.time() - started, 2)
    stats = {
        "sample_rate": sr,
        "duration_sec": round(total, 3),
        "notes": played,
        "accents": len(plan.accents),
        "peak": round(peaks[0], 4),
        "rms_db": round(peaks[1], 2),
        "bytes": path.stat().st_size if path.is_file() else 0,
        "elapsed_sec": elapsed,
    }
    logger.info(
        "配乐合成完成：%.1fs / %d 个音符 / 峰值 %.3f / RMS %.1f dBFS / %.1fs",
        total,
        played,
        peaks[0],
        peaks[1],
        elapsed,
    )
    return stats


def _section_at(plan: MusicPlan, at: float) -> MusicSection:
    for section in plan.sections:
        if section.start_sec <= at <= section.end_sec:
            return section
    return plan.sections[0] if plan.sections else MusicSection()


def _master(left: array.array, right: array.array, path: Path) -> tuple[float, float]:
    """归一化 + 限幅 + 交错写盘；返回（峰值, RMS dBFS）。"""
    peak = 0.0
    for index in range(len(left)):
        value = abs(left[index])
        if value > peak:
            peak = value
        value = abs(right[index])
        if value > peak:
            peak = value
    scale = (0.89 / peak) if peak > 0.89 else 1.0
    squares = 0.0
    frames = array.array("h", bytes(4 * len(left)))
    for index in range(len(left)):
        value_l = max(-1.0, min(1.0, left[index] * scale))
        value_r = max(-1.0, min(1.0, right[index] * scale))
        squares += value_l * value_l + value_r * value_r
        frames[2 * index] = int(value_l * 32767)
        frames[2 * index + 1] = int(value_r * 32767)
    rms = math.sqrt(squares / max(1, 2 * len(left)))
    rms_db = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
    write_wav(path, frames, sr=SR)
    return peak * scale, rms_db


def write_wav(path: Path, frames: array.array, *, sr: int = SR) -> None:
    """写 16bit 立体声 WAV（`frames` 是交错好的 int16 数组）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(frames.tobytes())


def make_impulse_response(
    path: Path, *, seconds: float = 1.7, sr: int = SR, seed: int = 0, pre_delay: float = 0.012
) -> Path:
    """造一条立体声混响脉冲响应（`afir` 卷积用）：指数衰减噪声 + 早期反射。"""
    rng = random.Random(seed)
    count = max(1, int(seconds * sr))
    delay = max(0, int(pre_delay * sr))
    taps = ((0.011, 0.55), (0.019, 0.42), (0.031, 0.3), (0.047, 0.22))
    left_channel = [0.0] * (count + delay)
    right_channel = [0.0] * (count + delay)
    for index in range(count):
        decay = math.exp(-6.4 * index / count)
        left = (rng.random() * 2.0 - 1.0) * decay
        right = (rng.random() * 2.0 - 1.0) * decay
        time = index / sr
        for tap_time, tap_gain in taps:
            if abs(time - tap_time) < 1.0 / sr:
                left += tap_gain
                right += tap_gain * 0.85
        left_channel[index + delay] = left
        right_channel[index + delay] = right

    # **必须自己定死峰值**：`afir` 的自动增益（irnorm/gtype）在这条链路上把湿音压掉了
    # 80dB（实测），所以混响那一层用固定增益，IR 的能量就必须是可预期的
    peak = max(
        max((abs(value) for value in left_channel), default=0.0),
        max((abs(value) for value in right_channel), default=0.0),
    )
    scale = (IR_PEAK / peak) if peak > 1e-9 else 0.0
    frames = array.array("h", bytes(4 * len(left_channel)))
    for index in range(len(left_channel)):
        value_l = max(-1.0, min(1.0, left_channel[index] * scale))
        value_r = max(-1.0, min(1.0, right_channel[index] * scale))
        frames[2 * index] = int(value_l * 32767)
        frames[2 * index + 1] = int(value_r * 32767)
    write_wav(path, frames, sr=sr)
    return path
