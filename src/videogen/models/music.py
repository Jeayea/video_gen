"""配乐谱（MusicPlan）：把「这条片子该怎么配乐」写成结构化数据。

为什么要有独立模型而不是直接渲音频：

- **可核对**：段落边界、调式、速度、每个段落用什么配器、重音落在哪一秒，
  都是能给人看、能给 LLM 改、能写进报告的字段。音频本身没法 review。
- **可复现**：`seed` + 计划 = 同一首曲子（不写计划就只能靠耳朵比对两次跑得像不像）。
- **可解释**：`notes` 是中文人话，UI 直接显示「为什么这样配」——
  用户问的就是「结合视频内容生成音乐」，那必须说得出依据。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class MusicAccent(BaseModel):
    """一次重音：通常落在镜头切换或画面动作最猛的瞬间。"""

    at_sec: float
    #: hit = 低频打击（动作爆发）/ crash = 镲（段落开始）/ stab = 短和弦（转场）/ kick
    kind: str = "hit"
    reason: str = ""


class MusicChord(BaseModel):
    """一个和弦：`root` 是相对调式主音的半音数（0 = 主和弦）。"""

    root: int = 0
    #: m = 小三和弦 / M = 大三和弦 / dim = 减三和弦
    quality: str = "m"
    name: str = ""

    def intervals(self) -> tuple[int, int, int]:
        if self.quality == "M":
            return (0, 4, 7)
        if self.quality == "dim":
            return (0, 3, 6)
        return (0, 3, 7)


class MusicSection(BaseModel):
    """一个配乐段落（边界对齐镜头切换点）。"""

    index: int = 0
    #: intro（开场）/ bed（铺陈）/ build（推进）/ climax（高潮）/ resolve（收束）
    label: str = "bed"
    start_sec: float = 0.0
    end_sec: float = 0.0
    #: 0..3，决定配器层次与力度
    energy: int = 1
    layers: list[str] = Field(default_factory=list)
    #: 本段的和弦走向（结构化：渲染要用 root/quality，UI 显示 name）
    progression: list[MusicChord] = Field(default_factory=list)
    #: 这段里有没有人说话（有就自动让路：去鼓、去旋律）
    dialogue: bool = False
    shot_ids: list[str] = Field(default_factory=list)
    #: 判断依据（人话）
    reason: str = ""

    @property
    def duration_sec(self) -> float:
        return round(max(0.0, self.end_sec - self.start_sec), 3)

    @property
    def chords(self) -> list[str]:
        """和弦走向的人话名（UI 显示用）。"""
        return [chord.name or f"{chord.root}{chord.quality}" for chord in self.progression]

    def summary(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "duration_sec": self.duration_sec,
            "energy": self.energy,
            "layers": list(self.layers),
            "chords": self.chords,
            "dialogue": self.dialogue,
            "shots": len(self.shot_ids),
            "reason": self.reason,
        }


class MusicPlan(BaseModel):
    """整条片子的配乐谱。"""

    seed: int = 0
    #: 谁写的谱：local-synth（内置作曲）/ llm（LLM 给情绪与段落建议） / cli（外部模型）
    engine: str = "local-synth"
    source: str = "heuristic"
    #: 情绪色调（tense/sad/warm/hopeful/action/mystery/playful/neutral）
    color: str = "neutral"
    color_label: str = ""
    key_name: str = ""
    bpm: int = 84
    bar_sec: float = 3.0
    duration_sec: float = 0.0
    mood_tags: list[str] = Field(default_factory=list)
    #: 全片用到的配器（并集）
    layers: list[str] = Field(default_factory=list)
    sections: list[MusicSection] = Field(default_factory=list)
    accents: list[MusicAccent] = Field(default_factory=list)
    #: 有人声的时段（作曲时会避开这些窗口写旋律/重音）
    speech_windows: list[tuple[float, float]] = Field(default_factory=list)
    #: 人话说明，给 UI 显示「为什么这样配」
    notes: list[str] = Field(default_factory=list)
    #: 生成的音频（相对项目目录的路径，渲染后填）
    audio_path: str = ""
    #: 渲染统计（峰值/时长/耗时）
    stats: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def summary(self) -> dict:
        return {
            "engine": self.engine,
            "source": self.source,
            "color": self.color,
            "color_label": self.color_label,
            "key": self.key_name,
            "bpm": self.bpm,
            "duration_sec": self.duration_sec,
            "sections": len(self.sections),
            "accents": len(self.accents),
            "layers": list(self.layers),
            "speech_windows": len(self.speech_windows),
            "audio": bool(self.audio_path),
            "stats": self.stats,
        }


class MusicRequest(BaseModel):
    """一次配乐生成/选曲的覆盖参数（全部可空 = 用项目设置与全局设置）。"""

    #: library / generate / cli / auto / none（留空 = 用设置里的 BGM_ENGINE）
    engine: str = ""
    #: 色调覆盖：tense/sad/warm/hopeful/action/mystery/playful/neutral 或中文关键词
    mood: str = ""
    bpm: int = 0
    #: 调式主音（C..B），留空按色调
    key: str = ""
    #: 用户的配乐要求（自由文本，会参与情绪判断，也会写进外部模型提示词）
    note: str = ""
    gain_db: float | None = None
    #: 人声闪避（说话时自动压低音乐）
    duck: bool | None = None
    fade_sec: float | None = None
    #: 固定随机种子（0 = 由项目 id 推）
    seed: int = 0
    #: 忽略缓存重新生成
    force: bool = False
    #: 让 LLM 给一次配乐建议（付费，约 $0.001；默认关）
    llm_brief: bool = False


class MusicBriefSection(BaseModel):
    """LLM 给的一段配乐建议。"""

    mood: str = ""
    #: 0..3（也接受 0..1，上层会换算）
    energy: float = 1.0


class MusicBrief(BaseModel):
    """LLM 的配乐设计建议（付费一步；失败就退回启发式）。"""

    color: str = ""
    bpm: int = 0
    note: str = ""
    sections: list[MusicBriefSection] = Field(default_factory=list)


__all__ = [
    "MusicAccent",
    "MusicBrief",
    "MusicBriefSection",
    "MusicChord",
    "MusicPlan",
    "MusicRequest",
    "MusicSection",
]
