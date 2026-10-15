from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.models.script import MIN_SCENE_DURATION_SEC
from videogen.utils.coerce import StrList, coerce_seconds, ensure_optional_str

DEFAULT_SHOT_DURATION_SEC = 4


class DurationTier(BaseModel):
    """视频模型的计费档位（分镜阶段用它决定「一场分几镜」，避免买 5 秒只剪 3 秒）。

    - `min_sec`：模型最短计费时长（H3 = 5 秒）。镜头短于它就等于白送钱
    - `max_sec`：模型最长时长（H3 = 15 秒，0 = 不限制）
    - `target_sec`：单个镜头的目标时长（0 = 自动取 min×1.6）
    - `source`：档位来源（provider = 问了模型能力表；config = 配置兜底）
    """

    min_sec: float = 0.0
    max_sec: float = 0.0
    target_sec: float = 0.0
    source: str = ""

    @property
    def active(self) -> bool:
        """只有「最短时长 > 1 秒」才值得改变分镜策略（否则本来就不浪费）。"""
        return self.min_sec > 1.0


class VoiceDirection(BaseModel):
    """一条配音文案的念法建议（LLM 按画面情绪给，留空 = 用全局设置）。"""

    rate: str = ""
    volume: str = ""
    pitch: str = ""
    #: 自然语言念法（云端 TTS 用）：例如「压抑的哭腔，语速慢，句尾发颤」。
    #: Edge 不认这种描述，只有云端 provider 会用它
    instruction: str = ""
    note: str = ""


class ShotDraft(BaseModel):
    """LLM 返回的镜头草稿（id/scene_id/shot_no 由系统分配，不信任模型输出）。"""

    shot_no: int | None = None
    duration_sec: int = DEFAULT_SHOT_DURATION_SEC
    camera: str = ""
    visual_description: str = ""
    character_in_frame: StrList = Field(default_factory=list)
    mood: str = ""
    video_prompt: str = ""
    #: 这一镜发生在哪个场景（故事圣经 settings 的 id）——背景一致性与丰富度的锚点
    location: str = ""
    #: 画面里出现的关键道具（故事圣经 props 的 id）
    props_in_frame: StrList = Field(default_factory=list)
    narration: str | None = None
    dialogue_text: str | None = None
    dialogue_speaker: str | None = None
    #: 画外音：说话人不在画面里（门外 / 电话里 / 隔墙喊话）。这时不把他补进画面、
    #: 视频 prompt 不要求画面里有人对口型、合成时也不做口型
    voice_offscreen: bool = False

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, value: object) -> int:
        return max(1, coerce_seconds(value, DEFAULT_SHOT_DURATION_SEC))

    @field_validator("narration", "dialogue_text", "dialogue_speaker", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> str | None:
        return ensure_optional_str(value)

    @field_validator("voice_offscreen", mode="before")
    @classmethod
    def _coerce_offscreen(cls, value: object) -> bool:
        # 模型会写 null / "true" / "是"
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "是"}
        return bool(value)


class Shot(BaseModel):
    id: str
    scene_id: str
    shot_no: int
    duration_sec: int = DEFAULT_SHOT_DURATION_SEC
    camera: str = ""  # e.g. "中景 · 固定"
    visual_description: str = ""
    character_in_frame: StrList = Field(default_factory=list)
    mood: str = ""
    video_prompt: str = ""
    #: 这一镜发生在哪个场景（故事圣经 settings 的 id），用于注入背景锚点
    location: str = ""
    #: 画面里出现的关键道具（故事圣经 props 的 id）：有参考图时一起送进视频模型
    props_in_frame: StrList = Field(default_factory=list)
    #: 段落分镜：所属段落 id（同一段的镜头一次生成、段内硬切）。空 = 单镜生成
    segment_id: str = ""
    narration: str | None = None
    dialogue_text: str | None = None
    dialogue_speaker: str | None = None
    #: 画外音：说话人不在画面里（门外 / 电话里 / 隔墙喊话）。这时不把他补进画面、
    #: 视频 prompt 不要求画面里有人对口型、合成时也不做口型
    voice_offscreen: bool = False
    #: 配音文案来源标记："llm:narration" / "llm:polish" / "llm:full" = 这条是
    #: LLM 看着画面写的；空 = 剧本/人工原文。同一模式重跑会跳过，不重复花额度。
    voice_script_source: str = ""
    #: 被 LLM 改写前的原文（便于人工核对 / 回滚）
    voice_script_original: str = ""
    #: LLM 给的念法建议（rate / volume / pitch），合成配音时优先于全局设置
    voice_direction: VoiceDirection | None = None

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, value: object) -> int:
        return max(1, coerce_seconds(value, DEFAULT_SHOT_DURATION_SEC))

    @field_validator("narration", "dialogue_text", "dialogue_speaker", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> str | None:
        return ensure_optional_str(value)


class Storyboard(BaseModel):
    shots: list[Shot] = Field(default_factory=list)
    duration_normalized: bool = False
    #: 是否已按配音时长对齐过（对齐只会拉长镜头，不会裁掉配音）
    audio_aligned: bool = False
    #: 生成时用的视频计费档位（没有真实 provider 时为空 = 不受档位约束）
    duration_tier: DurationTier | None = None
    #: 档位相关的告警（例如「有对白的镜头无法再合并，镜头数多于计划」）
    tier_warnings: list[str] = Field(default_factory=list)

    @property
    def total_duration_sec(self) -> int:
        return sum(s.duration_sec for s in self.shots)

    def shots_for_scene(self, scene_id: str) -> list[Shot]:
        return [s for s in self.shots if s.scene_id == scene_id]

    def scene_ids(self) -> set[str]:
        return {s.scene_id for s in self.shots}

    def segment_ids(self) -> list[str]:
        """按出现顺序去重的段落 id（没编段的镜头不算）。"""
        seen: list[str] = []
        for shot in self.shots:
            if shot.segment_id and shot.segment_id not in seen:
                seen.append(shot.segment_id)
        return seen


__all__ = [
    "DEFAULT_SHOT_DURATION_SEC",
    "MIN_SCENE_DURATION_SEC",
    "DurationTier",
    "Shot",
    "ShotDraft",
    "Storyboard",
    "VoiceDirection",
]
