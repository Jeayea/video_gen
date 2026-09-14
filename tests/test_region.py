"""名字 ↔ 国家 ↔ 脸 的对齐测试。

要保证的行为：
1. 中文名 → 中国脸、日文名（假名/日本姓氏）→ 日本脸、韩文名 → 韩国脸、英文名 → 欧美脸；
2. 中日韩**共用**的汉字姓（金/李/林…）单独判断不了国籍 → 跟随整个剧组；
3. 整个剧组按多数派 + 主角权重投票，一部片子一张脸谱；
4. 角色自己写了 region、或配置里强制了国家，优先级最高；
5. 名字**绝不进 prompt**，但人种锚点要进 —— 且角色已经写明人种时不叠加。
"""

from __future__ import annotations

from videogen.config import get_settings
from videogen.models.bible import Character, StoryBible
from videogen.models.storyboard import Shot
from videogen.services import character_image_service, video_service
from videogen.utils import region

# ── 名字 → 国家 ───────────────────────────────────────


def test_kana_and_hangul_are_decisive():
    assert region.guess_region("さくら").region == "jp"
    assert region.guess_region("さくら").confidence >= 0.9
    assert region.guess_region("김지은").region == "kr"
    assert region.guess_region("김지은").confidence >= 0.9
    # 片假名写的韩国姓（キム・ジウン）不能被当成日本人
    assert region.guess_region("キム・ジウン").region == "kr"


def test_japanese_kanji_surname_beats_chinese():
    for name in ("田中由纪", "佐藤健", "铃木美咲", "高桥一郎", "渡边淳"):
        guess = region.guess_region(name)
        assert guess.region == "jp", name
        assert guess.confident


def test_chinese_surname_is_chinese():
    for name in ("王小明", "赵总", "欧阳锋", "司马昭"):
        guess = region.guess_region(name)
        assert guess.region == "cn", name


def test_korean_specific_hanzi_surname():
    assert region.guess_region("朴槿惠").region == "kr"


def test_shared_surnames_follow_the_project():
    """金/李/林 在中日韩都常见 —— 单独判断不了，所以只给低置信度。"""
    for name in ("金敏俊", "李在镕", "林悦"):
        guess = region.guess_region(name)
        assert guess.region == "cn"
        assert not guess.confident, name


def test_latin_names():
    assert region.guess_region("Tanaka Yuki").region == "jp"
    assert region.guess_region("Kim Min-jun").region == "kr"
    assert region.guess_region("Wang Wei").region == "cn"
    assert region.guess_region("Emily Carter").region == "west"
    assert region.guess_region("John Smith").confident


def test_text_hint_wins_over_name():
    """「日本人」这种描述比名字更硬（名字可能看不出国籍）。"""
    guess = region.guess_region("林悦", text="她是日本人，在东京长大")
    assert guess.region == "jp"
    assert guess.source.startswith("hint")


def test_empty_name_is_other():
    assert region.guess_region("").region == "other"
    assert not region.guess_region("").confident


def test_normalize_and_describe():
    assert region.normalize_region("中国") == "cn"
    assert region.normalize_region("Japanese") == "jp"
    assert region.normalize_region("KR") == "kr"
    assert region.normalize_region("欧美") == "west"
    assert region.normalize_region("火星") == ""
    assert region.describe_region("jp") == "日本"


# ── 剧组投票 ──────────────────────────────────────────


def _bible(*characters: Character) -> StoryBible:
    return StoryBible(characters=list(characters))


def test_project_region_majority():
    bible = _bible(
        Character(id="c1", name="林悦", role="主角"),
        Character(id="c2", name="李薇儿", role="配角"),
        Character(id="c3", name="张姐", role="配角"),
        Character(id="c4", name="田中由纪", role="配角"),
    )
    assert region.project_region(bible).region == "cn"


def test_project_region_protagonist_has_more_weight():
    bible = _bible(
        Character(id="c1", name="田中由纪", role="主角"),
        Character(id="c2", name="林悦", role="配角"),
        Character(id="c3", name="李薇儿", role="配角"),
    )
    # 主角 2.0×0.8=1.6（日）> 两个配角 2×0.4=0.8（中）→ 日本
    assert region.project_region(bible).region == "jp"


def test_project_region_override_and_default():
    bible = _bible(Character(id="c1", name="林悦", role="主角"))
    assert region.project_region(bible, override="kr").region == "kr"
    assert region.project_region(bible, override="日本").region == "jp"
    assert region.project_region(_bible(), default="kr").region == "kr"
    assert region.project_region(_bible()).region == "cn"


def test_project_region_respects_character_field():
    bible = _bible(Character(id="c1", name="林悦", role="主角", region="jp"))
    assert region.project_region(bible).region == "jp"


# ── 单角色定调 ────────────────────────────────────────


def test_resolve_region_priority():
    bible = _bible(
        Character(id="c1", name="林悦", role="主角", region="kr"),
        Character(id="c2", name="田中由纪", role="配角"),
        Character(id="c3", name="金敏俊", role="配角"),
    )
    # 1) 角色自己的 region 最硬
    assert region.resolve_region(bible.characters[0], bible).region == "kr"
    # 2) 名字够自信就用名字（日文姓压过剧组多数）
    assert region.resolve_region(bible.characters[1], bible).region == "jp"
    # 3) 名字分不出来 → 跟剧组
    guess = region.resolve_region(bible.characters[2], bible)
    assert guess.region == "kr"
    assert guess.source.startswith("follow-cast")
    # 4) 配置强制 / 关掉
    assert region.resolve_region(bible.characters[1], bible, override="cn").region == "cn"
    assert region.resolve_region(bible.characters[1], bible, auto=False).region == ""


def test_anchor_and_mentioned():
    assert "average everyday Japanese face" in region.region_anchor("jp")
    assert region.region_anchor("nope") == ""
    assert region.region_mentioned("A Chinese woman in her 20s")
    assert region.region_mentioned("East Asian features")
    assert not region.region_mentioned("A young woman in a trench coat")


# ── 进 prompt ─────────────────────────────────────────


def _cast() -> StoryBible:
    return _bible(
        Character(
            id="char_1",
            name="林悦",
            role="主角",
            age=26,
            visual_prompt="A young woman in a beige trench coat",
        ),
        Character(
            id="char_2",
            name="田中由纪",
            role="配角",
            age=30,
            visual_prompt="A man in a dark suit",
        ),
    )


def test_portrait_prompt_gets_region_anchor():
    bible = _cast()
    prompt = character_image_service.build_portrait_prompt(bible.characters[0], bible)
    assert "average everyday Chinese face" in prompt
    # 日文名的角色按日本来，不被剧组多数带偏
    other = character_image_service.build_portrait_prompt(bible.characters[1], bible)
    assert "average everyday Japanese face" in other


def test_portrait_prompt_never_contains_chinese_even_with_region():
    import re

    bible = _cast()
    for character in bible.characters:
        prompt = character_image_service.build_portrait_prompt(character, bible)
        assert not re.search(r"[\u4e00-\u9fff]", prompt), prompt
        assert character.name not in prompt


def test_portrait_prompt_skips_anchor_when_ethnicity_already_given():
    bible = _cast()
    bible.characters[0].visual_prompt = "A young Chinese woman in a trench coat"
    prompt = character_image_service.build_portrait_prompt(bible.characters[0], bible)
    assert "average everyday Chinese face" not in prompt
    # 手动 suffix 里写明人种时也不叠加（两个都写会自相矛盾）
    prompt = character_image_service.build_portrait_prompt(
        bible.characters[0],
        bible,
        extra="East Asian cast, Chinese television drama",
    )
    assert "average everyday Chinese face" not in prompt


def test_portrait_prompt_region_can_be_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "image_region_auto", False)
    bible = _cast()
    prompt = character_image_service.build_portrait_prompt(
        bible.characters[0], bible, settings=settings
    )
    assert "average everyday Chinese face" not in prompt


def test_portrait_prompt_region_override_wins(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "image_region_override", "kr")
    bible = _cast()
    prompt = character_image_service.build_portrait_prompt(
        bible.characters[0], bible, settings=settings
    )
    assert "average everyday Korean face" in prompt


def test_video_anchor_carries_cast_look_without_names():
    bible = _cast()
    shot = Shot(
        id="s1", scene_id="sc", shot_no=1, duration_sec=3, character_in_frame=["char_1"]
    )
    anchor = video_service.appearance_anchor(shot, bible, "medium shot of a woman")
    assert "cast look" in anchor
    assert "average everyday Chinese face" in anchor
    # 中文名绝不能进 prompt（会被模型画成画面上的文字）
    assert "林悦" not in anchor
    assert "char_1" not in anchor


def test_video_anchor_skips_cast_look_when_already_mentioned():
    bible = _cast()
    shot = Shot(
        id="s1", scene_id="sc", shot_no=1, duration_sec=3, character_in_frame=["char_1"]
    )
    anchor = video_service.appearance_anchor(
        shot, bible, "medium shot of a Chinese woman"
    )
    assert "cast look" not in anchor


# ── 圣经模型 ──────────────────────────────────────────


def test_character_region_is_normalized():
    assert Character(id="c", name="x", region="中国").region == "cn"
    assert Character(id="c", name="x", region="Japanese").region == "jp"
    assert Character(id="c", name="x", region="火星").region == ""
    assert Character(id="c", name="x").region == ""


def test_bible_prompt_requires_alignment():
    from videogen.services.bible_service import BIBLE_GEN_SYSTEM

    assert '"region": "cn"' in BIBLE_GEN_SYSTEM
    assert "名字、地区、外貌三者必须对齐" in BIBLE_GEN_SYSTEM
