from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from videogen.models.bible import StoryBible
from videogen.models.llm_io import ShotsResponse
from videogen.models.project import Project
from videogen.models.script import Scene, Script
from videogen.models.storyboard import Shot, Storyboard
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.utils.duration import scale_durations
from videogen.utils.text import clean_spoken_text

logger = logging.getLogger(__name__)

__all__ = [
    "STORYBOARD_SYSTEM",
    "MergeReport",
    "generate_shots_for_scene",
    "merge_shots",
    "normalize_storyboard_durations",
    "plan_merge_buckets",
    "sanitize_video_prompt",
]

STORYBOARD_SYSTEM = """你是一位专业分镜师。根据剧本场景生成分镜脚本。

每个场景拆成 2-5 个镜头。每个镜头输出：
{
  "shot_no": 1,
  "duration_sec": 4,
  "camera": "景别 · 运镜（如：中景 · 固定 / 近景 · 推）",
  "visual_description": "中文画面描述",
  "character_in_frame": ["char_1"],
  "mood": "画面情绪",
  "video_prompt": "English prompt: subject + action + environment + lighting + style",
  "narration": null 或 "旁白文字",
  "dialogue_text": null 或 "台词文字",
  "dialogue_speaker": null 或 "char_id"
}

要求：
- video_prompt 必须全英文，面向 AI 视频生成模型
- video_prompt 里禁止出现 char_1 / loc_1 这类内部 id，必须写成具体的外貌/场景描述
- character_in_frame 必须是故事圣经里已有的角色 id
- 构图提示随 aspect_ratio 变化
- 台词尽量对准角色镜头
- 只输出 JSON: {"shots": [...]}"""

_ASPECT_HINTS = {
    "9:16": "vertical 9:16, emphasize vertical composition, full-body or upper-body framing",
    "16:9": "horizontal 16:9, emphasize wide composition, landscape framing",
    "1:1": "square 1:1, centered composition",
}

_CHAR_ID_RE = re.compile(r"\bchar_[0-9]+\b")
_LOC_ID_RE = re.compile(r"\bloc_[0-9]+\b")


def _format_brief_bible(bible: StoryBible) -> str:
    lines = ["角色（id 仅用于 character_in_frame / dialogue_speaker，不要写进 video_prompt）:"]
    for c in bible.characters:
        lines.append(f"  - id={c.id} 姓名={c.name} 外貌={c.visual_prompt or c.description}")
    lines.append("场景:")
    for s in bible.settings:
        lines.append(f"  - id={s.id} 名称={s.name} 环境={s.visual_prompt or s.description}")
    return "\n".join(lines)


def _character_phrase(bible: StoryBible, char_id: str) -> str:
    character = bible.get_character(char_id)
    if not character:
        return "the character"
    if character.visual_prompt:
        first_clause = re.split(r"[,.;，。；]", character.visual_prompt)[0].strip()
        if first_clause:
            return first_clause
    return character.description or "the character"


def sanitize_video_prompt(prompt: str, bible: StoryBible) -> str:
    """把 video_prompt 里的内部 id 换成对视频模型有意义的外貌/环境描述。"""
    text = prompt or ""

    def replace_char(match: re.Match[str]) -> str:
        return _character_phrase(bible, match.group(0))

    def replace_loc(match: re.Match[str]) -> str:
        for setting in bible.settings:
            if setting.id == match.group(0):
                return setting.name
        return "the location"

    text = _CHAR_ID_RE.sub(replace_char, text)
    text = _LOC_ID_RE.sub(replace_loc, text)
    text = re.sub(r"(\s*,\s*){2,}", ", ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


async def generate_shots_for_scene(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    scene: Scene,
) -> list[Shot]:
    """为一个场景生成镜头列表（id / shot_no / scene_id 由系统分配）。"""
    aspect_hint = _ASPECT_HINTS.get(project.aspect_ratio.value, _ASPECT_HINTS["9:16"])
    dialogue_text = "\n".join(f"  {d.speaker}: {d.line}" for d in scene.dialogue) or "（无对白）"

    user_prompt = f"""## 角色与场景参考
{_format_brief_bible(bible)}

## 本场剧本
场次: {scene.id}
标题: {scene.heading}
摘要: {scene.summary}
情绪: {scene.emotion}
时长: {scene.duration_sec} 秒
对白:
{dialogue_text}

## 要求
- 画面比例: {project.aspect_ratio.value} → {aspect_hint}
- 拆成 2-5 个镜头，所有镜头时长加起来必须正好等于 {scene.duration_sec} 秒
- video_prompt 必须是英文，且不得包含 char_N / loc_N 这类 id
- 台词对应的镜头要标记 dialogue_text 和 dialogue_speaker（speaker 用角色 id）
- 无对白的空镜/过渡镜 narration 可为 null

输出 JSON: {{"shots": [...]}}"""

    messages = [
        LLMMessage(role="system", content=STORYBOARD_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(messages, schema=ShotsResponse, temperature=0.5, max_tokens=4096)

    shots: list[Shot] = []
    for index, draft in enumerate(response.shots, start=1):
        shots.append(
            Shot(
                id=f"{scene.id}_shot_{index}",
                scene_id=scene.id,
                shot_no=index,
                duration_sec=draft.duration_sec,
                camera=draft.camera,
                visual_description=draft.visual_description,
                character_in_frame=draft.character_in_frame,
                mood=draft.mood,
                video_prompt=sanitize_video_prompt(draft.video_prompt, bible),
                narration=draft.narration,
                dialogue_text=draft.dialogue_text,
                dialogue_speaker=draft.dialogue_speaker,
            )
        )
    return shots


def normalize_storyboard_durations(storyboard: Storyboard, script: Script) -> bool:
    """让每个场景的镜头时长之和精确等于该场剧本时长。"""
    changed = False
    for scene in script.scenes:
        shots = storyboard.shots_for_scene(scene.id)
        if not shots:
            continue
        current = [s.duration_sec for s in shots]
        if sum(current) == scene.duration_sec:
            continue
        for shot, duration in zip(
            shots, scale_durations(current, scene.duration_sec, 1), strict=True
        ):
            shot.duration_sec = duration
        changed = True
    storyboard.duration_normalized = changed
    return changed


# ── 短镜头合并（省钱：少而长 vs 多而短）─────────────────


@dataclass
class MergeReport:
    """一次「合并短镜头」的结果（dry_run 也可用来只预览省多少钱）。"""

    dry_run: bool = True
    min_sec: float = 0.0
    max_sec: float = 0.0
    from_shots: int = 0
    to_shots: int = 0
    from_seconds: int = 0
    to_seconds: int = 0
    #: 每个被合并的桶：主镜头 id + 被吸收的镜头 id + 时长变化
    merged: list[dict] = field(default_factory=list)
    #: 因为主镜头已有文案而丢掉的旁白（有对白的台词绝不丢，也绝不合并）
    dropped_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "min_sec": self.min_sec,
            "max_sec": self.max_sec,
            "from_shots": self.from_shots,
            "to_shots": self.to_shots,
            "from_seconds": self.from_seconds,
            "to_seconds": self.to_seconds,
            "merged": len(self.merged),
            "dropped_lines": self.dropped_lines,
            "warnings": self.warnings,
        }


def _scene_order(storyboard: Storyboard) -> list[str]:
    """场景出现顺序（保持分镜原有顺序，不要按 set 打乱）。"""
    order: list[str] = []
    for shot in storyboard.shots:
        if shot.scene_id not in order:
            order.append(shot.scene_id)
    return order


def plan_merge_buckets(
    storyboard: Storyboard, *, min_sec: float, max_sec: float = 0.0
) -> list[list[Shot]]:
    """把每个场景的镜头按顺序切成「桶」，每桶最终只产出一个片段。

    规则（安全但省钱）：
    - 顺序不能乱：桶一定是连续的一段镜头（叙事顺序不能因为省钱被打乱）
    - 一个镜头能不能被吸收，看它的对白会不会被弄丢：
      * 没有对白 → 可以（旁白在主镜头需要时接过去，否则记录丢弃）
      * 有对白，而主镜头本来一个字都没有 → 可以（对白直接接管这条配音）
      * 有对白，且主镜头是**同一个说话人** → 可以（两句并成一条配音）
      * 其它情况（不同说话人、主镜头已有旁白）→ 不行：一条配音只能有一个声音
    - 桶只会为了凑够 `min_sec` 而继续吸收；到 `max_sec` 就封顶（0 = 不封顶）
    """
    min_sec = max(0.0, float(min_sec))
    max_sec = max(0.0, float(max_sec))
    buckets: list[list[Shot]] = []
    for scene_id in _scene_order(storyboard):
        run: list[Shot] = []
        for shot in storyboard.shots_for_scene(scene_id):
            if not run:
                run = [shot]
                continue
            room = max_sec <= 0 or sum(s.duration_sec for s in run) + shot.duration_sec <= max_sec
            if _can_absorb(run, shot) and _secs(run) < min_sec and room:
                run.append(shot)
                continue
            buckets.append(run)
            run = [shot]
        if run:
            buckets.append(run)
    return buckets


def _secs(shots: list[Shot]) -> int:
    return sum(int(s.duration_sec) for s in shots)


def _speaker_of(shot: Shot) -> str:
    return (shot.dialogue_speaker or "").strip()


def _has_text(shot: Shot) -> bool:
    return bool(clean_spoken_text(shot.dialogue_text) or clean_spoken_text(shot.narration))


def _can_absorb(run: list[Shot], shot: Shot) -> bool:
    """被吸收镜头的对白会不会丢？会丢就不许吸收。"""
    if not clean_spoken_text(shot.dialogue_text):
        return True
    primary = run[0]
    if not _has_text(primary):
        return True  # 主镜头本来没文案：对白接管，什么都不丢
    speaker = _speaker_of(primary)
    return bool(speaker) and speaker == _speaker_of(shot)


def _join_lines(first: str, second: str) -> str:
    """同一说话人的两句并成一条：已经带句末标点就不重复加。"""
    head = clean_spoken_text(first)
    tail = clean_spoken_text(second)
    if not head:
        return tail
    if not tail:
        return head
    if head[-1] in "。！？…!?.":
        return f"{head}{tail}"
    return f"{head}。{tail}"


def merge_shots(
    storyboard: Storyboard,
    *,
    min_sec: float,
    max_sec: float = 0.0,
    dry_run: bool = True,
) -> MergeReport:
    """把「太短的镜头」合并成更少更长的片段（真实视频模型按最短档位计费，短镜头是浪费）。

    `dry_run=True` 只算不写（前端先给用户看能省多少，确认后再真合并）。
    合并后主镜头的 id 保持不变，被吸收镜头的画面描述会追加到主镜头上，
    旁白在「主镜头本来没文案」时会接过来（不丢信息）。
    """
    buckets = plan_merge_buckets(storyboard, min_sec=min_sec, max_sec=max_sec)
    report = MergeReport(
        dry_run=dry_run,
        min_sec=max(0.0, float(min_sec)),
        max_sec=max(0.0, float(max_sec)),
        from_shots=len(storyboard.shots),
        to_shots=len(buckets),
        from_seconds=storyboard.total_duration_sec,
        to_seconds=sum(_secs(bucket) for bucket in buckets),
    )
    for bucket in buckets:
        if len(bucket) == 1:
            continue
        primary = bucket[0]
        absorbed = bucket[1:]
        report.merged.append(
            {
                "shot_id": primary.id,
                "absorbed": [s.id for s in absorbed],
                "from_sec": _secs(bucket),
                "shots": len(bucket),
            }
        )
        extra = [
            clean_spoken_text(s.visual_description) for s in absorbed if s.visual_description
        ]
        if extra:
            joined = "；".join(part for part in extra if part)
            if joined:
                primary.visual_description = "；".join(
                    part for part in (clean_spoken_text(primary.visual_description), joined) if part
                )
        for shot in absorbed:
            line = clean_spoken_text(shot.dialogue_text)
            if not line:
                continue
            if not clean_spoken_text(primary.dialogue_text):
                # 主镜头本来没有对白：让这条对白接管（原有的旁白会因此让位，报告里会记）
                primary.dialogue_text = line
                primary.dialogue_speaker = shot.dialogue_speaker
                continue
            primary.dialogue_text = _join_lines(primary.dialogue_text, line)
        carried_narration = "\n".join(
            clean_spoken_text(s.narration)
            for s in absorbed
            if clean_spoken_text(s.narration)
        )
        if not clean_spoken_text(primary.dialogue_text) and not clean_spoken_text(
            primary.narration
        ):
            if carried_narration:
                primary.narration = carried_narration
        elif carried_narration:
            for shot in absorbed:
                line = clean_spoken_text(shot.narration)
                if line:
                    report.dropped_lines.append(f"{shot.id}: {line}")
        if not dry_run:
            primary.duration_sec = _secs(bucket)

    if not dry_run and report.to_shots != report.from_shots:
        keep: list[Shot] = []
        counters: dict[str, int] = {}
        for bucket in buckets:
            primary = bucket[0]
            counters[primary.scene_id] = counters.get(primary.scene_id, 0) + 1
            primary.shot_no = counters[primary.scene_id]
            keep.append(primary)
        storyboard.shots = keep
        storyboard.audio_aligned = False
        storyboard.duration_normalized = False

    if report.dropped_lines:
        report.warnings.append(
            f"有 {len(report.dropped_lines)} 条旁白因为主镜头已有文案被丢弃（对白一条都不会丢）"
        )
    if report.to_shots == report.from_shots:
        report.warnings.append("没有可以合并的镜头（要么已经够长，要么都被对白占着）")
    return report
