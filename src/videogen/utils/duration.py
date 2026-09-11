from __future__ import annotations

import math

__all__ = [
    "apply_duration_budget",
    "deviation_ratio",
    "fit_durations",
    "plan_shot_count",
    "scale_durations",
]


def scale_durations(values: list[int], target: int, minimum: int = 1) -> list[int]:
    """按比例缩放时长，使总和精确等于 target。

    用于把 LLM 输出的时长强行拉回目标时长（DESIGN §4.3 的「时长对齐」）。
    取整误差用逐项 ±1 的方式抹平，保证 sum(result) == target（除非 target 小到
    无法给每项分配 minimum）。
    """
    if not values or target <= 0:
        return list(values)

    safe = [max(minimum, int(v)) for v in values]
    total = sum(safe)
    if total == target:
        return safe

    scaled = [v * target / total for v in safe]
    out = [max(minimum, round(s)) for s in scaled]

    diff = target - sum(out)
    guard = 0
    while diff != 0 and guard < len(out) * 8:
        idx = guard % len(out)
        if diff > 0:
            out[idx] += 1
            diff -= 1
        elif out[idx] > minimum:
            out[idx] -= 1
            diff += 1
        guard += 1
    return out


def deviation_ratio(values: list[int], target: int) -> float:
    """返回 |sum(values) - target| / target；target<=0 时返回 0。"""
    if target <= 0:
        return 0.0
    return abs(sum(values) - target) / target


def apply_duration_budget(
    project_target_sec: int,
    scene_count: int,
    minimum_per_scene: int = 3,
) -> int:
    """把项目总时长预算均分到每一场，给出单场建议时长。"""
    if scene_count <= 0:
        return project_target_sec
    return max(minimum_per_scene, round(project_target_sec / scene_count))


def fit_durations(
    count: int, total: int, *, min_sec: int = 1, max_sec: int = 0
) -> list[int]:
    """把 `total` 秒分配给 `count` 个镜头，尽量让每个都落在 [min_sec, max_sec] 内。

    真实视频模型按最短档位计费（H3 是 5 秒），所以：
    - 总时长不够 `count × min_sec` 时，**宁可让本场变长**也不做「买 5 秒只剪 3 秒」的浪费
    - 总时长超过 `count × max_sec` 时按 max 封顶（调用方应先把镜头数算对）
    取整误差用「前面几个多 1 秒」的方式抹平。
    """
    count = max(1, int(count))
    min_sec = max(1, int(min_sec))
    if max_sec and max_sec < min_sec:
        max_sec = min_sec

    target = max(int(total), count * min_sec)
    if max_sec > 0:
        target = min(target, count * max_sec)

    base = target // count
    rest = target - base * count
    out = [base + (1 if index < rest else 0) for index in range(count)]
    return [
        min(max(value, min_sec), max_sec) if max_sec > 0 else max(value, min_sec)
        for value in out
    ]


def plan_shot_count(
    scene_sec: int,
    *,
    min_sec: float,
    max_sec: float = 0.0,
    target_sec: float = 0.0,
    hard_max: int = 5,
) -> int:
    """一场戏该拆几个镜头：在「每个镜头 ≥ min、≤ max」的前提下尽量贴近目标时长。

    `min_sec <= 1` 时返回 0 = 不约束（保持 LLM 自己拆 2-5 镜的原有行为）。
    """
    if min_sec <= 1:
        return 0
    scene_sec = max(1, int(scene_sec))
    low = max(1, int(min_sec))
    high = int(max_sec) if max_sec and max_sec >= low else 0
    target = float(target_sec) if target_sec and target_sec > 0 else low * 1.6
    target = max(float(low), target)

    feasible_max = max(1, scene_sec // low)  # 每个都 ≥ min 时最多几个
    feasible_min = max(1, math.ceil(scene_sec / high)) if high else 1
    want = max(1, round(scene_sec / target))
    return max(1, min(max(want, feasible_min), feasible_max, max(1, hard_max)))
