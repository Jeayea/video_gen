"""成片语言：台词 / 旁白 / 剧本用什么语言写、视频模型念什么语言、语速怎么估。

目前支持 `zh`（普通话，默认）与 `en`（英语）。设计上尽量不往下游传参数：

- **视频 prompt 按台词原文判断语言**（有汉字 → Mandarin，否则 English），所以分镜改成英文后
  视频阶段不需要知道项目语言
- **语速按文字本身估**：汉字按 4.5 字/秒、拉丁单词按 2.5 词/秒，中英混排各算各的
- **LLM 提示词**是中文写的，示例里也写着「中文描述」；英文项目在 system prompt 末尾追加一段
  「所有给人看的内容一律用英文写」的覆盖指令（`LanguageLLM`），提示词本身不用改两份
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from videogen.providers.llm.base import LLMMessage, LLMProvider

__all__ = [
    "DEFAULT_LANGUAGE",
    "EN_WORDS_PER_SEC",
    "LANGUAGES",
    "ZH_CHARS_PER_SEC",
    "LanguageLLM",
    "language_directive",
    "normalize_language",
    "speech_seconds",
    "speech_units",
    "spoken_language_name",
    "with_language",
]

DEFAULT_LANGUAGE = "zh"
#: 语言代码 → 给人看的名字
LANGUAGES = {"zh": "中文（普通话）", "en": "English"}

#: 中文念白语速（字/秒），与分镜提示词里的「台词字数 ÷ 4.5」同一个口径
ZH_CHARS_PER_SEC = 4.5
#: 英文念白语速（词/秒）：影视对白常见 140-160 词/分钟
EN_WORDS_PER_SEC = 2.5

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA_RE = re.compile(r"[぀-ヿ]")
_HANGUL_RE = re.compile(r"[가-힯ᄀ-ᇿ]")
_WORD_RE = re.compile(r"[A-Za-z0-9À-ɏ]+(?:['’][A-Za-z]+)?")

_ALIASES = {
    "zh": "zh", "zh-cn": "zh", "cn": "zh", "chinese": "zh", "mandarin": "zh", "中文": "zh",
    "en": "en", "en-us": "en", "en-gb": "en", "english": "en", "英文": "en", "英语": "en",
}


def normalize_language(value: Any) -> str:
    """任意写法 → `zh` / `en`；认不出来的回到默认（中文）。"""
    key = str(value or "").strip().lower()
    return _ALIASES.get(key, DEFAULT_LANGUAGE)


def spoken_language_name(text: str) -> str:
    """按台词原文判断视频模型该念什么语言（写进英文 prompt 的那个词）。"""
    if _KANA_RE.search(text or ""):
        return "Japanese"
    if _HANGUL_RE.search(text or ""):
        return "Korean"
    if _CJK_RE.search(text or ""):
        return "Mandarin"
    return "English"


def speech_units(text: str) -> tuple[int, int]:
    """(汉字数, 拉丁单词数)：念白时长按这两个量分别估。"""
    text = text or ""
    cjk = len(_CJK_RE.findall(text)) + len(_KANA_RE.findall(text)) + len(_HANGUL_RE.findall(text))
    latin = _CJK_RE.sub(" ", _KANA_RE.sub(" ", _HANGUL_RE.sub(" ", text)))
    return cjk, len(_WORD_RE.findall(latin))


def speech_seconds(text: str) -> float:
    """念完这段文字大约要几秒：汉字 4.5 字/秒 + 英文 2.5 词/秒。"""
    cjk, words = speech_units(text)
    return cjk / ZH_CHARS_PER_SEC + words / EN_WORDS_PER_SEC


_EN_DIRECTIVE = """

## LANGUAGE OVERRIDE — this project is an ENGLISH-language film
This overrides every instruction above that says to write in Chinese (中文).
- Write ALL human-readable content in natural, idiomatic English: titles, loglines, synopses,
  character names (use English/Western names unless the story is set elsewhere), descriptions,
  personalities, scene briefs, visual descriptions, dialogue lines, narration, voice directions,
  summaries, review messages and suggestions.
- Dialogue and narration will be SPOKEN in English by the video model / TTS: write how people
  actually talk, short sentences, no Chinese characters anywhere.
- Keep JSON keys, ids (scene_1, char_1, ch_1 …) and enumerated values (e.g. region codes, role
  categories, gender) exactly as specified above — only free text switches to English.
- Speech-rate rule: wherever the instructions count dialogue in Chinese characters (字) at
  4.5 字/秒, count English words at 2.5 words/sec instead (a shot of N seconds fits about
  2.5 × (N − 1) words)."""


def language_directive(language: str) -> str:
    """追加到 system prompt 末尾的语言覆盖指令（中文项目为空）。"""
    return _EN_DIRECTIVE if normalize_language(language) == "en" else ""


def _with_directive(messages: list[LLMMessage], directive: str) -> list[LLMMessage]:
    if not directive:
        return messages
    out = list(messages)
    for idx, message in enumerate(out):
        if message.role == "system" and isinstance(message.content, str):
            out[idx] = message.model_copy(update={"content": message.content + directive})
            return out
    return [LLMMessage(role="system", content=directive.strip()), *out]


class LanguageLLM(LLMProvider):
    """包一层 LLM：每次请求都在 system prompt 末尾追加语言覆盖指令，用量记在原 provider 上。"""

    def __init__(self, inner: LLMProvider, language: str):
        self.inner = inner
        self.language = normalize_language(language)
        self._directive = language_directive(self.language)

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def usage(self):  # type: ignore[override]
        return self.inner._usage_ref()

    @usage.setter
    def usage(self, value) -> None:
        self.inner.usage = value

    def record_usage(self, **kwargs: Any) -> None:
        self.inner.record_usage(**kwargs)

    def take_usage(self):
        return self.inner.take_usage()

    def with_model(self, model: str) -> LLMProvider:
        return LanguageLLM(self.inner.with_model(model), self.language)

    def __getattr__(self, item: str) -> Any:
        # 测试替身（FakeLLM）的计数器等属性照常可读
        return getattr(self.inner, item)

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def chat(self, messages: list[LLMMessage], **kwargs: Any) -> str:
        return await self.inner.chat(_with_directive(messages, self._directive), **kwargs)

    async def chat_json(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self.inner.chat_json(
            _with_directive(messages, self._directive), schema=schema, **kwargs
        )


def with_language(llm: LLMProvider, language: str) -> LLMProvider:
    """中文项目原样返回；其他语言包一层 `LanguageLLM`。"""
    if normalize_language(language) == DEFAULT_LANGUAGE or isinstance(llm, LanguageLLM):
        return llm
    return LanguageLLM(llm, language)
