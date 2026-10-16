from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from videogen.utils.region import normalize_region


class AspectRatio(str, Enum):
    VERTICAL = "9:16"
    HORIZONTAL = "16:9"
    SQUARE = "1:1"


class InputMode(str, Enum):
    THEME = "theme"
    OUTLINE = "outline"
    FULL_SCRIPT = "full_script"
    #: 小说原文：按章切分 → 逐章抽取 → 合并成圣经；章节即大纲，选章改编成场次
    NOVEL = "novel"


class ContentType(str, Enum):
    #: 短剧（默认）：角色、冲突、台词
    DRAMA = "drama"
    #: 知识讲解：旁白为主线，每句旁白挂到知识底稿里的事实上（docs/EXPLAINER_DESIGN.md）
    EXPLAINER = "explainer"


class ProjectStatus(str, Enum):
    """阶段状态机。

    文本链路（当前已实现）：
        DRAFT → SCRIPT_REVIEW → SCRIPT_CONFIRMED
              → STORYBOARD_REVIEW → STORYBOARD_CONFIRMED

    媒体链路（已实现）：
        … → STORYBOARD_CONFIRMED → VIDEO_GENERATING → VIDEO_READY
           → AUDIO_GENERATING → AUDIO_READY → COMPOSING → DONE
    """

    DRAFT = "draft"
    SCRIPT_REVIEW = "script_review"
    SCRIPT_CONFIRMED = "script_confirmed"
    STORYBOARD_REVIEW = "storyboard_review"
    STORYBOARD_CONFIRMED = "storyboard_confirmed"

    # ── 媒体链路（v0.4 起全部落地：视频/配音/合成都是后台任务）──
    VIDEO_GENERATING = "video_generating"
    VIDEO_READY = "video_ready"
    AUDIO_GENERATING = "audio_generating"
    AUDIO_READY = "audio_ready"
    COMPOSING = "composing"
    DONE = "done"
    FAILED = "failed"

    # ── 历史遗留值（旧数据里出现过，仅用于兼容读取）──
    SCRIPT_GENERATED = "script_generated"
    STORYBOARD_GENERATED = "storyboard_generated"


#: 旧状态 → 新状态。旧代码把 SCRIPT_GENERATED 当「剧本已确认」用。
LEGACY_STATUS_MAP: dict[str, str] = {
    ProjectStatus.SCRIPT_GENERATED.value: ProjectStatus.SCRIPT_CONFIRMED.value,
    ProjectStatus.STORYBOARD_GENERATED.value: ProjectStatus.STORYBOARD_CONFIRMED.value,
}

#: 旧 VIDEO_READY 在 Phase 2 落地前等价于「分镜已确认」，仅在磁盘上确实没有视频产物时迁移
LEGACY_VIDEO_READY = ProjectStatus.VIDEO_READY.value
LEGACY_VIDEO_READY_TARGET = ProjectStatus.STORYBOARD_CONFIRMED.value


class Project(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    title: str = "未命名项目"
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: ProjectStatus = ProjectStatus.DRAFT
    aspect_ratio: AspectRatio = AspectRatio.VERTICAL
    target_duration_sec: int = 45
    input_mode: InputMode = InputMode.THEME
    input_text: str = ""
    #: 内容类型（建项目时定，之后不改）
    content_type: ContentType = ContentType.DRAMA
    #: 科普：目标观众，进提示词（「初中生」「没有物理基础的成年人」）
    audience: str = ""
    #: 科普：参考资料原文（可空）。有资料时事实只能从资料里取
    source_material: str = ""
    #: 科普：成片加「AI 生成」标识（片头显式 + 文件元数据隐式）。短剧不读
    ai_label: bool = True
    #: 科普：画面来源。`still` = 每句一张 AI 示意图 + 推拉（新建科普项目的默认值）；
    #: `video` = 视频模型。空 = 按 `video`（老项目行为不变）。短剧不读
    visual_mode: str = ""
    #: 科普：语气（serious 严谨 / light 轻松 / meme 玩梗；空 = 生成知识底稿时按题目推荐）。
    #: 进写稿、画面分镜、审稿和念法的提示词。短剧不读
    tone: str = ""
    #: 科普：插图画风（`explainer_service.ILLUSTRATION_STYLES` 的 key，或自填的英文画风；
    #: 空 = 生成知识底稿时按题目推荐，推荐也没有时用 EXPLAINER_ILLUSTRATION_STYLE）。短剧不读
    illustration_style: str = ""
    #: 该项目的 LLM 模型（空 = 用 .env 里的默认）。多模型切换用。
    llm_model: str = ""
    #: **整片的国家/地区限定**（cn/jp/kr/west/india/mideast/africa/latam/sea/other；
    #: 空 = 按角色名字自动推断）。
    #: 生成圣经时会写进提示词（所有角色用该国名字 + 该国长相），并写进圣经的
    #: `cast_region` 作为下游（定妆照/视频）的**权威依据** —— 比按名字猜可靠得多。
    cast_region: str = ""
    #: 生成圣经时的**额外要求**（人物设定 / 世界观 / 年代 / 语言 / 禁忌…），原文进提示词。
    #: 例：「女主要 30 岁左右，全员日本人，不要玄幻，台词口语化」
    bible_notes: str = ""
    #: 配乐引擎（library/generate/cli/auto/none；空 = 用 .env 的 BGM_ENGINE）
    music_engine: str = ""
    #: 情绪色调覆盖（tense/sad/warm/hopeful/action/mystery/playful/neutral 或中文关键词）
    music_mood: str = ""
    #: 用户的配乐要求（自由文本）。例：「压抑一点，结尾要有希望，别太吵」
    music_note: str = ""
    #: 音乐音量（dB，留空 = 用全局 BGM_GAIN_DB）
    music_gain_db: float | None = None
    #: 人声闪避（说话时压低音乐；留空 = 用全局 BGM_DUCK）
    music_duck: bool | None = None
    #: 烧字幕样式（soft/box/doc/classic；空 = 用 .env 的 COMPOSE_SUBTITLE_STYLE）
    subtitle_style: str = ""
    #: 人声来源（native = 视频原声 / tts = TTS 配音 + 相关后期；空 = 用 .env 的 AUDIO_DUB_MODE）
    dub_mode: str = ""
    #: 成片语言（zh = 普通话 / en = 英语；空 = 用 .env 的 PROJECT_LANGUAGE）：剧本、台词、旁白、
    #: 视频原声与配音音色都按它走
    language: str = ""

    @field_validator("cast_region", mode="before")
    @classmethod
    def _coerce_cast_region(cls, value: object) -> str:
        # "中国" / "Japanese" / "KR" 都归一成 cn/jp/kr；不认识就留空（= 自动）
        return normalize_region(value)

    @property
    def output_resolution(self) -> tuple[int, int]:
        return {
            AspectRatio.VERTICAL: (1080, 1920),
            AspectRatio.HORIZONTAL: (1920, 1080),
            AspectRatio.SQUARE: (1080, 1080),
        }[self.aspect_ratio]
