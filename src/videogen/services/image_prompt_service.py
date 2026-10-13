"""用户自己写的出图 prompt：任何语言都行，发给图片模型前统一翻成英文。

为什么必须是英文：图片模型会把 prompt 里的中文当成要画的字（真实事故：角色名和风格名被印成
海报标题，见 character_image_service）。系统自动拼的 prompt 本来就是英文；用户手写的 prompt
以前要求用户自己写英文，现在由这里翻译，结果原样返回给前端核对。
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from videogen.providers.llm.base import LLMMessage, LLMProvider

__all__ = ["TRANSLATE_SYSTEM", "needs_translation", "one_line", "strip_cjk", "translate"]

#: 中日韩字符 + 全角标点：翻译之后还剩的一律剔掉，绝不让它们进图片 prompt
_CJK_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]+")

TRANSLATE_SYSTEM = """你是图片 prompt 翻译：把用户写的出图描述翻成给图片生成模型用的英文 prompt。

规则：
- 忠实翻译画面内容，不增不减：不要自己加风格词、镜头词、画质词
- 人名、地名这类专有名词**不要写出来**（图片模型会把它们画成文字），改写成外貌 / 场景本身的描述
- 只输出英文，一个中文字符都不能有
- 输出 JSON：{"prompt": "..."}"""


class _Translated(BaseModel):
    prompt: str = ""


def one_line(text: str | None) -> str:
    return " ".join(str(text or "").split())


def strip_cjk(text: str | None) -> str:
    return " ".join(_CJK_RE.sub(" ", str(text or "")).split()).strip(" ,.;")


def needs_translation(text: str | None) -> bool:
    """只要有非 ASCII 的文字就翻（中文、日文、带重音的法语……）；纯英文原样用、不花钱。"""
    return any(not ch.isascii() and ch.isalpha() for ch in str(text or ""))


async def translate(llm: LLMProvider, text: str) -> str:
    """翻成英文；翻完是空的（模型没听话）就抛错，不悄悄退回自动 prompt。"""
    messages = [
        LLMMessage(role="system", content=TRANSLATE_SYSTEM),
        LLMMessage(role="user", content=one_line(text)),
    ]
    result = await llm.chat_json(messages, schema=_Translated, temperature=0.2, max_tokens=1024)
    english = strip_cjk(result.prompt)
    if not english:
        raise ValueError("翻译结果为空")
    return english
