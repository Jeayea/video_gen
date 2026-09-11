from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    llm_provider: str = "openrouter"
    openrouter_api_key: str = Field(default="", repr=False)
    openrouter_model: str = "google/gemini-2.5-flash"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_sec: float = 120.0
    llm_max_retries: int = 2
    llm_json_retries: int = 2

    # Video
    #: mock = 免费占位片段；openrouter = 真实视频模型（按秒计费）
    video_provider: str = "mock"
    #: OpenRouter 的视频模型（minimax/hailuo-3 = MiniMax H3；也支持 hailuo-3-max 等）
    openrouter_video_model: str = "minimax/hailuo-3"
    #: 分辨率交给模型能力校验，不支持会自动换成第一个支持的（H3 只支持 2K）
    video_resolution: str = ""
    #: 是否让视频模型自带音轨（我们用 TTS 配音，默认关）
    video_generate_audio: bool = False
    #: 角色一致性：把角色参考图传给视频模型
    video_use_reference_images: bool = True
    video_poll_interval_sec: float = 10.0
    #: 单个片段最多等多久（真实模型通常 1-5 分钟）
    video_timeout_sec: float = 900.0
    #: 合并短镜头时的目标最短时长（秒）。0 = 自动用模型最短计费档位（H3 是 5 秒）
    video_min_shot_sec: float = 0.0
    #: 分镜阶段单个镜头的目标时长（秒）。0 = 自动取最短档位 × 1.6（H3 → 约 8 秒）
    video_shot_target_sec: float = 0.0
    #: 单个镜头的时长上限（秒）。0 = 用模型最长档位（H3 = 15 秒）
    video_max_shot_sec: float = 0.0
    video_concurrency: int = 3
    #: 追加到每个视频 prompt 的「表演与连贯性」规范。真实样本里模型会把哭戏演成舞台剧
    #: （眉毛挤成一团、身体僵硬），也会在长 prompt 下变形；这段是统一的最低要求
    video_performance_directive: str = (
        "natural subtle expressions and micro-expressions, relaxed body language, "
        "natural blinking and breathing, smooth continuous motion, stable framing, "
        "single continuous take with no cuts, no morphing or warping, "
        "avoid exaggerated theatrical acting"
    )
    #: 追加到每个视频 prompt 的风格约束（英文；留空 = 不追加）
    video_style_prompt: str = ""
    video_max_retries: int = 2
    video_retry_backoff_sec: float = 1.5
    video_fps: int = 24
    video_crf: int = 30

    # TTS（配音）
    tts_provider: str = "edge"  # edge | mock
    #: 默认音色；角色音色由 voice_map 决定，这里是兜底
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_voice_female: str = "zh-CN-XiaoxiaoNeural"
    tts_voice_male: str = "zh-CN-YunxiNeural"
    tts_voice_narrator: str = "zh-CN-YunjianNeural"
    tts_rate: str = "+0%"
    tts_volume: str = "+0%"
    tts_pitch: str = "+0Hz"
    tts_concurrency: int = 3
    tts_max_retries: int = 2
    tts_retry_backoff_sec: float = 1.5
    #: 允许给角色选方言音色（例如东北话/陕西话）。默认不允许：同一部戏里混口音很出戏，
    #: 宁可「复用通用音色 + 音高区分」也不给主角上方言
    tts_allow_accent_voices: bool = False
    # 剧本严格模式：结构硬伤（空场/重复场/结尾悬着/缺冲突/LLM 评审报 error）会阻止「确认剧本」，
    # 需要 force=true 才能强制确认
    script_strict: bool = True
    #: 生成剧本后让 LLM 当一次剧本医生（结构/因果/动机），多花一次 LLM 调用（≈$0.001）
    script_llm_review: bool = True
    tts_timeout_sec: float = 60.0
    #: 每句对白后面留的呼吸时间（秒），用于音画对齐
    audio_gap_sec: float = 0.25
    #: 配音比镜头长时的最大容忍倍率，超过只告警不裁音频
    audio_align_max_ratio: float = 2.0

    # 配音文案（可选：让 LLM 看着真实画面写/改台词，会额外花 LLM 额度）
    #: off = 只用分镜里的原文案；narration = 只给没台词的镜头补旁白；
    #: polish = 只把已有文案改得更口语/更贴画面；full = 补旁白 + 改写 + 念法建议
    audio_llm_script: str = "off"
    #: 是否把视频抽帧发给模型（关掉 = 纯文本，便宜但对不上画面）
    audio_llm_vision: bool = True
    #: 每个镜头抽几帧（1-3）；帧越多越准也越贵
    audio_llm_frames_per_shot: int = 1
    #: 抽帧缩放后的宽度（像素），越小越便宜
    audio_llm_frame_width: int = 640
    #: 配音文案单独指定模型（留空 = 用当前 LLM 模型）
    audio_llm_model: str = ""
    #: 单句文案的硬上限字符数，超了截断（防止模型写论文）
    audio_llm_max_chars: int = 60
    #: 对已经生成过文案的镜头再跑一次（默认跳过，避免重复花钱）
    audio_llm_force: bool = False

    # BGM
    bgm_enabled: bool = True
    bgm_dir: Path = Path("./assets/bgm")
    bgm_gain_db: float = -20.0
    bgm_fade_sec: float = 1.5
    #: 手动指定曲目 id（留空 = 按镜头情绪自动匹配）
    bgm_id: str = ""

    # 合成
    #: soft=mp4 内嵌 mov_text 字幕轨（不需要 libass）| burn=烧进画面 | none=只导出 srt
    compose_subtitle_mode: str = "soft"
    compose_crf: int = 23
    compose_preset: str = "veryfast"
    compose_concurrency: int = 2
    compose_subtitle_font: str = ""
    #: 字幕字号 = min(宽, 高) * 该比例（0.05 ≈ 1080 宽的竖屏 54px）
    compose_subtitle_font_scale: float = 0.05
    #: 字幕距底部 = min(宽, 高) * 该比例
    compose_subtitle_margin_scale: float = 0.04
    #: 视频模型自带音轨怎么处理：off = 丢弃（人声最干净）；under = 压低垫在人声下面
    compose_ambient_mode: str = "off"
    #: 环境音相对人声的音量（dB）
    compose_ambient_gain_db: float = -20.0
    #: 保留中间分段文件（排查用）
    compose_keep_segments: bool = False

    # 成本与预算（单价都可配，第三方价格会变）
    #: 0 = 不限制；单位 USD
    budget_usd: float = 0.0
    budget_warn_ratio: float = 0.8
    llm_input_price_per_mtok: float = 0.30
    llm_output_price_per_mtok: float = 2.50
    #: 真实视频 API 按秒计费（mock = 0），例如可灵约 0.05/秒
    video_price_per_second: float = 0.0
    #: 真实 TTS 按字符计费（edge-tts 免费 = 0）
    tts_price_per_mchar: float = 0.0
    #: 可切换的模型候选（逗号分隔），用于前端下拉
    llm_model_candidates: str = (
        "google/gemini-2.5-flash,google/gemini-2.5-pro,anthropic/claude-3.5-haiku"
    )

    # 角色一致性
    #: 固定 seed（0 = 不指定），真实 provider 用来稳定人物外观
    video_seed: int = 0
    reference_image_max_mb: float = 8.0

    # 批量生产
    batch_max_items: int = 20

    # Paths
    projects_dir: Path = Path("./projects")
    static_dir: Path | None = None
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = ""
    #: 主 ffmpeg 缺能力（例如没有 libass 烧不了字幕）时，是否自动换用候选里
    #: 能满足要求的那个（imageio-ffmpeg 便携版通常带 libass）
    ffmpeg_auto_upgrade: bool = True
    # 角色图（AI 生成参考图）
    image_provider: str = "openrouter"
    openrouter_image_model: str = "bytedance-seed/seedream-4.5"
    #: 角色图宽高比 / 分辨率档位（留空 = 交给模型默认）
    image_aspect_ratio: str = "1:1"
    image_resolution: str = ""
    image_timeout_sec: float = 180.0
    #: 图片接口失败重试的退避基数（秒）；测试里置 0
    image_retry_backoff_sec: float = 2.0
    #: 模型没报 usage.cost 时用于记账的单价（USD / 张）
    image_price_usd: float = 0.04
    #: 生成角色图时是否带上已有参考图（做「同一角色的另一角度」）
    image_use_existing_reference: bool = False
    #: 追加到定妆照 prompt 末尾的额外约束（**必须英文**，中文会被模型画进画面）。
    #: 角色设定里没写人种时，用 'East Asian cast, Chinese television drama'
    #: 之类统一整组角色的面貌基调
    image_prompt_suffix: str = ""

    # 安全：留空表示不校验（仅建议在本机 127.0.0.1 上跑）；设置后所有 /api 请求需带
    # X-API-Token 头或 ?token= 查询参数
    api_token: str = Field(default="", repr=False)

    @property
    def projects_path(self) -> Path:
        return self.projects_dir.resolve()

    @property
    def static_path(self) -> Path:
        if self.static_dir:
            return self.static_dir.resolve()
        return _REPO_ROOT / "static"

    @property
    def has_llm_key(self) -> bool:
        """只暴露「有没有配」，绝不返回密钥本身。"""
        return bool(self.openrouter_api_key.strip())

    @property
    def bgm_path(self) -> Path:
        return self.bgm_dir.expanduser().resolve()

    @property
    def candidate_models(self) -> list[str]:
        models = [m.strip() for m in (self.llm_model_candidates or "").split(",")]
        seen: list[str] = []
        for model in models:
            if model and model not in seen:
                seen.append(model)
        if self.openrouter_model and self.openrouter_model not in seen:
            seen.insert(0, self.openrouter_model)
        return seen

    @property
    def ambient_mode(self) -> str:
        mode = (self.compose_ambient_mode or "off").strip().lower()
        return mode if mode in {"off", "under"} else "off"

    @property
    def subtitle_mode(self) -> str:
        mode = (self.compose_subtitle_mode or "soft").strip().lower()
        return mode if mode in {"soft", "burn", "none"} else "soft"

    @property
    def audio_script_mode(self) -> str:
        """配音文案模式，非法值一律当成 off（绝不让配置笔误偷偷花钱）。"""
        mode = (self.audio_llm_script or "off").strip().lower()
        return mode if mode in {"off", "narration", "polish", "full"} else "off"

    @property
    def audio_frame_count(self) -> int:
        return min(3, max(1, int(self.audio_llm_frames_per_shot or 1)))

    @property
    def audio_frame_width(self) -> int:
        return min(1600, max(160, int(self.audio_llm_frame_width or 640)))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """测试用：清掉单例，让环境变量重新生效。"""
    global _settings
    _settings = None
