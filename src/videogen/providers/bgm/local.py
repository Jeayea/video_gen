from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from videogen.config import get_settings
from videogen.models.audio import BgmTrack
from videogen.providers.bgm.base import BgmLibrary

logger = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".opus"}
#: 自动发现的文件名里，用 _ - 空格 分隔情绪标签，纯数字段落当序号忽略
_TOKEN_SPLIT_RE = re.compile(r"[_\-\s]+")
_METADATA_NAME = "bgm.json"


class LocalBgmLibrary(BgmLibrary):
    """本地曲库：扫 `BGM_DIR` 下的音频文件 + 可选的 `bgm.json` 元数据。

    元数据格式：

    ```json
    {
      "tracks": [
        {"id": "tense_01", "title": "暗涌", "file": "tense_01.mp3",
         "moods": ["紧张", "压抑"], "gain_db": -22, "license": "CC0"}
      ]
    }
    ```

    没写进元数据的音频文件也会被自动收录：文件名按 `_ - 空格` 切分出情绪标签
    （`tense_01.mp3` → id=`tense_01`、moods=[`tense`]）。
    """

    name = "local"

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else get_settings().bgm_path
        self._cache: list[BgmTrack] | None = None

    # ── 接口 ──────────────────────────────────────────

    def tracks(self) -> list[BgmTrack]:
        if self._cache is None:
            self._cache = self._scan()
        return list(self._cache)

    def refresh(self) -> None:
        self._cache = None

    # ── 扫描 ──────────────────────────────────────────

    def _scan(self) -> list[BgmTrack]:
        if not self.directory.is_dir():
            logger.info("BGM 曲库目录不存在，本次不配乐：%s", self.directory)
            return []

        declared = self._load_metadata()
        found: dict[str, BgmTrack] = {}
        for track in declared:
            if track.path and Path(track.path).is_file():
                found[track.id] = track
            else:
                logger.warning("BGM 元数据指向的文件不存在，已跳过：%s", track.path or track.id)

        for path in sorted(self.directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            track_id = path.stem
            if any(t.path == str(path) for t in found.values()):
                continue
            found.setdefault(track_id, self._auto_track(path))

        return sorted(found.values(), key=lambda t: t.id)

    def _load_metadata(self) -> list[BgmTrack]:
        meta_path = self.directory / _METADATA_NAME
        if not meta_path.is_file():
            return []
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("BGM 元数据解析失败（%s）：%s", meta_path, exc)
            return []

        entries = payload.get("tracks", []) if isinstance(payload, dict) else payload
        tracks: list[BgmTrack] = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            file_name = str(entry.get("file") or "").strip()
            track_id = str(entry.get("id") or Path(file_name).stem).strip()
            if not track_id or not file_name:
                continue
            tracks.append(
                BgmTrack(
                    id=track_id,
                    title=str(entry.get("title") or track_id),
                    path=str(self.directory / file_name),
                    moods=[str(m) for m in entry.get("moods") or []],
                    duration_sec=float(entry.get("duration_sec") or 0.0),
                    gain_db=float(entry["gain_db"]) if entry.get("gain_db") is not None else None,
                    license=str(entry.get("license") or ""),
                )
            )
        return tracks

    @staticmethod
    def _auto_track(path: Path) -> BgmTrack:
        moods = [
            token.lower()
            for token in _TOKEN_SPLIT_RE.split(path.stem)
            if token and not token.isdigit()
        ]
        return BgmTrack(id=path.stem, title=path.stem, path=str(path), moods=moods)
