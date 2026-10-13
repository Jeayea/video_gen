from __future__ import annotations

import re

from videogen.utils import language

__all__ = ["clean_spoken_text", "count_speech_chars", "estimate_speech_sec"]

#: 括号里的舞台提示 / 音效提示，配音时不能念出来
_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]{0,40}[）)】\]]")
_WS_RE = re.compile(r"\s+")

#: 中文配音语速经验值（字/秒），用于估算时长与成本
DEFAULT_CHARS_PER_SEC = 4.5
MIN_SPEECH_SEC = 0.6


def clean_spoken_text(text: str | None) -> str:
    """清洗要念出来的文本：去掉舞台提示、多余空白。"""
    if not text:
        return ""
    cleaned = _BRACKET_RE.sub("", str(text))
    return _WS_RE.sub(" ", cleaned).strip()


def count_speech_chars(text: str | None) -> int:
    """计入配音成本的字符数（去掉空白后的长度）。"""
    cleaned = clean_spoken_text(text)
    return len(_WS_RE.sub("", cleaned))


def estimate_speech_sec(text: str | None, *, chars_per_sec: float = DEFAULT_CHARS_PER_SEC) -> float:
    """按语速估算语音时长（秒）—— 拿不到真实音频时的兜底。"""
    cleaned = clean_spoken_text(text)
    cjk, words = language.speech_units(cleaned)
    if cjk + words <= 0:
        return 0.0
    # 英文单词按 2.5 词/秒（与中文语速同比缩放，调用方改 chars_per_sec 时一起变）
    words_per_sec = language.EN_WORDS_PER_SEC * chars_per_sec / DEFAULT_CHARS_PER_SEC
    seconds = cjk / max(0.5, chars_per_sec) + words / max(0.3, words_per_sec)
    return max(MIN_SPEECH_SEC, round(seconds, 3))
