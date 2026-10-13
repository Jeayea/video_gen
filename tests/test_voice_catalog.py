"""配音选角：性别判定、不撞音色、复用区分、写回圣经。

真实事故的回归测试：`_MALE_HINTS` 里混进了「反派」这种**身份词**，
于是 22 岁的女反派被配成男声（`zh-CN-YunxiNeural`），而且 4 个角色只用到 2 个音色。
"""

from __future__ import annotations

from videogen.models.bible import Character, StoryBible, StyleGuide
from videogen.models.storyboard import Shot, Storyboard
from videogen.services import voice_catalog
from videogen.services.voice_catalog import (
    NARRATOR,
    cast_voice_map,
    catalog_rows,
    infer_gender,
    voice_gender,
)


def make_bible(*characters: Character, genre: str = "都市复仇") -> StoryBible:
    return StoryBible(characters=list(characters), style=StyleGuide(genre=genre))


def make_storyboard(speakers: list[str], *, narration: bool = False) -> Storyboard:
    shots = [
        Shot(
            id=f"scene_1_shot_{index + 1}",
            scene_id="scene_1",
            shot_no=index + 1,
            duration_sec=3,
            dialogue_text=f"台词 {index + 1}",
            dialogue_speaker=speaker,
        )
        for index, speaker in enumerate(speakers)
    ]
    if narration:
        shots.append(
            Shot(
                id="scene_1_shot_99",
                scene_id="scene_1",
                shot_no=99,
                duration_sec=3,
                narration="夜色渐深。",
            )
        )
    return Storyboard(shots=shots)


# ── 性别判定 ──────────────────────────────────────────


def test_role_word_is_not_a_gender_hint():
    """「反派」是身份不是性别 —— 这正是当初女反派被配成男声的根因。"""
    villain = Character(
        id="char_2",
        name="李薇儿",
        role="反派",
        age=22,
        description="当红小模特，心高气傲",
        visual_prompt="A beautiful, sharp-featured young woman with a haughty expression",
    )
    gender, reason = infer_gender(villain)
    assert gender == "female", reason


def test_gender_from_explicit_words_and_family_titles():
    man = Character(id="a", name="赵总", role="配角", description="中年男子")
    woman = Character(id="b", name="张姐", role="导师", description="资深女经纪人")
    unknown = Character(id="c", name="某人", role="配角", description="路人")
    assert infer_gender(man)[0] == "male"
    assert infer_gender(woman)[0] == "female"
    assert infer_gender(unknown)[0] is None


def test_male_hint_wins_only_with_real_evidence():
    """同时又「女」又「总裁」时不能被身份词带偏。"""
    female_ceo = Character(
        id="c",
        name="女总裁",
        role="反派",
        age=35,
        description="女强人，集团总裁",
        visual_prompt="a confident businesswoman",
    )
    assert infer_gender(female_ceo)[0] == "female"


# ── 选角 ─────────────────────────────────────────────


def test_every_character_gets_their_own_voice():
    """4 个角色（3 女 1 男）+ 旁白：性别正确、尽量不撞音色。"""
    bible = make_bible(
        Character(id="char_1", name="林悦", role="主角", age=20, visual_prompt="a young woman"),
        Character(id="char_2", name="李薇儿", role="反派", age=22, visual_prompt="a woman"),
        Character(id="char_3", name="张姐", role="导师", age=38, description="资深女经纪人"),
        Character(id="char_4", name="赵总", role="次要反派", age=45, description="中年男子"),
    )
    storyboard = make_storyboard(["char_1", "char_2", "char_3", "char_4"], narration=True)
    casting = cast_voice_map(storyboard, bible)

    assert voice_gender(casting.voice_map["char_1"]) == "female"
    assert voice_gender(casting.voice_map["char_2"]) == "female"  # 女反派必须是女声
    assert voice_gender(casting.voice_map["char_3"]) == "female"
    assert voice_gender(casting.voice_map["char_4"]) == "male"
    assert casting.voice_map[NARRATOR].endswith("YunjianNeural")

    # 通用普通话女声只有 2 个，第 3 个女角色必须靠念法区分（并留下说明）
    female_voices = [casting.voice_map[c] for c in ("char_1", "char_2", "char_3")]
    assert len(set(female_voices)) >= 2, female_voices
    assert casting.notes, "音色池用尽时必须留下可读的说明"


def test_same_voice_reuse_gets_distinct_delivery():
    bible = make_bible(
        Character(id="char_1", name="甲", role="主角", age=20, visual_prompt="a young woman"),
        Character(id="char_2", name="乙", role="配角", age=24, visual_prompt="a woman"),
        Character(id="char_3", name="丙", role="配角", age=30, visual_prompt="a woman"),
    )
    casting = cast_voice_map(make_storyboard(["char_1", "char_2", "char_3"]), bible)
    reused = [sp for sp in casting.voice_map if sp in casting.delivery]
    assert reused, "第三个女角色应该走「复用音色 + 念法区分」"
    for speaker in reused:
        delivery = casting.delivery[speaker]
        assert delivery.pitch or delivery.rate  # 必须有实际区分手段
        assert delivery.note


def test_existing_assignment_is_kept_when_gender_agrees():
    bible = make_bible(
        Character(id="char_1", name="林悦", role="主角", age=20, visual_prompt="a woman")
    )
    storyboard = make_storyboard(["char_1"])
    kept = cast_voice_map(storyboard, bible, existing={"char_1": "zh-CN-XiaoyiNeural"})
    assert kept.voice_map["char_1"] == "zh-CN-XiaoyiNeural"


def test_wrong_gendered_assignment_is_recast():
    """女角色身上挂着男声音色时必须重配，不能让它跟着 voice_map 一直续跑。"""
    bible = make_bible(
        Character(id="char_2", name="李薇儿", role="反派", age=22, visual_prompt="a woman")
    )
    casting = cast_voice_map(
        make_storyboard(["char_2"]), bible, existing={"char_2": "zh-CN-YunxiNeural"}
    )
    assert voice_gender(casting.voice_map["char_2"]) == "female"
    assert any("冲突" in note for note in casting.notes)


def test_age_mismatch_triggers_recast_when_better_voice_is_free():
    """真实样本：45 岁的杂志主编挂着「清朗少年感」的云希，必须换成沉稳音色。"""
    bible = make_bible(
        Character(id="char_4", name="赵总", role="次要反派", age=45, description="中年男子")
    )
    casting = cast_voice_map(
        make_storyboard(["char_4"]), bible, existing={"char_4": "zh-CN-YunxiNeural"}
    )
    assert casting.voice_map["char_4"] != "zh-CN-YunxiNeural"
    assert casting.voice_map["char_4"].endswith("YunyangNeural")
    assert any("年龄不符" in note for note in casting.notes)


def test_age_mismatch_keeps_voice_when_pool_is_empty():
    """音色池空了就保留原音色（靠念法区分），不要为了「更年轻」换一个更离谱的。"""
    bible = make_bible(
        Character(id="char_1", name="甲", role="主角", age=20, visual_prompt="a woman"),
        Character(id="char_2", name="乙", role="配角", age=24, visual_prompt="a woman"),
        Character(id="char_3", name="张姐", role="导师", age=38, description="资深女经纪人"),
    )
    casting = cast_voice_map(
        make_storyboard(["char_1", "char_2", "char_3"]),
        bible,
        existing={"char_1": "zh-CN-XiaoxiaoNeural", "char_2": "zh-CN-XiaoyiNeural"},
    )
    assert casting.voice_map["char_3"] == "zh-CN-XiaoxiaoNeural"
    assert "char_3" in casting.delivery  # 靠念法区分


def test_bible_voice_id_is_pinned_and_wins():
    """人工在角色卡片上写死的音色优先（但仍要过性别校验）。"""
    bible = make_bible(
        Character(
            id="char_1",
            name="林悦",
            role="主角",
            age=20,
            visual_prompt="a woman",
            voice_id="zh-CN-shaanxi-XiaoniNeural",
        )
    )
    casting = cast_voice_map(make_storyboard(["char_1"]), bible)
    assert casting.voice_map["char_1"] == "zh-CN-shaanxi-XiaoniNeural"


def test_accent_voices_are_avoided_by_default(settings):
    """默认不给角色上方言音色（同一部戏混口音很出戏）；开了才允许。"""
    bible = make_bible(
        Character(id="char_1", name="甲", role="主角", age=20, visual_prompt="a woman"),
        Character(id="char_2", name="乙", role="配角", age=22, visual_prompt="a woman"),
        Character(id="char_3", name="丙", role="配角", age=26, visual_prompt="a woman"),
    )
    storyboard = make_storyboard(["char_1", "char_2", "char_3"])
    default = cast_voice_map(storyboard, bible, settings)
    assert all(
        not voice_catalog.CATALOG_BY_ID[voice].dialect for voice in default.voice_map.values()
    )

    settings.tts_allow_accent_voices = True
    allowed = cast_voice_map(storyboard, bible, settings)
    # 放开后能用上「同性别但非通用普通话」的备用音色，因此第三个角色的选角会变
    assert allowed.voice_map["char_3"] != default.voice_map["char_3"]


def test_casting_is_deterministic():
    bible = make_bible(
        Character(id="char_1", name="甲", role="主角", age=20, visual_prompt="a woman"),
        Character(id="char_2", name="乙", role="反派", age=22, description="年轻男子"),
    )
    storyboard = make_storyboard(["char_1", "char_2"])
    first = cast_voice_map(storyboard, bible).voice_map
    second = cast_voice_map(storyboard, bible).voice_map
    assert first == second


def test_unknown_speaker_falls_back_to_default_voice(settings):
    """不在圣经里的说话人（脏数据）不能让它崩，退回默认音色。"""
    casting = cast_voice_map(make_storyboard(["char_x"]), make_bible())
    assert casting.voice_map["char_x"] == settings.tts_voice


def test_catalog_rows_expose_metadata_for_ui():
    rows = catalog_rows()
    assert len(rows) >= 8
    row = next(r for r in rows if r["id"] == "zh-CN-XiaoxiaoNeural")
    assert row["gender"] == "female"
    assert row["age"] and row["timbre"] and row["label"]
    zh = [r for r in rows if r["locale"].startswith("zh")]
    en = [r for r in rows if r["locale"].startswith("en")]
    assert len(zh) + len(en) == len(rows) and len(zh) >= 8 and len(en) >= 8


# ── 年龄未知 / 圣经里声明的性别（NOVEL 实测：父亲被配成少年音）──────────────


def test_declared_gender_wins_over_text_guess():
    character = Character(id="char_1", name="小林", gender="女", description="公司代表")
    assert character.gender == "female"
    assert infer_gender(character) == ("female", "圣经 gender 字段")
    assert Character(id="char_2", name="某人", gender="unknown").gender == ""


def test_unknown_age_adult_never_falls_back_to_child_voice():
    """《变形记》实测：父亲没填年龄，男声里格里高占了云希，父亲被分到 8-16 岁的云夏。"""
    bible = make_bible(
        Character(id="char_1", name="格里高", role="主角", gender="male", description="推销员"),
        Character(id="char_3", name="父亲", role="配角", gender="male",
                  description="格里高的父亲"),
    )
    casting = cast_voice_map(make_storyboard(["char_1", "char_3"]), bible)
    assert not casting.voice_map["char_3"].endswith("YunxiaNeural")
    assert voice_gender(casting.voice_map["char_3"]) == "male"


def test_young_character_without_age_can_still_get_child_voice():
    bible = make_bible(
        Character(id="char_1", name="男主", role="主角", gender="male", age=25),
        Character(id="char_2", name="小宝", role="配角", gender="male",
                  description="邻居家的小男孩"),
    )
    casting = cast_voice_map(make_storyboard(["char_1", "char_2"]), bible)
    assert casting.voice_map["char_2"].endswith("YunxiaNeural")
