"""文学剧本导出：把剧本（+ 分镜）排成人读的格式。纯排版，不调 LLM、$0。

为什么不让 LLM 另写一份「文学剧本」：那会多出一个说法来源，改了剧本 / 分镜它不会跟着变。
这里只从已有产物排版，所以导出的永远和片子一致：

- `mode="script"`：场次标题 + 时空 + 动作描写 + 台词（带括注）+ 转场，读戏用
- `mode="shots"`：同样的场次框架，场内按镜头展开（景别 / 画面 / 谁说了什么），通读分镜用

内部 id（char_N / loc_N / prop_N / narrator）一律换成名字；转场优先取分镜第一镜的
（片子实际怎么剪），没有分镜时取剧本的 `transition_in`。
"""

from __future__ import annotations

import re
from typing import Any

from videogen.models.bible import StoryBible
from videogen.models.script import NARRATOR, Scene, Script
from videogen.models.storyboard import Shot, Storyboard

MODES = ("script", "shots")
SFX_LABELS = {
    "phone_ring": "手机铃声",
    "phone_vibrate": "手机震动",
    "call_connect": "接通",
    "busy_tone": "忙音",
    "hang_up": "挂断",
}

#: 场末转场（写在上一场末尾）/ 场首转场（写在这一场开头）
_OUT_LABELS = {"dissolve": "叠化至：", "fade": "淡出。"}
_IN_LABELS = {"fade": "淡入："}
_ID_RE = re.compile(r"\b(?:char|loc|prop)_\d+\b")
_HEADING_PREFIX = re.compile(
    r"^\s*(INT\.?\s*/\s*EXT\.?|EXT\.?\s*/\s*INT\.?|INT\b\.?|EXT\b\.?)\s*", re.I
)
_HEADING_WORDS = {"INT": "内景", "EXT": "外景"}


def build(
    script: Script,
    bible: StoryBible | None,
    storyboard: Storyboard | None = None,
    *,
    mode: str = "script",
    title: str = "",
    chapters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """返回 `{"title", "mode", "blocks", "markdown", "stats", "warnings"}`。

    `chapters`：场次 id → 所属章的标题（长篇项目）；换章时插一行章标题。
    """
    mode = mode if mode in MODES else "script"
    names = _name_map(bible)
    shots_by_scene: dict[str, list[Shot]] = {}
    for shot in storyboard.shots if storyboard else []:
        shots_by_scene.setdefault(shot.scene_id, []).append(shot)

    blocks: list[dict[str, Any]] = []
    heading_title = (script.title or title or "").strip() or "未命名"
    blocks.append({"type": "title", "text": heading_title})
    if script.logline:
        blocks.append({"type": "logline", "text": _sub_ids(script.logline, names)})
    cast = _cast(script, bible, shots_by_scene)
    if cast:
        blocks.append({"type": "cast", "items": cast})

    warnings: list[str] = []
    bare = [s.id for s in script.scenes if not (s.action or s.closing or _has_line_action(s))]
    if bare and mode == "script":
        warnings.append(
            f"{len(bare)} 场没有动作描写（老剧本没有这一层），读起来只有台词；"
            "可以对这几场「按意见重写」补上：" + "、".join(bare)
        )
    if mode == "shots" and not shots_by_scene:
        warnings.append("还没有分镜，按镜头展开不了，先按剧本排版")
        mode = "script"

    transitions = [_scene_transition(s, shots_by_scene.get(s.id)) for s in script.scenes]
    current_chapter = None
    for index, scene in enumerate(script.scenes):
        shots = shots_by_scene.get(scene.id, [])
        chapter = (chapters or {}).get(scene.id)
        if chapter and chapter != current_chapter:
            blocks.append({"type": "chapter", "text": chapter})
        current_chapter = chapter
        blocks.append(
            {
                "type": "scene",
                "no": index + 1,
                "id": scene.id,
                "heading": _heading(scene.heading) or scene.id,
                "time_label": scene.time_label,
                "meta": _scene_meta(scene, shots, mode),
            }
        )
        incoming = transitions[index]
        if index == 0 and incoming:
            blocks.append({"type": "transition", "text": "淡入：", "align": "left"})
        elif incoming in _IN_LABELS:
            blocks.append({"type": "transition", "text": _IN_LABELS[incoming], "align": "left"})
        expand = mode == "shots" and bool(shots)
        if scene.action and not (expand and _just_black(scene.action) and shots[0].black_screen):
            blocks.append({"type": "action", "text": _sub_ids(scene.action, names)})
        if expand:
            blocks.extend(_shot_blocks(shots, names))
        else:
            blocks.extend(_dialogue_blocks(scene, names))
        if scene.closing:
            blocks.append({"type": "action", "text": _sub_ids(scene.closing, names)})
        outgoing = transitions[index + 1] if index + 1 < len(transitions) else ""
        if outgoing in _OUT_LABELS:
            blocks.append({"type": "transition", "text": _OUT_LABELS[outgoing], "align": "right"})

    blocks.append({"type": "end", "text": "（完）"})
    stats = {
        "scenes": len(script.scenes),
        "duration_sec": script.total_duration_sec,
        "lines": sum(len(s.dialogue) for s in script.scenes),
        "shots": sum(len(v) for v in shots_by_scene.values()),
        "transitions": sum(1 for t in transitions if t),
    }
    return {
        "title": heading_title,
        "mode": mode,
        "blocks": blocks,
        "markdown": to_markdown(blocks),
        "stats": stats,
        "warnings": warnings,
    }


# ── 场次 ──────────────────────────────────────────────


def _scene_transition(scene: Scene, shots: list[Shot] | None) -> str:
    """片子实际怎么从上一场进来：有分镜听分镜（剪辑以它为准），没有就听剧本。"""
    if shots:
        return shots[0].transition
    return scene.transition_in


def _heading(text: str) -> str:
    """`INT. 出租屋 - 夜` → `内景　出租屋 - 夜`（中文剧本的写法）。"""
    text = (text or "").strip()
    match = _HEADING_PREFIX.match(text)
    if not match:
        return text
    words = re.findall(r"INT|EXT", match.group(1).upper())
    if len(words) > 1:
        prefix = "/".join(_HEADING_WORDS[w][0] for w in words) + "景"  # 内/外景
    else:
        prefix = _HEADING_WORDS[words[0]]
    rest = text[match.end():].strip()
    return f"{prefix}　{rest}" if rest else prefix


def _scene_meta(scene: Scene, shots: list[Shot], mode: str) -> str:
    parts = [f"约 {scene.duration_sec} 秒"]
    if mode == "shots" and shots:
        parts.append(f"{len(shots)} 个镜头")
    if scene.emotion:
        parts.append(scene.emotion)
    return " · ".join(parts)


def _just_black(text: str) -> bool:
    return text.strip().rstrip("。.") in {"黑屏", "黑场"}


def _has_line_action(scene: Scene) -> bool:
    return any(line.action for line in scene.dialogue)


def _dialogue_blocks(scene: Scene, names: dict[str, str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for line in scene.dialogue:
        if line.action:
            blocks.append({"type": "action", "text": _sub_ids(line.action, names)})
        blocks.append(
            {
                "type": "line",
                "speaker": _speaker(line.speaker, names),
                "parenthetical": line.parenthetical,
                "text": line.line,
            }
        )
    return blocks


def _shot_blocks(shots: list[Shot], names: dict[str, str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for position, shot in enumerate(shots, start=1):
        label = [f"镜 {position}"]
        if shot.black_screen:
            label.append("黑场")
        elif shot.camera:
            label.append(shot.camera)
        label.append(f"{shot.duration_sec} 秒")
        if position > 1 and shot.transition == "dissolve":
            label.append("叠化进")
        elif position > 1 and shot.transition == "fade":
            label.append("经黑场进")
        if shot.black_screen:
            picture = "黑屏。"
            if shot.screen_text:
                picture += f"画面文字：「{shot.screen_text}」。"
            if shot.sfx:
                picture += "音效：" + "、".join(SFX_LABELS.get(n, n) for n in shot.sfx) + "。"
        else:
            picture = _sub_ids(shot.visual_description, names)
        blocks.append({"type": "shot", "label": " · ".join(label), "text": picture})
        if shot.dialogue_text:
            blocks.append(
                {
                    "type": "line",
                    "speaker": _speaker(shot.dialogue_speaker or "", names),
                    "parenthetical": "画外" if shot.voice_offscreen else "",
                    "text": shot.dialogue_text,
                }
            )
        if shot.narration:
            blocks.append(
                {"type": "line", "speaker": "旁白", "parenthetical": "", "text": shot.narration}
            )
    return blocks


# ── 名字 ──────────────────────────────────────────────


def _name_map(bible: StoryBible | None) -> dict[str, str]:
    names = {NARRATOR: "旁白"}
    if bible is None:
        return names
    for group in (bible.characters, bible.settings, bible.props):
        for item in group:
            if item.name:
                names[item.id] = item.name
    return names


def _speaker(speaker: str, names: dict[str, str]) -> str:
    speaker = (speaker or "").strip()
    return names.get(speaker, speaker) or "（未标说话人）"


def _sub_ids(text: str, names: dict[str, str]) -> str:
    """把 char_N / loc_N / prop_N 换成名字；模型为了隔开 id 写的空格（「char_1 坐在」）一起去掉。"""
    text = (text or "").strip()
    out: list[str] = []
    last = 0
    for match in _ID_RE.finditer(text):
        name = names.get(match.group(0))
        if name is None:
            continue
        head = text[last : match.start()]
        if head.endswith(" ") and not _ascii_word(head.rstrip()[-1:]):
            head = head.rstrip(" ")
        out.append(head + name)
        last = match.end()
        if text[last : last + 1] == " " and not _ascii_word(text[last + 1 : last + 2]):
            last += 1
    out.append(text[last:])
    return "".join(out)


def _ascii_word(char: str) -> bool:
    return bool(char) and char.isascii() and char.isalnum()


def _cast(
    script: Script, bible: StoryBible | None, shots_by_scene: dict[str, list[Shot]]
) -> list[dict[str, str]]:
    """人物表：只列剧本里真正开口的（加上分镜里出镜的），按出场顺序。"""
    if bible is None:
        return []
    order: list[str] = []
    for scene in script.scenes:
        for line in scene.dialogue:
            if line.speaker not in order:
                order.append(line.speaker)
        for shot in shots_by_scene.get(scene.id, []):
            for cid in [*shot.character_in_frame, shot.dialogue_speaker or ""]:
                if cid and cid not in order:
                    order.append(cid)
    by_id = {c.id: c for c in bible.characters}
    items = []
    for cid in order:
        character = by_id.get(cid)
        if character is None:
            continue
        name = character.name + (f"（{character.age} 岁）" if character.age else "")
        items.append({"name": name, "note": _first_sentence(character.description)})
    return items


def _first_sentence(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    for stop in "。；！？":
        cut = text.find(stop)
        if 0 < cut < limit:
            return text[: cut + 1]
    return text if len(text) <= limit else text[:limit].rstrip("，,、 ") + "…"


# ── Markdown ──────────────────────────────────────────


def to_markdown(blocks: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for block in blocks:
        kind = block["type"]
        if kind == "title":
            out.append(f"# {block['text']}")
        elif kind == "logline":
            out.append(f"> {block['text']}")
        elif kind == "cast":
            out.append("**人物**\n\n" + "\n".join(
                f"- {item['name']}" + (f"：{item['note']}" if item["note"] else "")
                for item in block["items"]
            ))
            out.append("---")
        elif kind == "chapter":
            out.append(f"**—— {block['text']} ——**")
        elif kind == "scene":
            title = f"## {block['no']}. {block['heading']}"
            sub = " · ".join(p for p in (block.get("time_label"), block.get("meta")) if p)
            out.append(title + (f"\n\n*{sub}*" if sub else ""))
        elif kind == "transition":
            out.append(f"**{block['text']}**")
        elif kind == "action":
            out.append(block["text"])
        elif kind == "shot":
            out.append(f"`{block['label']}`　{block['text']}".rstrip())
        elif kind == "line":
            paren = f"（{block['parenthetical']}）" if block.get("parenthetical") else ""
            out.append(f"**{block['speaker']}**{paren}：{block['text']}")
        elif kind == "end":
            out.append(f"---\n\n{block['text']}")
    return "\n\n".join(out) + "\n"
