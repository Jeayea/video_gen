from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from videogen.models.audio import BgmTrack

__all__ = ["BgmLibrary", "BgmLibraryError", "BgmTrack", "EmptyBgmLibrary", "match_score"]


class BgmLibraryError(RuntimeError):
    """曲库不可用（目录缺失、元数据损坏等）。"""


def match_score(track_moods: Sequence[str], wanted: Sequence[str]) -> float:
    """情绪标签匹配打分（中文标签用互相包含判断，避免「紧张」vs「紧张的」漏配）。"""

    def normalized(values: Sequence[str]) -> list[str]:
        return [str(v).strip().lower() for v in values if str(v).strip()]

    haystack = normalized(track_moods)
    score = 0.0
    for tag in normalized(wanted):
        for have in haystack:
            if tag == have:
                score += 2.0
                break
            if tag in have or have in tag:
                score += 1.0
                break
    return score


class BgmLibrary(ABC):
    """背景音乐曲库。

    约定：曲库为空不是错误 —— 返回 `None` 表示「这次没有 BGM」，合成链路照常走。
    """

    name: str = "base"

    @abstractmethod
    def tracks(self) -> list[BgmTrack]:
        """列出曲库里的曲目（按 id 稳定排序）。"""

    def pick(
        self,
        moods: Sequence[str] = (),
        *,
        preferred_id: str = "",
        fallback: bool = True,
    ) -> BgmTrack | None:
        """挑一首：手动指定 > 情绪匹配 > 兜底第一首 > None。"""
        items = self.tracks()
        if not items:
            return None

        wanted_id = (preferred_id or "").strip()
        if wanted_id:
            for track in items:
                if track.id == wanted_id:
                    return track.model_copy(update={"score": 99.0})

        best: BgmTrack | None = None
        for track in items:
            score = match_score(track.moods, moods)
            if score > 0 and (best is None or score > best.score):
                best = track.model_copy(update={"score": score})
        if best is not None:
            return best
        return items[0] if fallback else None

    def track_by_id(self, track_id: str) -> BgmTrack | None:
        for track in self.tracks():
            if track.id == track_id:
                return track
        return None


class EmptyBgmLibrary(BgmLibrary):
    """显式关闭配乐时用的空曲库。"""

    name = "none"

    def tracks(self) -> list[BgmTrack]:
        return []
