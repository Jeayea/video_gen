"""画面里本来就有的中文（黑板 / 招牌）进图片 prompt；台词、人名照样剔除。"""

from __future__ import annotations

from videogen.models.bible import Setting
from videogen.services import asset_service
from videogen.services.asset_service import english_only, signage_text, text_rule


def test_signage_is_kept_but_dialogue_and_names_are_stripped():
    text = (
        '林悦 sits by the blackboard, which reads "距离高考还有305天"; a red sign reads '
        '“新时代网吧”. She says this exact Mandarin line: "你听到没有？"'
    )
    assert signage_text(text) == ["距离高考还有305天", "新时代网吧"]
    kept = english_only(text, keep_signage=True)
    assert '"距离高考还有305天"' in kept and '"新时代网吧"' in kept
    assert "林悦" not in kept and "你听到没有" not in kept  # 名字 / 台词不进画面
    assert "距离" not in english_only(text)  # 默认行为不变：中文全剔


def test_english_quotes_are_not_signage():
    assert signage_text('a sign reads "OPEN"') == []
    assert text_rule('a sign reads "OPEN"', default="no text") == "no text"


def test_setting_prompt_allows_only_the_quoted_signage():
    setting = Setting(
        id="loc_3", name="网吧",
        visual_prompt='An internet cafe, the red sign reads "新时代网吧" in white characters',
    )
    prompt = asset_service.build_setting_prompt(setting)
    assert '"新时代网吧"' in prompt
    assert "The only visible writing is" in prompt
    assert "no signs with words" not in prompt

    plain = Setting(id="loc_1", name="教室", visual_prompt="A quiet classroom")
    assert "no signs with words" in asset_service.build_setting_prompt(plain)
