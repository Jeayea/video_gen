from __future__ import annotations

import pytest
from pydantic import ValidationError

from videogen.models.bible import Character
from videogen.models.script import Scene
from videogen.models.storyboard import Shot
from videogen.utils.coerce import coerce_int, coerce_optional_int, ensure_str_list
from videogen.utils.duration import apply_duration_budget, deviation_ratio, scale_durations


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, 7),
        ("10", 10),
        (7.5, 8),
        (7.4, 7),
        ("约10秒", 10),
        ("第3天", 3),
        ("12 秒", 12),
        (None, 5),
        ("没有数字", 5),
        (True, 5),
        ({}, 5),
        (float("nan"), 5),
    ],
)
def test_coerce_int_is_total(value: object, expected: int) -> None:
    assert coerce_int(value, 5) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("25岁", 25), ("约30", 30), (30, 30), ("", None), (None, None), ("不知道", None)],
)
def test_coerce_optional_int(value: object, expected: int | None) -> None:
    assert coerce_optional_int(value) == expected


def test_ensure_str_list_normalizes() -> None:
    assert ensure_str_list("a") == ["a"]
    assert ensure_str_list(None) == []
    assert ensure_str_list(["a", None, "", 2]) == ["a", "2"]
    assert ensure_str_list({"a": 1}) == []


def test_scene_tolerates_dirty_llm_values() -> None:
    """P0-3 回归：脏 duration 不该让整条流水线崩掉。"""
    assert Scene(id="s", duration_sec=7.5).duration_sec == 8
    assert Scene(id="s", duration_sec="约10秒").duration_sec == 10
    # 明显不合理的值会被夹到下限，而不是抛异常
    assert Scene(id="s", duration_sec=0).duration_sec >= 1
    assert Scene(id="s", duration_sec="abc").duration_sec == 10


def test_scene_dialogue_none_becomes_empty() -> None:
    assert Scene(id="s", dialogue=None).dialogue == []


def test_shot_tolerates_dirty_values() -> None:
    shot = Shot(
        id="x",
        scene_id="scene_1",
        shot_no=1,
        duration_sec="约4秒",
        character_in_frame="char_1",
        narration="",
        dialogue_text=None,
    )
    assert shot.duration_sec == 4
    assert shot.character_in_frame == ["char_1"]
    assert shot.narration is None


def test_character_age_and_personality_tolerance() -> None:
    character = Character(id="c", name="n", age="24岁", personality="坚韧")
    assert character.age == 24
    assert character.personality == ["坚韧"]


def test_character_missing_required_id_still_errors() -> None:
    with pytest.raises(ValidationError):
        Character(name="n")  # type: ignore[call-arg]


def test_scale_durations_hits_target_exactly() -> None:
    assert sum(scale_durations([45, 45, 45, 45, 45], 45, 3)) == 45
    assert sum(scale_durations([4, 4, 4], 10, 1)) == 10
    assert sum(scale_durations([100, 1, 1], 99, 1)) == 99


def test_scale_durations_is_stable() -> None:
    values = [7, 11, 13, 19]
    assert scale_durations(values, sum(values), 1) == values


def test_deviation_ratio() -> None:
    assert deviation_ratio([45, 45], 45) == 1.0
    assert deviation_ratio([45], 45) == 0.0
    assert deviation_ratio([10], 0) == 0.0


def test_apply_duration_budget() -> None:
    assert apply_duration_budget(45, 5, 3) == 9
    assert apply_duration_budget(45, 0, 3) == 45
    assert apply_duration_budget(10, 10, 3) == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1分30秒", 90),
        ("1.5分钟", 90),
        ("1:30", 90),
        ("2min", 120),
        ("约10秒", 10),
        ("10-15秒", 10),
    ],
)
def test_duration_fields_understand_units(raw, expected):
    """回归：「1分30秒」以前只取第一个数，被当成 1 秒。"""
    from videogen.models.storyboard import Shot

    assert Shot(id="s", scene_id="c", shot_no=1, duration_sec=raw).duration_sec == expected


def test_nan_and_infinity_do_not_crash():
    from videogen.utils.coerce import coerce_optional_int

    assert coerce_optional_int(float("nan")) is None
    assert coerce_optional_int(float("inf")) is None
