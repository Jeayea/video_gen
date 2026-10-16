from __future__ import annotations

import itertools
import re

from videogen.models.bible import StoryBible
from videogen.models.consistency import ConsistencyIssue, ConsistencyReport, IssueSeverity
from videogen.models.script import NARRATOR, Scene, Script
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.world_state import WorldState
from videogen.services import explainer_service
from videogen.utils.duration import deviation_ratio, speech_chars, speech_seconds

#: 时长偏离目标超过这个比例算问题
DURATION_TOLERANCE = 0.2

#: 「X说/道」启发式里常见的中文误报词
_FALSE_POSITIVE_NAMES = {
    "知道",
    "难道",
    "难说",
    "听说",
    "虽说",
    "就是说",
    "不知",
    "只得",
    "只好",
    "倒是",
    "不说",
    "再说",
    "想说",
    "要说",
}

#: 以代词/指示词开头的匹配不是人名（第一人称小说里「她大笑」「我也不知道」「谁知道」）
_NON_NAME_PREFIXES = tuple("我你他她它您咱谁这那")

#: 台词念完需要的时长超过场次时长的倍数：超过 1 倍提示，超过 2 倍（严格模式）拦确认
DIALOGUE_OVERLOAD_ERROR_RATIO = 2.0


def validate_script(
    script: Script,
    bible: StoryBible,
    world_state: WorldState | None = None,
    target_duration_sec: int = 0,
    *,
    strict: bool = False,
    partial: bool = False,
) -> ConsistencyReport:
    """检查剧本与故事圣经之间的一致性。

    - `strict=True`（`SCRIPT_STRICT`，默认开）时，**结构硬伤**会升级成 ERROR ——
      ERROR 会让「确认剧本」被拒绝（可 force 跳过）
    - `partial=True` 表示「这只是长篇剧集里选中的几章」：此时**不能**因为结尾还悬着就报警，
      因为后面还要继续展开别的章（否则选前两章必然被拦）
    """
    issues: list[ConsistencyIssue] = []
    known_names = bible.all_names()
    known_ids = {c.id for c in bible.characters}

    for scene in script.scenes:
        issues.extend(_check_dialogue_speakers(scene, known_ids))
        issues.extend(_check_names_in_text(scene, known_names))
        issues.extend(_check_numbers(scene, bible))
        issues.extend(_check_scene_content(scene, strict=strict))
        issues.extend(_check_dialogue_load(scene, strict=strict))

    issues.extend(_check_timeline(script))
    issues.extend(_check_structure(script, strict=strict, partial=partial))
    issues.extend(_check_duration(script, target_duration_sec))
    if world_state:
        issues.extend(_check_world_state_open_threads(world_state))

    return ConsistencyReport(stage="script", issues=issues)


def has_errors(report: ConsistencyReport | None) -> list[ConsistencyIssue]:
    """报告里 severity=error 的条目（确认阶段的拦截依据）。"""
    if report is None:
        return []
    return [issue for issue in report.issues if issue.severity == IssueSeverity.ERROR]


#: 结尾出现这些标记，说明「悬念/问题」还挂着没收
_OPEN_END_MARKERS = ("？", "?", "未完", "待续", "究竟", "到底", "会如何", "悬念")


def _check_structure(
    script: Script, *, strict: bool = False, partial: bool = False
) -> list[ConsistencyIssue]:
    """结构硬伤（确定性可判的部分）。

    钩子/转折/高潮这类需要读故事才能判断的，交给 `script_service.review_structure`
    的 LLM 评审；这里只抓「机器一定能看出来」的：

    - 相邻场内容完全重复（= 这场不推进，纯注水）
    - 结尾一场还挂着未闭合的问题/悬念标记（= 故事没落地）
    - 场次之间没有任何冲突语汇（= 平铺直叙，缺少对抗）
    """
    issues: list[ConsistencyIssue] = []
    scenes = script.scenes
    if not scenes:
        return issues

    # 只有「精确证据」才够格拦确认（相邻场一模一样 = 一定没推进）；
    # 「结尾带问号」「找不到冲突词」是提示级线索 —— 正常台词里问句很常见，
    # 拿它当 ERROR 会把好剧本也拦住，所以永远是 WARNING
    issues.extend(_check_adjacent_duplicates(script, strict=strict))

    last = scenes[-1]
    tail = _scene_text(last)
    # 「结尾没落地」只在**整片**的情况下才成立：长篇剧集选了前几章时，悬念正是要留的
    if not partial and any(marker in tail for marker in _OPEN_END_MARKERS):
        issues.append(
            ConsistencyIssue(
                severity=IssueSeverity.WARNING,
                scene_id=last.id,
                category="structure",
                message=f"结尾场次 {last.id} 仍以悬念/问题收尾，故事没有落地",
                suggestion="补一个收束性的结尾（结果/代价/新常态），悬念留给下一季也要先收当前这条线",
            )
        )

    if len(scenes) >= 3:
        all_text = " ".join(_scene_text(scene) for scene in scenes)
        if not any(word in all_text for word in _CONFLICT_WORDS):
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.WARNING,
                    category="structure",
                    message="全剧找不到任何对抗/冲突语汇，故事可能平铺直叙",
                    suggestion="至少给主角一个明确的目标与阻力（对手、期限、代价）",
                )
            )
    return issues


def _check_adjacent_duplicates(script: Script, *, strict: bool = False) -> list[ConsistencyIssue]:
    """相邻两场内容一字不差 = 一定没推进（精确证据，严格模式下拦确认）。"""
    hard = IssueSeverity.ERROR if strict else IssueSeverity.WARNING
    issues: list[ConsistencyIssue] = []
    for previous, current in itertools.pairwise(script.scenes):
        before = _scene_text(previous)
        after = _scene_text(current)
        if before and before == after:
            issues.append(
                ConsistencyIssue(
                    severity=hard,
                    scene_id=current.id,
                    category="structure",
                    message=f"场次 {current.id} 与上一场内容完全相同，剧情没有推进",
                    suggestion="删掉重复场次，或写出新的信息/冲突/转折",
                )
            )
    return issues


def _scene_text(scene: Scene) -> str:
    """一场戏的可比文本（概述 + 台词 + 情绪）——用于「是否重复/是否推进」判断。"""
    return " ".join(
        str(part or "").strip()
        for part in (
            scene.summary,
            scene.emotion,
            " ".join(line.line for line in scene.dialogue),
        )
    ).strip()


#: 冲突/对抗语汇（粗线索，只用来提示「可能没有戏」）
_CONFLICT_WORDS = (
    "拒绝",
    "反对",
    "阻止",
    "争夺",
    "抢",
    "威胁",
    "逼迫",
    "背叛",
    "揭穿",
    "反击",
    "报复",
    "打压",
    "竞争",
    "对峙",
    "冲突",
    "吵",
    "怒",
    "恨",
    "不甘",
)


def repair_storyboard(
    storyboard: Storyboard, script: Script, bible: StoryBible
) -> tuple[Storyboard, list[str]]:
    """确定性修复分镜里**不需要创作者决策**的问题，返回 `(修好的分镜, 修复记录)`。

    目前只修一条，但它同时影响三件事（画面、配音、参考图）：

    - **说话人不在画面里** → 把说话人补进 `character_in_frame`。
      不改的话：这个镜头不会带该角色的参考图（脸会漂）、外貌锚点也不会写上他，
      而且「人不在画面却在说话」本身就是穿帮。

    不修任何需要人判断的东西（比如缺 video_prompt、悬念没闭合），那些留给人工/重新生成。
    """
    fixed: list[str] = []
    known = {c.id for c in bible.characters}
    for shot in storyboard.shots:
        speaker = (shot.dialogue_speaker or "").strip()
        if not speaker or speaker in shot.character_in_frame or speaker not in known:
            continue  # 说话人不存在时交给校验报 ERROR，不能猜
        if shot.voice_offscreen:
            continue  # 画外音：人本来就不在画面里（门外喊话），补进去反而穿帮
        before = list(shot.character_in_frame)
        shot.character_in_frame = [*before, speaker]
        fixed.append(f"镜头 {shot.id}：把说话人 {speaker} 补进画面角色（原为 {before}）")
    return storyboard, fixed


def validate_storyboard(
    storyboard: Storyboard,
    script: Script,
    bible: StoryBible,
) -> list[ConsistencyIssue]:
    """分镜层校验：说话人在场、id 合法、video_prompt 就绪。"""
    issues: list[ConsistencyIssue] = []
    known_ids = {c.id for c in bible.characters}
    scene_ids = script.scene_ids()

    for scene in script.scenes:
        shots = storyboard.shots_for_scene(scene.id)
        if not shots:
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.WARNING,
                    scene_id=scene.id,
                    category="storyboard",
                    message=f"场景 {scene.id} 没有任何镜头",
                    suggestion="重新生成分镜或手工补镜头",
                )
            )
        elif not 2 <= len(shots) <= 5:
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.INFO,
                    scene_id=scene.id,
                    category="storyboard",
                    message=f"场景 {scene.id} 有 {len(shots)} 个镜头（建议 2-5 个）",
                )
            )

    for shot in storyboard.shots:
        if shot.scene_id not in scene_ids:
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.ERROR,
                    scene_id=shot.scene_id,
                    shot_id=shot.id,
                    category="storyboard",
                    message=f"镜头 {shot.id} 指向不存在的场景 {shot.scene_id}",
                    suggestion="检查分镜与剧本是否来自同一次生成",
                )
            )

        for char_id in shot.character_in_frame:
            if char_id not in known_ids:
                issues.append(
                    ConsistencyIssue(
                        severity=IssueSeverity.WARNING,
                        scene_id=shot.scene_id,
                        shot_id=shot.id,
                        category="name",
                        message=f"镜头 {shot.id} 画面角色 '{char_id}' 不在故事圣经中",
                        suggestion="检查拼写，或把该角色加入故事圣经",
                    )
                )

        if shot.dialogue_speaker:
            if shot.dialogue_speaker not in known_ids:
                issues.append(
                    ConsistencyIssue(
                        severity=IssueSeverity.ERROR,
                        scene_id=shot.scene_id,
                        shot_id=shot.id,
                        category="name",
                        message=f"镜头 {shot.id} 的说话人 '{shot.dialogue_speaker}' 不在角色列表中",
                        suggestion="改为故事圣经里的角色 id",
                    )
                )
            elif (
                shot.dialogue_speaker not in shot.character_in_frame
                and not shot.voice_offscreen
            ):
                issues.append(
                    ConsistencyIssue(
                        severity=IssueSeverity.WARNING,
                        scene_id=shot.scene_id,
                        shot_id=shot.id,
                        category="presence",
                        message=(
                            f"镜头 {shot.id} 的说话人 {shot.dialogue_speaker} "
                            "不在该镜头的画面角色里"
                        ),
                        suggestion="把说话人加入 character_in_frame，或补一个反应镜头",
                    )
                )

        issues.extend(_check_shot_voice(shot, script))

        if not shot.video_prompt.strip():
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.ERROR,
                    scene_id=shot.scene_id,
                    shot_id=shot.id,
                    category="storyboard",
                    message=f"镜头 {shot.id} 缺少 video_prompt",
                    suggestion="补写英文视频 prompt 后再进入视频生成",
                )
            )
        elif not shot.video_prompt.isascii():
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.WARNING,
                    scene_id=shot.scene_id,
                    shot_id=shot.id,
                    category="storyboard",
                    message=f"镜头 {shot.id} 的 video_prompt 含有非英文字符",
                    suggestion="视频模型对英文 prompt 更稳定，建议改写为纯英文",
                )
            )

        if shot.duration_sec <= 0:
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.ERROR,
                    scene_id=shot.scene_id,
                    shot_id=shot.id,
                    category="duration",
                    message=f"镜头 {shot.id} 时长为 {shot.duration_sec}s",
                    suggestion="重新生成该场分镜",
                )
            )

    return issues


#: 判断「混进了别人的台词」时，台词至少要有这么多字（太短的「好」「嗯」谁都可能说）
_MIXED_LINE_MIN_CHARS = 4


def _check_shot_voice(shot: Shot, script: Script) -> list[ConsistencyIssue]:
    """一条配音只有一个声音：别人的台词混进这个镜头会被同一个音色念出来；台词太长会拉长镜头。"""
    text = shot.dialogue_text or ""
    if not text.strip():
        return []
    issues: list[ConsistencyIssue] = []
    scene = next((s for s in script.scenes if s.id == shot.scene_id), None)
    if scene and shot.dialogue_speaker:
        own = {_compact(d.line) for d in scene.dialogue if d.speaker == shot.dialogue_speaker}
        compact_text = _compact(text)
        others = sorted({
            d.speaker
            for d in scene.dialogue
            if d.speaker != shot.dialogue_speaker
            and len(line := _compact(d.line)) >= _MIXED_LINE_MIN_CHARS
            and line not in own
            and line in compact_text
        })
        if others:
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.ERROR,
                    scene_id=shot.scene_id,
                    shot_id=shot.id,
                    category="presence",
                    message=(
                        f"镜头 {shot.id} 的台词里混进了 {'、'.join(others)} 的台词，"
                        f"但整条会用 {shot.dialogue_speaker} 的声音念"
                    ),
                    suggestion="把不同人的台词拆到各自的镜头里（一个镜头只有一个说话人）",
                )
            )
    needed = speech_seconds(text)
    if shot.duration_sec > 0 and needed > shot.duration_sec * 2:
        issues.append(
            ConsistencyIssue(
                severity=IssueSeverity.WARNING,
                scene_id=shot.scene_id,
                shot_id=shot.id,
                category="duration",
                message=(
                    f"镜头 {shot.id} 的台词 {speech_chars(text)} 字，念完约 {needed:.0f} 秒，"
                    f"镜头只有 {shot.duration_sec} 秒"
                ),
                suggestion="拆成多个镜头或删减台词（否则配音会把镜头拉长）",
            )
        )
    return issues


def _compact(text: str) -> str:
    return "".join(ch for ch in text or "" if ch.isalnum())


def merge_storyboard_issues(
    report: ConsistencyReport, extra: list[ConsistencyIssue]
) -> ConsistencyReport:
    return ConsistencyReport(stage="script+storyboard", issues=[*report.issues, *extra])



def validate_explainer_script(
    script: Script,
    bible: StoryBible,
    target_duration_sec: int = 0,
    *,
    strict: bool = False,
    source_material: str = "",
    min_quote_chars: int = 8,
) -> ConsistencyReport:
    """科普项目的剧本检查（docs/EXPLAINER_DESIGN.md 第 4 节）。

    沿用：说话人（`narrator` 是保留 id，放行）、空场 / 重复场、时长。
    跳过：「X说」人名（旁白里常有牛顿、爱因斯坦）、数字比对（由事实引用管）、时间线、对抗语汇。
    换成：事实引用检查、旁白总量超时长、旁白以外的说话人提示。
    """
    issues: list[ConsistencyIssue] = []
    known_ids = {c.id for c in bible.characters} | {NARRATOR}
    for scene in script.scenes:
        issues.extend(_check_dialogue_speakers(scene, known_ids))
        issues.extend(_check_scene_content(scene, strict=strict))
    issues.extend(_check_adjacent_duplicates(script, strict=strict))
    issues.extend(_check_duration(script, target_duration_sec))
    issues.extend(explainer_service.check_speakers(script, bible))
    issues.extend(
        explainer_service.check_facts(
            script, bible, source_material, min_chars=min_quote_chars
        )
    )
    issues.extend(explainer_service.check_length(script, target_duration_sec))
    return ConsistencyReport(stage="script", issues=issues)


# ── 剧本层检查 ────────────────────────────────────────


def _check_dialogue_speakers(scene: Scene, known_ids: set[str]) -> list[ConsistencyIssue]:
    issues = []
    for d in scene.dialogue:
        if not d.speaker:
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.WARNING,
                    scene_id=scene.id,
                    category="name",
                    message="有一条对白缺少说话人",
                    suggestion="补上 speaker（角色 id）",
                )
            )
        elif d.speaker not in known_ids:
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.ERROR,
                    scene_id=scene.id,
                    category="name",
                    message=f"对白说话人 '{d.speaker}' 不在角色列表中",
                    suggestion="检查是否拼写错误，或该角色需要加入故事圣经",
                )
            )
    return issues


def _check_names_in_text(scene: Scene, known_names: set[str]) -> list[ConsistencyIssue]:
    """扫描场景文本中出现的人名，检测是否引用了未定义的角色名。"""
    texts = [scene.heading, scene.summary]
    texts.extend(d.line for d in scene.dialogue)
    full_text = " ".join(texts)

    name_pattern = re.compile(
        r"(?:^|[\s，。！？、])((?:小)?[\u4e00-\u9fa5]{2,4})(?:说|道|问|答|笑|怒|哭|叫|喊)"
    )
    issues: list[ConsistencyIssue] = []
    seen: set[str] = set()
    for match in name_pattern.findall(full_text):
        if match in seen or match in known_names or match in _FALSE_POSITIVE_NAMES:
            continue
        if match.startswith(_NON_NAME_PREFIXES):
            continue
        seen.add(match)
        issues.append(
            ConsistencyIssue(
                severity=IssueSeverity.WARNING,
                scene_id=scene.id,
                category="name",
                message=f"检测到可能未定义的人名 '{match}'",
                suggestion=f"确认 '{match}' 是否为已知角色，或是否为误识别",
            )
        )
    return issues


def _check_dialogue_load(scene: Scene, *, strict: bool = False) -> list[ConsistencyIssue]:
    """台词总量 vs 场次时长：念不完的台词会在配音阶段把镜头拉长，成片远超目标时长。"""
    if scene.duration_sec <= 0 or not scene.dialogue:
        return []
    needed = sum(speech_seconds(d.line) for d in scene.dialogue)
    if needed <= scene.duration_sec:
        return []
    ratio = needed / scene.duration_sec
    blocking = strict and ratio > DIALOGUE_OVERLOAD_ERROR_RATIO
    return [
        ConsistencyIssue(
            severity=IssueSeverity.ERROR if blocking else IssueSeverity.WARNING,
            scene_id=scene.id,
            category="duration",
            message=(
                f"{scene.id} 有 {len(scene.dialogue)} 句台词，念完约 {needed:.0f} 秒，"
                f"但这场只有 {scene.duration_sec} 秒"
            ),
            suggestion="删掉不推动剧情的台词，或加长这场的时长（否则配音会把镜头拉长）",
        )
    ]


def _check_scene_content(scene: Scene, *, strict: bool = False) -> list[ConsistencyIssue]:
    if not scene.dialogue and not scene.summary:
        return [
            ConsistencyIssue(
                severity=IssueSeverity.ERROR if strict else IssueSeverity.WARNING,
                scene_id=scene.id,
                category="presence",
                message="场景内容为空",
                suggestion="补充场景内容或重新生成该场",
            )
        ]
    return []


def _number_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _check_numbers(scene: Scene, bible: StoryBible) -> list[ConsistencyIssue]:
    """提取场景中的数字，与 bible.facts 比对（忽略空格差异）。"""
    known_digits: set[str] = set()
    for fact in bible.facts:
        known_digits |= _number_tokens(fact.content)

    full_text = " ".join([scene.summary, *(d.line for d in scene.dialogue)])

    patterns = [
        (r"\d+(?:\.\d+)?\s*万", "金额/数量"),
        (r"\d+(?:\.\d+)?\s*亿", "金额/数量"),
        (r"\d{4}\s*年", "年份"),
        (r"第\s*\d+\s*[天号月]", "序数"),
    ]
    issues: list[ConsistencyIssue] = []
    seen: set[str] = set()
    for pattern, category in patterns:
        for match in re.finditer(pattern, full_text):
            raw = match.group(0)
            digits = _number_tokens(raw)
            key = re.sub(r"\s+", "", raw)
            if key in seen or (digits and digits <= known_digits):
                continue
            seen.add(key)
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.INFO,
                    scene_id=scene.id,
                    category="number",
                    message=f"场景中出现数字 '{key}' ({category})，未在关键事实中登记",
                    suggestion="如果是重要数字，请添加到故事圣经的 facts 中",
                )
            )
    return issues


def _check_timeline(script: Script) -> list[ConsistencyIssue]:
    """检查场景 heading 中的时间标注是否单调递进。"""
    issues = []
    day_pattern = re.compile(r"第\s*(\d+)\s*天")
    prev_day = 0
    for scene in script.scenes:
        match = day_pattern.search(scene.heading)
        if not match:
            continue
        day = int(match.group(1))
        if day < prev_day:
            issues.append(
                ConsistencyIssue(
                    severity=IssueSeverity.WARNING,
                    scene_id=scene.id,
                    category="timeline",
                    message=f"时间线可能倒退：第{day}天出现在第{prev_day}天之后",
                    suggestion="检查场景顺序是否正确",
                )
            )
        prev_day = max(prev_day, day)
    return issues


def _check_duration(script: Script, target_duration_sec: int) -> list[ConsistencyIssue]:
    if target_duration_sec <= 0 or not script.scenes:
        return []
    total = script.total_duration_sec
    ratio = deviation_ratio([s.duration_sec for s in script.scenes], target_duration_sec)
    if ratio <= DURATION_TOLERANCE:
        if script.duration_normalized:
            return [
                ConsistencyIssue(
                    severity=IssueSeverity.INFO,
                    scene_id="",
                    category="duration",
                    message=f"时长已自动归一化：{total}s（目标 {target_duration_sec}s）",
                    suggestion="如需精确控制，请手工调整各场时长",
                )
            ]
        return []
    return [
        ConsistencyIssue(
            severity=IssueSeverity.WARNING,
            scene_id="",
            category="duration",
            message=f"总时长 {total}s 与目标 {target_duration_sec}s 偏差 {ratio:.0%}",
            suggestion="重新生成剧本，或手工调整各场 duration_sec",
        )
    ]


def _check_world_state_open_threads(ws: WorldState) -> list[ConsistencyIssue]:
    if not ws.open_threads:
        return []
    return [
        ConsistencyIssue(
            severity=IssueSeverity.INFO,
            scene_id="",
            category="timeline",
            message=f"未闭合悬念 ({len(ws.open_threads)}): {'; '.join(ws.open_threads[:3])}",
            suggestion="确认这些悬念是否在后续场景中解决",
        )
    ]
