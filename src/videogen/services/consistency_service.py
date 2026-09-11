from __future__ import annotations

import re

from videogen.models.bible import StoryBible
from videogen.models.consistency import ConsistencyIssue, ConsistencyReport, IssueSeverity
from videogen.models.script import Scene, Script
from videogen.models.storyboard import Storyboard
from videogen.models.world_state import WorldState
from videogen.utils.duration import deviation_ratio

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


def validate_script(
    script: Script,
    bible: StoryBible,
    world_state: WorldState | None = None,
    target_duration_sec: int = 0,
) -> ConsistencyReport:
    """检查剧本与故事圣经之间的一致性。"""
    issues: list[ConsistencyIssue] = []
    known_names = bible.all_names()
    known_ids = {c.id for c in bible.characters}

    for scene in script.scenes:
        issues.extend(_check_dialogue_speakers(scene, known_ids))
        issues.extend(_check_names_in_text(scene, known_names))
        issues.extend(_check_numbers(scene, bible))
        issues.extend(_check_scene_content(scene))

    issues.extend(_check_timeline(script))
    issues.extend(_check_duration(script, target_duration_sec))
    if world_state:
        issues.extend(_check_world_state_open_threads(world_state))

    return ConsistencyReport(stage="script", issues=issues)


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
            elif shot.dialogue_speaker not in shot.character_in_frame:
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


def merge_storyboard_issues(
    report: ConsistencyReport, extra: list[ConsistencyIssue]
) -> ConsistencyReport:
    return ConsistencyReport(stage="script+storyboard", issues=[*report.issues, *extra])


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


def _check_scene_content(scene: Scene) -> list[ConsistencyIssue]:
    if not scene.dialogue and not scene.summary:
        return [
            ConsistencyIssue(
                severity=IssueSeverity.WARNING,
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
