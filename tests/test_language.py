"""成片语言：英文项目的提示词覆盖、视频 prompt 语言、语速估算、英文音色选角。"""

from __future__ import annotations

import pytest

from tests.fakes import FakeLLM
from videogen.config import get_settings
from videogen.models.bible import Character, StoryBible
from videogen.models.project import Project
from videogen.models.storyboard import Shot, Storyboard
from videogen.orchestrator.pipeline import Pipeline
from videogen.providers.llm.base import LLMMessage
from videogen.services import video_service, voice_catalog
from videogen.utils import language
from videogen.utils.duration import speech_seconds
from videogen.utils.text import estimate_speech_sec


def test_normalize_language_aliases():
    assert language.normalize_language("EN") == "en"
    assert language.normalize_language("English") == "en"
    assert language.normalize_language("中文") == "zh"
    assert language.normalize_language("") == "zh"
    assert language.normalize_language("klingon") == "zh"


def test_spoken_language_follows_the_line_itself():
    assert language.spoken_language_name("薇儿，为什么？") == "Mandarin"
    assert language.spoken_language_name("Why, Vivian?") == "English"
    assert language.spoken_language_name("どうして") == "Japanese"


def test_speech_rate_counts_english_words_not_letters():
    """英文按字母数当「字」算会把时长估长约 3 倍（4.5 字/秒 vs 2.5 词/秒）。"""
    line = "I never asked you to leave."  # 6 个词
    assert speech_seconds(line) == pytest.approx(6 / 2.5)
    assert speech_seconds("温两碗酒") == pytest.approx(4 / 4.5)
    assert estimate_speech_sec(line) == pytest.approx(6 / 2.5, abs=0.01)
    # 中英混排各算各的
    assert speech_seconds("好的 OK") == pytest.approx(2 / 4.5 + 1 / 2.5)


def test_video_prompt_says_english_for_english_lines():
    shot = Shot(
        id="scene_1_shot_1",
        scene_id="scene_1",
        shot_no=1,
        duration_sec=4,
        dialogue_text="Why did you do it?",
        dialogue_speaker="char_1",
    )
    spoken = video_service.dialogue_directive(shot)
    assert "exact English line" in spoken and "Mandarin" not in spoken
    shot_zh = shot.model_copy(update={"dialogue_text": "为什么？"})
    assert "exact Mandarin line" in video_service.dialogue_directive(shot_zh)


def test_narration_marker_matches_english_narration(monkeypatch):
    """原声旁白直通的判据是 prompt 里的标记 —— 英文旁白也要认得出来。"""
    monkeypatch.setattr(get_settings(), "video_prompt_narration", True)
    shot = Shot(
        id="scene_1_shot_1",
        scene_id="scene_1",
        shot_no=1,
        duration_sec=5,
        narration="The bamboo is cut before the rains.",
    )
    text = video_service.narration_directive(shot)
    assert "exact English narration" in text
    assert video_service._narration_marker(shot.narration) in text


def test_english_camera_text_passes_through():
    assert video_service.camera_directive("close-up, slow dolly in") == "close-up, slow dolly in"
    assert video_service.camera_directive("特写 · 推") == "close-up, slow dolly in"


@pytest.mark.asyncio
async def test_language_llm_appends_override_and_keeps_usage():
    inner = FakeLLM()
    llm = language.with_language(inner, "en")
    assert language.with_language(inner, "zh") is inner
    await llm.chat([LLMMessage(role="system", content="用一句中文概括"), LLMMessage(
        role="user", content="x")])
    system = inner.calls[-1][0].content
    assert system.startswith("用一句中文概括") and "LANGUAGE OVERRIDE" in system
    llm.record_usage(input_tokens=5, output_tokens=2)
    assert inner.take_usage().input_tokens >= 5


@pytest.mark.asyncio
async def test_english_project_prompts_carry_the_override(store):
    llm = FakeLLM()
    pipeline = Pipeline(store, llm_factory=lambda: llm)
    project = Project(input_text="A laid-off engineer builds an AI startup", language="en")
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    assert llm.calls and all("LANGUAGE OVERRIDE" in call[0].content for call in llm.calls)

    zh_llm = FakeLLM()
    zh = Project(input_text="一个被裁员的程序员靠 AI 逆袭创业")
    store.save_project(zh)
    await Pipeline(store, llm_factory=lambda: zh_llm).generate_bible(zh.id)
    assert not any("LANGUAGE OVERRIDE" in call[0].content for call in zh_llm.calls)


def test_default_language_comes_from_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "project_language", "en")
    assert settings.language_for(Project(input_text="x")) == "en"
    assert settings.language_for(Project(input_text="x", language="zh")) == "zh"


def _board(speakers: list[str]) -> Storyboard:
    shots = [
        Shot(
            id=f"scene_1_shot_{i + 1}",
            scene_id="scene_1",
            shot_no=i + 1,
            duration_sec=3,
            dialogue_text=f"Line {i + 1}",
            dialogue_speaker=sp,
        )
        for i, sp in enumerate(speakers)
    ]
    shots.append(
        Shot(id="scene_1_shot_9", scene_id="scene_1", shot_no=9, duration_sec=3,
             narration="Night falls.")
    )
    return Storyboard(shots=shots)


def test_english_casting_uses_en_us_voices():
    bible = StoryBible(
        characters=[
            Character(id="char_1", name="Emily", role="女主角", age=26, gender="female"),
            Character(id="char_2", name="Mark", role="反派", age=40, gender="male"),
            Character(id="char_3", name="Grace", role="配角", age=60, gender="female"),
        ]
    )
    casting = voice_catalog.cast_voice_map(
        _board(["char_1", "char_2", "char_3"]), bible, language="en", target="edge"
    )
    assert all(v.startswith("en-US-") for v in casting.voice_map.values()), casting.voice_map
    assert casting.voice_map[voice_catalog.NARRATOR] == "en-US-GuyNeural"
    assert len({casting.voice_map[c] for c in ("char_1", "char_2", "char_3")}) == 3

    # 中文片默认仍只用中文音色
    zh = voice_catalog.cast_voice_map(_board(["char_1"]), bible, target="edge")
    assert all(v.startswith("zh-") for v in zh.voice_map.values())


def test_english_casting_replaces_a_leftover_chinese_voice():
    bible = StoryBible(characters=[Character(id="char_1", name="Emily", gender="female")])
    casting = voice_catalog.cast_voice_map(
        _board(["char_1"]),
        bible,
        existing={"char_1": "zh-CN-XiaoxiaoNeural"},
        language="en",
        target="edge",
    )
    assert casting.voice_map["char_1"].startswith("en-US-")
    assert any("不是本片语言" in note for note in casting.notes)

