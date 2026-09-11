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

    # TTS
    tts_provider: str = "edge"

    # Paths
    projects_dir: Path = Path("./projects")
    static_dir: Path | None = None
    ffmpeg_path: str = "ffmpeg"

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
