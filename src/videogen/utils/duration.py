from __future__ import annotations

__all__ = ["apply_duration_budget", "deviation_ratio", "scale_durations"]


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
