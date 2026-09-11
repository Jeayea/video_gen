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
    video_provider: str = "mock"
    video_concurrency: int = 3
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
    tts_timeout_sec: float = 60.0
    #: 每句对白后面留的呼吸时间（秒），用于音画对齐
    audio_gap_sec: float = 0.25
    #: 配音比镜头长时的最大容忍倍率，超过只告警不裁音频
    audio_align_max_ratio: float = 2.0

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
    #: 保留中间分段文件（排查用）
    compose_keep_segments: bool = False

    # Paths
    projects_dir: Path = Path("./projects")
    static_dir: Path | None = None
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = ""

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
    def subtitle_mode(self) -> str:
        mode = (self.compose_subtitle_mode or "soft").strip().lower()
        return mode if mode in {"soft", "burn", "none"} else "soft"


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
