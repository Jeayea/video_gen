from __future__ import annotations

from videogen.config import get_settings
from videogen.models.audio import BgmTrack
from videogen.providers.bgm.base import (
    BgmLibrary,
    BgmLibraryError,
    EmptyBgmLibrary,
    match_score,
)
from videogen.providers.bgm.local import LocalBgmLibrary

__all__ = [
    "BgmLibrary",
    "BgmLibraryError",
    "BgmTrack",
    "EmptyBgmLibrary",
    "LocalBgmLibrary",
    "get_bgm_library",
    "match_score",
]


def get_bgm_library() -> BgmLibrary:
    """当前只有本地曲库实现；`BGM_ENABLED=false` 时返回空曲库。"""
    settings = get_settings()
    if not settings.bgm_enabled:
        return EmptyBgmLibrary()
    return LocalBgmLibrary(directory=settings.bgm_path)
