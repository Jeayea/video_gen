from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass, field

from videogen.models.bible import StoryBible
from videogen.models.llm_io import ShotsResponse
from videogen.models.project import Project
from videogen.models.script import Scene, Script
from videogen.models.storyboard import DurationTier, Shot, Storyboard
from videogen.providers.llm.base import LLMMessage, LLMProvider
from videogen.utils.duration import fit_durations, plan_shot_count, scale_durations
from videogen.utils.text import clean_spoken_text

logger = logging.getLogger(__name__)

__all__ = [
    "STORYBOARD_SYSTEM",
    "MergeReport",
    "Segment",
    "absorb_bucket",
    "apply_duration_tier",
    "assign_segments",
    "chain_predecessors",
    "generate_shots_for_scene",
    "merge_shots",
    "merge_to_count",
    "normalize_storyboard_durations",
    "plan_merge_buckets",
    "sanitize_video_prompt",
    "segments_of",
]

STORYBOARD_SYSTEM = """你是一位专业分镜师。根据剧本场景生成分镜脚本。

每个场景拆成 2-5 个镜头（如果用户消息里给了明确的镜头数与时长硬约束，以用户消息为准）。
每个镜头输出：
{
  "shot_no": 1,
  "duration_sec": 4,
  "camera": "景别 · 运镜（如：中景 · 固定 / 近景 · 推）",
  "visual_description": "中文画面描述",
  "character_in_frame": ["char_1"],
  "mood": "画面情绪（建议用英文情绪词：shocked / wronged / determined …）",
  "video_prompt": "subject + action + environment + lighting（全英文，不要写标签）",
  "location": "loc_id（这一镜发生在哪个场景，必须是故事圣经里的场景 id）",
  "props_in_frame": ["prop_1"],
  "narration": null 或 "旁白文字",
  "dialogue_text": null 或 "台词文字",
  "dialogue_speaker": null 或 "char_id",
  "voice_offscreen": false
}

要求：
- video_prompt 必须全英文，面向 AI 视频生成模型；例外：
  画面里要出现的文字（黑板 / 招牌 / 横幅 / 屏幕）保留原文、用引号括起来，不要翻译
  （格式：the sign reads "<画面上的原文>"，原文照抄，不要换成它的英文意思）
- video_prompt 里禁止出现 char_1 / loc_1 这类内部 id，必须写成具体的外貌/场景描述
- **背景必须写实、写满**：environment 部分要说清「有什么陈设/道具、什么材质、光从哪来、
  前景与景深里有什么」，至少写到一个**具体的可见物**。
  只写 "in an office" / "in a room" 这类泛称、或压根不提背景，模型就会把背景画成一面
  空墙 —— 这种 prompt 一律不合格，必须重写成有层次的场景
- location 必须填故事圣经场景列表里真实存在的 id：视频生成时会用它的环境描述做**背景锚点**，
  跨镜头背景才不漂
- character_in_frame 必须是故事圣经里已有的角色 id
- props_in_frame 只填画面里**看得见**、且在故事圣经道具列表里的关键道具 id；没有就给空数组
- 构图提示随 aspect_ratio 变化
- 台词尽量对准角色镜头，**且说话人必须出现在 character_in_frame 里**（说话却不在画面里是穿帮）
- camera 必须同时写清景别与运镜（如「近景 · 缓推」）；一个镜头里只写**一个连续动作**，
  不要在一句 video_prompt 里塞多个画面切换
- 情绪强烈的镜头优先用中景/近景：大特写会让模型把表情演成夸张的舞台剧
- video_prompt 里描述表演要用克制的词（subtle / restrained / natural），不要写夸张副词
  （hysterically / dramatically / exaggeratedly）
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
    lines.append("场景（id 用于 location 字段；video_prompt 里要写成具体环境描述）:")
    for s in bible.settings:
        lines.append(f"  - id={s.id} 名称={s.name} 环境={s.visual_prompt or s.description}")
    if bible.props:
        lines.append("关键道具（id 用于 props_in_frame；video_prompt 里写成物品外观）:")
        for p in bible.props:
            lines.append(f"  - id={p.id} 名称={p.name} 外观={p.visual_prompt or p.description}")
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


#: 段落分镜下给分镜 LLM 的节奏要求（镜头可以短，计费按「段」算）
SEGMENT_SHOT_RULES = """- 同一场相邻的镜头会被编成一段（8-15 秒）**一次生成、段内硬切**，
  计费按段算：所以镜头可以短（2-6 秒），每个镜头只聚焦一个画面单元（一个动作 / 一个反应 / 一个特写）
- 段内靠换景别、换角度、换拍摄对象形成剪辑节奏；同一场里不要换地点
- 有台词的镜头时长 ≥ 台词字数 ÷ 4.5 + 1 秒，演不完的台词拆到下一个镜头"""


def duration_constraints(scene_sec: int, tier: DurationTier | None) -> str:
    """把视频模型的计费档位翻译成给 LLM 的硬约束（没档位就返回空串）。"""
    if tier is None or not tier.active:
        return ""
    planned = plan_shot_count(
        scene_sec, min_sec=tier.min_sec, max_sec=tier.max_sec, target_sec=tier.target_sec
    )
    planned = max(1, planned)
    low = f"{tier.min_sec:g}"
    high = f"{tier.max_sec:g}" if tier.max_sec and tier.max_sec >= tier.min_sec else "不限"
    per_shot = max(1, round(scene_sec / planned))
    lines = [
        f"- 本场只拆 **{planned}** 个镜头：视频模型按最短 {low} 秒/镜头计费，"
        f"镜头短于 {low} 秒等于白花钱，所以宁少勿碎",
        f"- 每个镜头时长必须在 {low}~{high} 秒之间，加起来正好等于 {scene_sec} 秒"
        f"（每个约 {per_shot} 秒）",
        "- 镜头少的时候要用「缓慢运镜 / 一个动作走完」来撑满时长，不要靠切碎镜头制造信息量",
    ]
    if planned == 1:
        lines.append("- 本场只有一个镜头：用连续动作 + 缓慢推/摇，别在描述里写多个画面切换")
    return "\n".join(lines)


async def generate_shots_for_scene(
    llm: LLMProvider,
    project: Project,
    bible: StoryBible,
    scene: Scene,
    *,
    tier: DurationTier | None = None,
    environment: str = "",
    story_context: str = "",
    segment_mode: bool = False,
    revision: list[Shot] | None = None,
    feedback: str = "",
    target_shot: str = "",
) -> list[Shot]:
    """为一个场景生成镜头列表（id / shot_no / scene_id 由系统分配）。

    `revision` + `feedback`：按修改意见重写这一场已有的镜头；再给 `target_shot`（镜头 id）
    时只重写那一个镜头，返回只含一个镜头的列表（id / 时长 / 段落由调用方沿用原镜头）。

    `tier` 是视频模型的计费档位：给了就把「本场几个镜头、每镜多少秒」写成硬约束，
    让模型一开始就产出「少而长」的分镜（事后合并只能补救，不如一开始就不浪费）。

    `environment` 是长篇大纲展开时留下的环境线索（背景不够丰富的补丁之一）；
    `story_context` 是这一场在整篇里的位置（章标题/情节点），避免分镜脱离全篇。
    """
    aspect_hint = _ASPECT_HINTS.get(project.aspect_ratio.value, _ASPECT_HINTS["9:16"])
    dialogue_text = "\n".join(f"  {d.speaker}: {d.line}" for d in scene.dialogue) or "（无对白）"
    constraints = duration_constraints(scene.duration_sec, tier)
    shot_count_rule = (
        "按上面的镜头数与时长硬约束拆分"
        if constraints
        else f"拆成 2-5 个镜头，所有镜头时长加起来必须正好等于 {scene.duration_sec} 秒"
    )

    context_block = f"\n## 全篇位置\n{story_context}\n" if story_context else ""
    env_block = (
        f"\n## 本场环境线索（必须落在 video_prompt 的 environment 里）\n{environment}\n"
        if environment
        else ""
    )
    tier_block = f"\n## 时长硬约束（视频模型计费档位）\n{constraints}\n" if constraints else ""
    if segment_mode and not constraints:
        tier_block = f"\n## 段落节奏（视频按段生成）\n{SEGMENT_SHOT_RULES}\n"
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
{context_block}{env_block}{tier_block}{_revision_block(revision, feedback, target_shot)}
## 要求
- 画面比例: {project.aspect_ratio.value} → {aspect_hint}
- {shot_count_rule}
- video_prompt 必须是英文，且不得包含 char_N / loc_N 这类 id；
  画面里要出现的文字（黑板 / 招牌 / 横幅 / 屏幕）保留原文、用引号括起来，不要翻译
- 每个镜头都要填 location（场景 id），video_prompt 的背景要和该场景一致
- 台词对应的镜头要标记 dialogue_text 和 dialogue_speaker（speaker 用角色 id）
- **一个镜头只有一个说话人**：dialogue_text 只放 dialogue_speaker 这一个人的话
  （整条会用他的声音念），两个人一来一回就拆成两个镜头（正反打）；
  台词念不完（字数 ÷ 4.5 秒）也拆到下一个镜头
- 说话人**不在画面里**（门外、电话里、隔墙喊话、画外音）时 voice_offscreen=true，
  且 character_in_frame 不要放他；画面拍听见声音的人或那扇门
- 无对白的空镜/过渡镜 narration 可为 null

输出 JSON: {{"shots": [...]}}"""

    messages = [
        LLMMessage(role="system", content=STORYBOARD_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
    ]
    response = await llm.chat_json(messages, schema=ShotsResponse, temperature=0.5, max_tokens=4096)

    known_locs = {s.id for s in bible.settings}
    known_props = {p.id for p in bible.props}
    shots: list[Shot] = []
    for index, draft in enumerate(response.shots, start=1):
        location = str(draft.location or "").strip()
        if location and location not in known_locs:
            location = ""
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
                location=location,
                props_in_frame=[p for p in draft.props_in_frame if p in known_props],
                narration=draft.narration,
                dialogue_text=draft.dialogue_text,
                dialogue_speaker=draft.dialogue_speaker,
                voice_offscreen=bool(draft.voice_offscreen and draft.dialogue_text),
            )
        )
    return shots


def _shot_brief(shot: Shot) -> str:
    parts = [
        f"{shot.id}（{shot.duration_sec} 秒）",
        f"景别: {shot.camera}" if shot.camera else "",
        f"画面: {shot.visual_description}" if shot.visual_description else "",
        f"在画面里: {', '.join(shot.character_in_frame)}" if shot.character_in_frame else "",
        f"地点: {shot.location}" if shot.location else "",
        f"台词（{shot.dialogue_speaker}）: {shot.dialogue_text}" if shot.dialogue_text else "",
        f"旁白: {shot.narration}" if shot.narration else "",
        f"video_prompt: {shot.video_prompt}" if shot.video_prompt else "",
    ]
    return "  - " + "；".join(p for p in parts if p)


def _revision_block(revision: list[Shot] | None, feedback: str, target_shot: str) -> str:
    """重写分镜时：本场当前的镜头 + 修改意见；只重写一个镜头时把输出限制成一个。"""
    if not revision:
        return ""
    note = " ".join(str(feedback or "").split()) or "整体重写一版，画面更有表现力"
    current = "\n".join(_shot_brief(shot) for shot in revision)
    target = next((shot for shot in revision if shot.id == target_shot), None)
    if target is None:
        scope = "重写本场的全部镜头。意见没提到的部分（台词分配、时长、地点）尽量保持。"
    else:
        scope = (
            f"**只重写镜头 {target.id}**，其余镜头不动，只是给你看上下文。\n"
            f"输出 {{\"shots\": [...]}} 里**只放这一个镜头**，"
            f"duration_sec 保持 {target.duration_sec}，台词和说话人除非意见要求否则不改。"
            "这一条优先于下面关于镜头数的要求。"
        )
    return f"""
## 按修改意见重写
本场当前的镜头：
{current}

修改意见（优先级最高）：{note}
{scope}
"""


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
        absorb_bucket(bucket[0], bucket[1:], report)
        if not dry_run:
            bucket[0].duration_sec = _secs(bucket)

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


def absorb_bucket(primary: Shot, absorbed: list[Shot], report: MergeReport) -> None:
    """把 `absorbed` 这些镜头并进主镜头（文案、画面描述、丢线记录都记进 report）。

    对白处理规则见 `_can_absorb`：同说话人并句、主镜头无文案时对白接管，
    否则调用方根本不该吸收它。
    """
    report.merged.append(
        {
            "shot_id": primary.id,
            "absorbed": [shot.id for shot in absorbed],
            "from_sec": _secs([primary, *absorbed]),
            "shots": len(absorbed) + 1,
        }
    )
    extra = [
        clean_spoken_text(shot.visual_description) for shot in absorbed if shot.visual_description
    ]
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
            # 主镜头本来没有对白：让这条对白接管（原有旁白会让位，下面会记）
            primary.dialogue_text = line
            primary.dialogue_speaker = shot.dialogue_speaker
            continue
        primary.dialogue_text = _join_lines(primary.dialogue_text, line)
    carried_narration = "\n".join(
        clean_spoken_text(shot.narration) for shot in absorbed if clean_spoken_text(shot.narration)
    )
    if not clean_spoken_text(primary.dialogue_text) and not clean_spoken_text(primary.narration):
        if carried_narration:
            primary.narration = carried_narration
    elif carried_narration:
        for shot in absorbed:
            line = clean_spoken_text(shot.narration)
            if line:
                report.dropped_lines.append(f"{shot.id}: {line}")


def merge_to_count(shots: list[Shot], keep: int) -> tuple[list[list[Shot]], list[str]]:
    """把一场里的镜头合并到**最多 keep 个**（分镜阶段按档位控制镜头数用）。

    先吸收「没有对白 / 能被吸收」的最短的那一个；实在合不动（剩下的都是不同说话人的对白）
    就停下并告警 —— 宁可多留几个镜头，也不丢谁的声音。
    """
    keep = max(1, int(keep))
    buckets: list[list[Shot]] = [[shot] for shot in shots]
    warnings: list[str] = []
    while len(buckets) > keep:
        order = sorted(
            range(1, len(buckets)),
            key=lambda index: (any(_has_text(s) for s in buckets[index]), _secs(buckets[index])),
        )
        merged = False
        for index in order:
            run = _try_absorb(buckets[index - 1], buckets[index])
            if run is None:
                continue
            buckets[index - 1] = run
            buckets.pop(index)
            merged = True
            break
        if not merged:
            labels = "、".join(bucket[0].id for bucket in buckets)
            warnings.append(
                f"这一场有 {len(buckets)} 个镜头无法再合并（不同说话人的对白不能共用一条配音）："
                f"{labels}"
            )
            break
    return buckets, warnings


def _try_absorb(primary_run: list[Shot], extra: list[Shot]) -> list[Shot] | None:
    """把 extra 整个并进 primary_run；只要有一个镜头不该被吸收就返回 None。"""
    run = list(primary_run)
    for shot in extra:
        if not _can_absorb(run, shot):
            return None
        run.append(shot)
    return run


def apply_duration_tier(
    storyboard: Storyboard, script: Script, tier: DurationTier
) -> list[str]:
    """按视频模型的计费档位把分镜「摊平」：每场镜头数合适、每个镜头都在档位内。

    做的事（按场）：
    1. 算出这一场该有几个镜头（`plan_shot_count`：每个镜头 ≥ min、≤ max，尽量贴近目标时长）
    2. 多出来的镜头按「不丢对白」的规则合并掉（合不动就保留并告警）
    3. 把这一场的时长按 `fit_durations` 重新分配（不足 min 的抬到 min —— 反正也是花 min 的钱）

    返回告警列表。**不写盘**，由调用方决定保存。
    """
    warnings: list[str] = []
    kept: list[Shot] = []
    counters: dict[str, int] = {}
    for scene in script.scenes:
        shots = storyboard.shots_for_scene(scene.id)
        if not shots:
            continue
        planned = plan_shot_count(
            scene.duration_sec,
            min_sec=tier.min_sec,
            max_sec=tier.max_sec,
            target_sec=tier.target_sec,
        )
        if planned <= 0:
            kept.extend(shots)
            continue
        buckets, merge_warnings = merge_to_count(shots, planned)
        warnings.extend(merge_warnings)
        report = MergeReport(min_sec=tier.min_sec, max_sec=tier.max_sec)
        primaries: list[Shot] = []
        for bucket in buckets:
            primary = bucket[0]
            if len(bucket) > 1:
                absorb_bucket(primary, bucket[1:], report)
            primaries.append(primary)
        durations = fit_durations(
            len(primaries),
            scene.duration_sec,
            min_sec=round(tier.min_sec),
            max_sec=round(tier.max_sec),
        )
        if scene.duration_sec < len(primaries) * tier.min_sec:
            # 本场时长不够每个镜头的最短档位：会让片子比剧本长一点，但钱是一样的
            warnings.append(
                f"场次 {scene.id} 只有 {scene.duration_sec}s，不够 "
                f"{len(primaries)} 个镜头各自最短 {tier.min_sec:g}s：已按 "
                f"{sum(durations)}s 排（否则也是花同样的钱）"
            )
        for primary, seconds in zip(primaries, durations, strict=True):
            primary.duration_sec = seconds
        counters[scene.id] = 0
        for primary in primaries:
            counters[scene.id] += 1
            primary.shot_no = counters[scene.id]
            kept.append(primary)
        if report.dropped_lines:
            warnings.extend(f"合并 {primary.id} 时丢弃旁白 {line}" for line in report.dropped_lines)
    storyboard.shots = kept
    storyboard.duration_tier = tier
    storyboard.tier_warnings = warnings
    storyboard.audio_aligned = False
    storyboard.duration_normalized = True
    return warnings


# ── 段落分镜 ──────────────────────────────────────────


@dataclass
class Segment:
    """一个视频生成任务：同一场里相邻的 1-N 个镜头，段内硬切。"""

    id: str
    scene_id: str
    shots: list[Shot] = field(default_factory=list)

    @property
    def duration_sec(self) -> int:
        return sum(max(1, int(s.duration_sec)) for s in self.shots)

    @property
    def shot_ids(self) -> list[str]:
        return [s.id for s in self.shots]


def assign_segments(
    storyboard: Storyboard,
    *,
    target_sec: float = 12.0,
    max_sec: float = 15.0,
    min_sec: float = 0.0,
    max_shots: int = 4,
) -> list[Segment]:
    """把镜头编成段落（写回每个镜头的 `segment_id`），返回段落列表。

    规则（确定性，同样的分镜永远编出同样的段）：
    - 只在同一场内编段；地点（`location`）变了就断开
    - 装到 `target_sec` 就收段；再装会超过 `max_sec` 或超过 `max_shots` 个镜头也收段
    - 场末尾的段短于模型最短档位 `min_sec` 时，能并进前一段（不超上限）就并进去，省一次最短计费
    - 单个镜头本身超过上限也自成一段（不拆镜头，交给模型档位去裁）
    """
    max_sec = max(1.0, float(max_sec))
    target_sec = min(max(1.0, float(target_sec)), max_sec)
    max_shots = max(1, int(max_shots))
    segments: list[Segment] = []
    scene_order: list[str] = []
    by_scene: dict[str, list[Shot]] = {}
    for shot in storyboard.shots:
        if shot.scene_id not in by_scene:
            scene_order.append(shot.scene_id)
            by_scene[shot.scene_id] = []
        by_scene[shot.scene_id].append(shot)

    for scene_id in scene_order:
        groups: list[list[Shot]] = []
        current: list[Shot] = []
        for shot in by_scene[scene_id]:
            if current:
                total = sum(max(1, int(s.duration_sec)) for s in current)
                loc_changed = bool(
                    shot.location and current[-1].location and shot.location != current[-1].location
                )
                full = (
                    total >= target_sec
                    or total + max(1, int(shot.duration_sec)) > max_sec
                    or len(current) >= max_shots
                )
                if loc_changed or full:
                    groups.append(current)
                    current = []
            current.append(shot)
        if current:
            groups.append(current)
        # 场末尾太短的一段：并进前一段（同地点、不超上限、不超镜头数）
        if len(groups) >= 2 and min_sec > 0:
            last, prev = groups[-1], groups[-2]
            last_sec = sum(max(1, int(s.duration_sec)) for s in last)
            prev_sec = sum(max(1, int(s.duration_sec)) for s in prev)
            same_place = not (
                last[0].location and prev[-1].location and last[0].location != prev[-1].location
            )
            if (
                last_sec < min_sec
                and same_place
                and prev_sec + last_sec <= max_sec
                and len(prev) + len(last) <= max_shots
            ):
                groups[-2] = prev + last
                groups.pop()
        for index, group in enumerate(groups, start=1):
            segment = Segment(id=f"{scene_id}_seg_{index}", scene_id=scene_id, shots=group)
            for shot in group:
                shot.segment_id = segment.id
            segments.append(segment)
    return segments


def chain_predecessors(
    segments: list[Segment], *, waived: list[str] | tuple[str, ...] = ()
) -> dict[str, Segment]:
    """段 id → 可以接戏的上一段：紧挨着、同一场、交界处的两个镜头在同一个地点。

    `waived`：用户不要接戏的段（接戏帧被拒后选了「改用画的关键帧」）。
    """
    found: dict[str, Segment] = {}
    for previous, current in itertools.pairwise(segments):
        if current.id in waived or not previous.shots or not current.shots:
            continue
        if previous.scene_id != current.scene_id:
            continue
        if (previous.shots[-1].location or "") != (current.shots[0].location or ""):
            continue
        found[current.id] = previous
    return found


def segments_of(storyboard: Storyboard) -> list[Segment]:
    """按镜头上已有的 `segment_id` 还原段落（没编段的镜头各自成段 = 单镜生成）。"""
    segments: list[Segment] = []
    index: dict[str, Segment] = {}
    for shot in storyboard.shots:
        key = shot.segment_id
        if not key:
            segments.append(Segment(id=shot.id, scene_id=shot.scene_id, shots=[shot]))
            continue
        segment = index.get(key)
        if segment is None:
            segment = Segment(id=key, scene_id=shot.scene_id)
            index[key] = segment
            segments.append(segment)
        segment.shots.append(shot)
    return segments
