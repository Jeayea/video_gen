"""知识讲解型科普（`content_type=explainer`，docs/EXPLAINER_DESIGN.md）。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import wait_for_job
from tests.factories import small_project
from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen import main
from videogen.models.audio import AudioClip, AudioManifest
from videogen.models.bible import Character, Fact, StoryBible
from videogen.models.compose import ComposeSegment, SubtitleMode
from videogen.models.consistency import IssueSeverity
from videogen.models.explainer import KnowledgeFact, KnowledgeResponse, NewFact
from videogen.models.project import ContentType, Project
from videogen.models.script import NARRATOR, DialogueLine, Scene, Script
from videogen.models.storyboard import Shot
from videogen.models.video import ClipStatus
from videogen.orchestrator.pipeline import Pipeline, PipelineError
from videogen.providers.tts.mock import MockTTSProvider
from videogen.providers.video.mock import MockVideoProvider
from videogen.services import (
    audio_service,
    compose_service,
    consistency_service,
    explainer_service,
    subtitle_service,
    video_service,
    voice_catalog,
)
from videogen.utils.ffmpeg import describe_media, ffmpeg_available
from videogen.utils.file_store import FileStore

requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="需要 ffmpeg")

NARRATION = "天之所以是蓝的，是因为空气把蓝光散射得最多。"


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "projects")
    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: FakeVideoProvider(),
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "pipeline", pipeline)
    with TestClient(main.app) as test_client:
        yield test_client


def _explainer(**kwargs) -> Project:
    return Project(title="天为什么是蓝的", content_type=ContentType.EXPLAINER, **kwargs)


# ── 数据模型 ──────────────────────────────────────────


def test_old_data_reads_as_drama_with_empty_fact_fields():
    project = Project.model_validate({"title": "老项目"})
    assert project.content_type == ContentType.DRAMA
    fact = Fact.model_validate({"id": "fact_1", "content": "主角 28 岁"})
    assert (fact.source, fact.status) == ("", "unverified")
    line = DialogueLine.model_validate({"speaker": "char_1", "line": "好。"})
    assert line.fact_refs == []
    assert Shot(id="s1", scene_id="scene_1", shot_no=1).fact_refs == []


@pytest.mark.parametrize("raw", ["Confirmed", "已确认", None, "ok"])
def test_unknown_fact_status_falls_back_to_unverified(raw):
    # 只有认得的值才算数：笔误绝不能被当成「已确认」
    status = Fact(id="fact_1", content="x", status=raw).status
    assert status == ("confirmed" if raw == "Confirmed" else "unverified")


def test_narrator_constant_is_shared():
    assert audio_service.NARRATOR == voice_catalog.NARRATOR == NARRATOR == "narrator"


# ── 建项目 ────────────────────────────────────────────


def test_create_explainer_project_fills_defaults(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "bgm_gain_db", -20.0)
    resp = client.post(
        "/api/projects",
        json={
            "title": "天为什么是蓝的",
            "input_text": "为什么天是蓝的",
            "content_type": "explainer",
            "audience": " 初中生 ",
            "source_material": "瑞利散射……",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content_type"] == "explainer"
    assert body["audience"] == "初中生"
    assert body["source_material"] == "瑞利散射……"
    assert body["ai_label"] is True
    assert body["subtitle_style"] == "box"
    assert body["music_mood"] == "neutral"
    assert body["music_gain_db"] == -26.0


def test_create_drama_project_is_unchanged(client):
    body = client.post("/api/projects", json={"title": "短剧", "input_text": "复仇"}).json()
    assert body["content_type"] == "drama"
    assert (body["subtitle_style"], body["music_mood"], body["music_gain_db"]) == ("", "", None)


def test_explainer_rejects_novel_mode(client):
    resp = client.post(
        "/api/projects",
        json={"input_text": "第一章 …", "content_type": "explainer", "input_mode": "novel"},
    )
    assert resp.status_code == 400


def test_update_explainer_fields(client):
    pid = client.post(
        "/api/projects", json={"input_text": "为什么天是蓝的", "content_type": "explainer"}
    ).json()["id"]
    resp = client.put(
        f"/api/projects/{pid}",
        json={"audience": "成年人", "source_material": "资料", "ai_label": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["audience"], body["source_material"], body["ai_label"]) == (
        "成年人",
        "资料",
        False,
    )


# ── 人声来源 / 视频 prompt ────────────────────────────


def test_explainer_defaults_to_tts(settings, monkeypatch):
    monkeypatch.setattr(settings, "audio_dub_mode", "native")
    assert video_service.dub_mode_for(_explainer(), settings) == "tts"
    assert video_service.dub_mode_for(Project(title="短剧"), settings) == "native"
    # 项目上明确选了就听项目的
    assert video_service.dub_mode_for(_explainer(dub_mode="native"), settings) == "native"


def test_explainer_never_puts_narration_into_video_prompt(settings, monkeypatch):
    monkeypatch.setattr(settings, "video_prompt_narration", True)
    shot = Shot(
        id="s1", scene_id="scene_1", shot_no=1, video_prompt="sunlight", narration=NARRATION
    )
    assert NARRATION in video_service.build_prompt(shot, Project(title="短剧"))
    assert NARRATION not in video_service.build_prompt(shot, _explainer())
    segment = video_service.build_segment_prompt([shot], _explainer(), settings=settings)
    assert NARRATION not in segment


# ── 剧本检查 ──────────────────────────────────────────


def _narrated_script() -> Script:
    return Script(
        scenes=[
            Scene(
                id="scene_1",
                summary="开场提问",
                dialogue=[DialogueLine(speaker=NARRATOR, line=NARRATION, fact_refs=["fact_1"])],
            )
        ]
    )


def _speaker_errors(report) -> list[str]:
    return [
        i.message
        for i in report.issues
        if i.severity == IssueSeverity.ERROR and "不在角色列表" in i.message
    ]


def test_narrator_speaker_passes_only_in_explainer():
    bible = StoryBible(characters=[Character(id="char_1", name="林悦")])
    script = _narrated_script()
    explainer = consistency_service.validate_explainer_script(script, bible)
    assert _speaker_errors(explainer) == []
    assert _speaker_errors(consistency_service.validate_script(script, bible))


def test_explainer_still_flags_unknown_speakers():
    script = _narrated_script()
    script.scenes[0].dialogue.append(DialogueLine(speaker="char_9", line="我来补一句。"))
    report = consistency_service.validate_explainer_script(script, StoryBible())
    assert len(_speaker_errors(report)) == 1


# ── 出处校验 ──────────────────────────────────────────

MATERIAL = (
    "太阳光由各种颜色的光组成。阳光穿过大气层时，空气分子对短波长的蓝光散射得更强烈，"
    "所以我们从各个方向都能看到蓝光，天空因此呈现蓝色。日落时阳光穿过的大气更厚，"
    "蓝光在途中被散射掉了，剩下的红光和橙光让天边变红。"
)


def test_quote_found_despite_punctuation_and_spacing():
    state, real = explainer_service.match_quote(
        "空气分子 对短波长的蓝光, 散射得更强烈", MATERIAL, min_chars=8
    )
    assert state == "ok"
    assert real == "空气分子对短波长的蓝光散射得更强烈"


def test_slightly_altered_quote_is_replaced_with_the_real_sentence():
    # 模型抄错一个字（「强烈」写成「剧烈」）：算通过，并换回资料里的原句
    state, real = explainer_service.match_quote(
        "所以我们从各个方向都能看到蓝光，天空因此呈现蓝色", MATERIAL, min_chars=8
    )
    assert state == "ok"
    state, real = explainer_service.match_quote(
        "蓝光在途中被散射掉了，剩下的红光和黄光让天边变红", MATERIAL, min_chars=8
    )
    assert state == "ok"
    assert real == "蓝光在途中被散射掉了，剩下的红光和橙光让天边变红"


def test_short_and_fabricated_quotes():
    assert explainer_service.match_quote("蓝光", MATERIAL, min_chars=8)[0] == "short"
    state, _ = explainer_service.match_quote(
        "海水的颜色反射到天空上形成蓝色", MATERIAL, min_chars=8
    )
    assert state == "missing"


def test_fuzzy_match_stays_fast_on_long_material():
    material = "无关的段落内容，用来把资料撑长。" * 4000 + MATERIAL
    started = time.perf_counter()
    state, _ = explainer_service.match_quote(
        "蓝光在途中被散射掉了，剩下的红光和黄光让天边变红", material, min_chars=8
    )
    assert state == "ok"
    assert time.perf_counter() - started < 2.0


def test_verify_sources_marks_self_stated_and_missing():
    facts = [
        Fact(id="fact_1", content="a", source=""),
        Fact(id="fact_2", content="b", source="空气分子对短波长的蓝光散射得更强烈"),
        Fact(id="fact_3", content="c", source="海水的颜色反射到天空上形成蓝色"),
    ]
    no_material = [Fact(id="fact_9", content="d", source="")]
    explainer_service.verify_sources(no_material, "", min_chars=8)
    assert no_material[0].source == explainer_service.SOURCE_SELF
    explainer_service.verify_sources(facts, MATERIAL, min_chars=8)
    assert facts[0].source == explainer_service.SOURCE_NOT_FOUND
    assert facts[1].source == "空气分子对短波长的蓝光散射得更强烈"
    assert facts[2].source == explainer_service.SOURCE_NOT_FOUND
    assert [explainer_service.source_state(f, MATERIAL, min_chars=8) for f in facts] == [
        "missing",
        "ok",
        "missing",
    ]


# ── 底稿合并：id 与核对状态 ──────────────────────────


def _response(*items: tuple[str, str]) -> KnowledgeResponse:
    return KnowledgeResponse(facts=[KnowledgeFact(content=c, source=s) for c, s in items])


def test_regenerated_knowledge_keeps_ids_and_status_of_unchanged_facts():
    old = StoryBible(
        characters=[Character(id="char_1", name="讲解员")],
        facts=[
            Fact(id="fact_1", content="甲", source="出处甲", status="confirmed"),
            Fact(id="fact_2", content="乙", source="出处乙", status="confirmed"),
            Fact(id="fact_3", content="丙", source="出处丙", status="rejected"),
        ],
    )
    bible = explainer_service.merge_knowledge(
        [_response(("甲", "出处甲"), ("乙", "换了出处"), ("丁", "出处丁"), ("甲", "重复"))],
        old,
        max_facts=60,
    )
    by_content = {f.content: f for f in bible.facts}
    assert (by_content["甲"].id, by_content["甲"].status) == ("fact_1", "confirmed")
    # 出处变了：id 不变，但要重新核对
    assert (by_content["乙"].id, by_content["乙"].status) == ("fact_2", "unverified")
    # 新事实不复用被丢掉的 fact_3：旁白里残留的 fact_3 引用必须还能被查出来
    assert by_content["丁"].id == "fact_4"
    assert "丙" not in by_content
    assert [c.name for c in bible.characters] == ["讲解员"]


def test_manual_edit_resets_fact_status():
    old = StoryBible(
        facts=[
            Fact(id="fact_1", content="甲", source="s", status="confirmed"),
            Fact(id="fact_2", content="乙", source="s", status="confirmed"),
        ]
    )
    new = old.model_copy(deep=True)
    new.facts[0].content = "甲（改过）"
    new.facts[1].status = "rejected"  # 只改状态 = 人工核对，保留
    assert explainer_service.reset_changed_facts(old, new) == ["fact_1"]
    assert [f.status for f in new.facts] == ["unverified", "rejected"]


# ── 生成知识底稿（流水线）────────────────────────────


def _explainer_project(store, **kwargs) -> Project:
    kwargs.setdefault("input_text", "为什么天是蓝的")
    project = _explainer(**kwargs)
    store.create_project(project)
    return project


async def test_theme_knowledge_base_without_characters(store, make_pipeline):
    llm = FakeLLM()
    project = _explainer_project(store)
    bible = await make_pipeline(llm).generate_bible(project.id)
    assert bible.characters == []
    assert [f.id for f in bible.facts] == ["fact_1", "fact_2", "fact_3"]
    assert {f.source for f in bible.facts} == {explainer_service.SOURCE_SELF}
    assert {f.status for f in bible.facts} == {"unverified"}
    assert [s.id for s in bible.settings] == ["loc_1"]
    assert [p.id for p in bible.props] == ["prop_1"]


async def test_source_material_is_chunked_and_resumes_after_failure(
    store, make_pipeline, settings, monkeypatch
):
    monkeypatch.setattr(settings, "explainer_source_max_chars", 500)
    paragraph = "空气分子对短波长的蓝光散射得更强烈，这就是天空呈现蓝色的原因之一。"
    material = "\n".join(f"第{i}段：{paragraph}" * 8 for i in range(1, 4))
    project = _explainer_project(store, source_material=material)
    assert len(explainer_service.split_source(material, 500)) == 3

    llm = FakeLLM(fail_at="knowledge:2")
    pipeline = make_pipeline(llm)
    with pytest.raises(PipelineError):
        await pipeline.generate_bible(project.id)
    progress = store.load_explainer_source(project.id)
    assert [c.result is not None for c in progress.chunks] == [True, False, False]

    store.save_project(store.load_project(project.id).model_copy(update={"status": "draft"}))
    llm.fail_at = None
    bible = await pipeline.generate_bible(project.id)
    assert llm.knowledge_calls == 3  # 第 1 块没有重抽
    assert store.load_explainer_source(project.id) is None
    sources = [f.source for f in bible.facts]
    assert explainer_service.SOURCE_NOT_FOUND in sources
    assert any(s.startswith("第1段") for s in sources)


async def test_update_bible_resets_confirmed_fact_when_content_changes(store, make_pipeline):
    project = _explainer_project(store)
    pipeline = make_pipeline(FakeLLM())
    bible = await pipeline.generate_bible(project.id)
    data = bible.model_dump()
    data["facts"][0]["status"] = "confirmed"
    data["facts"][1]["status"] = "confirmed"
    await pipeline.update_bible(project.id, data)
    data["facts"][0]["content"] = "改过的内容"
    saved = await pipeline.update_bible(project.id, data)
    assert [f.status for f in saved.facts[:2]] == ["unverified", "confirmed"]


async def test_explainer_has_no_long_form_outline(store, make_pipeline):
    project = _explainer_project(store)
    pipeline = make_pipeline(FakeLLM())
    await pipeline.generate_bible(project.id)
    with pytest.raises(PipelineError, match="固定结构"):
        await pipeline.generate_story_outline(project.id)


# ── 剧本（流水线）────────────────────────────────────


async def _with_bible(store, make_pipeline, llm=None, **kwargs):
    llm = llm or FakeLLM()
    project = _explainer_project(store, **kwargs)
    pipeline = make_pipeline(llm)
    await pipeline.generate_bible(project.id)
    return project, pipeline, llm


async def test_theme_script_is_pure_narration_with_resolved_fact_refs(store, make_pipeline):
    project, pipeline, llm = await _with_bible(store, make_pipeline)
    script = await pipeline.generate_script(project.id)

    assert [s.id for s in script.scenes] == [f"scene_{i}" for i in range(1, 6)]
    lines = [d for s in script.scenes for d in s.dialogue]
    assert {d.speaker for d in lines} == {NARRATOR}
    assert not any("（停顿）" in d.line for d in lines)  # 舞台提示照样清掉
    bible = store.load_bible(project.id)
    facts = {f.id: f for f in bible.facts}
    # 每场的 new_1 都换成了正式 id，且新事实已在底稿里（未核对、模型自述）
    new_ids = [s.dialogue[1].fact_refs[0] for s in script.scenes]
    assert new_ids == [f"fact_{i}" for i in range(4, 9)]
    assert all(facts[i].status == "unverified" for i in new_ids)
    assert all(facts[i].source == explainer_service.SOURCE_SELF for i in new_ids)
    # 场次时长按旁白算，不按大纲预算
    for scene in script.scenes:
        assert scene.duration_sec == explainer_service.scene_seconds(scene)
    assert llm.explainer_reviews == 1
    assert not any("剧本医生" in str(c[0].content) for c in llm.calls)

    report = store.load_consistency(project.id)
    errors = [i for i in report.issues if i.severity == IssueSeverity.ERROR]
    assert errors == []
    assert any("还没核对" in i.message for i in report.issues)


async def test_outline_mode_keeps_user_points(store, make_pipeline):
    project, pipeline, llm = await _with_bible(
        store, make_pipeline, input_mode="outline"
    )
    await pipeline.generate_script(project.id)
    outline_call = next(c for c in llm.calls if "科普讲解的结构编辑" in str(c[0].content))
    assert "不增不删" in str(outline_call[-1].content)


async def test_explainer_script_resumes_after_failure(store, make_pipeline):
    llm = FakeLLM()
    project, pipeline, _ = await _with_bible(store, make_pipeline, llm)
    llm.fail_at = "explainer_scene:scene_3"
    with pytest.raises(PipelineError):
        await pipeline.generate_script(project.id)
    assert [s.id for s in store.load_script(project.id).scenes] == ["scene_1", "scene_2"]

    llm.fail_at = None
    before = sum(1 for c in llm.calls if "科普旁白撰稿" in str(c[0].content))
    script = await pipeline.generate_script(project.id)
    after = sum(1 for c in llm.calls if "科普旁白撰稿" in str(c[0].content))
    assert len(script.scenes) == 5
    assert after - before == 3  # 前两场没有重写


async def test_explainer_rejects_chapters(store, make_pipeline):
    project, pipeline, _ = await _with_bible(store, make_pipeline)
    with pytest.raises(PipelineError, match="没有章节"):
        await pipeline.generate_script(project.id, chapters=["ch_1"])


FULL_SCRIPT = (
    "天为什么是蓝的？（先想一想）\n"
    "阳光里有各种颜色的光。蓝光波长短，更容易被空气散射。\n\n"
    "所以抬头看，四面八方都是蓝光！日落时光走的路更长，蓝光散射没了，天就红了。"
)


async def test_full_script_keeps_every_user_sentence_verbatim(store, make_pipeline):
    project, pipeline, _ = await _with_bible(
        store, make_pipeline, input_mode="full_script", input_text=FULL_SCRIPT
    )
    script = await pipeline.generate_script(project.id)

    said = [d.line for s in script.scenes for d in s.dialogue]
    assert said == [text for _, text in explainer_service.split_sentences(FULL_SCRIPT)]
    assert said[0] == "天为什么是蓝的？（先想一想）"  # 用户写的括号也不动
    assert len(said) == 5
    assert [len(s.dialogue) for s in script.scenes] == [2, 2, 1]
    bible = store.load_bible(project.id)
    first_ref = script.scenes[0].dialogue[0].fact_refs[0]
    assert bible.facts[-1].id == first_ref
    assert store.load_outline(project.id).scenes[-1].id == "scene_3"


async def test_full_script_falls_back_to_paragraphs_when_grouping_is_broken(
    store, make_pipeline
):
    llm = FakeLLM()
    llm.explainer_annotate_valid = False
    project, pipeline, _ = await _with_bible(
        store, make_pipeline, llm, input_mode="full_script", input_text=FULL_SCRIPT
    )
    script = await pipeline.generate_script(project.id)
    # 模型漏了一句：按用户的 3 个段落分场，5 句一句不少
    assert [len(s.dialogue) for s in script.scenes] == [1, 2, 2]
    assert sum(len(s.dialogue) for s in script.scenes) == 5


async def test_rewriting_a_scene_adds_its_new_facts(store, make_pipeline):
    project, pipeline, _ = await _with_bible(store, make_pipeline)
    await pipeline.generate_script(project.id)
    count = len(store.load_bible(project.id).facts)
    scene = await pipeline.regenerate_scene(project.id, "scene_2", feedback="讲慢一点")
    assert "（重写）" in scene.dialogue[0].line
    bible = store.load_bible(project.id)
    assert scene.dialogue[1].fact_refs == [bible.facts[-1].id]
    assert len(bible.facts) == count + 1
    assert scene.duration_sec == explainer_service.scene_seconds(scene)


async def test_rejected_fact_blocks_confirm_until_forced(store, make_pipeline):
    project, pipeline, _ = await _with_bible(store, make_pipeline)
    await pipeline.generate_script(project.id)
    bible = store.load_bible(project.id).model_dump()
    bible["facts"][0]["status"] = "rejected"
    await pipeline.update_bible(project.id, bible)
    with pytest.raises(PipelineError, match="已驳回"):
        await pipeline.confirm_script(project.id)
    confirmed = await pipeline.confirm_script(project.id, force=True)
    assert confirmed.status == "script_confirmed"


async def test_review_errors_are_reported(store, make_pipeline):
    llm = FakeLLM()
    llm.explainer_review_issues = [
        {"severity": "error", "scene_id": "scene_2", "message": "把散射说成了反射"}
    ]
    project, pipeline, _ = await _with_bible(store, make_pipeline, llm)
    await pipeline.generate_script(project.id)
    report = store.load_consistency(project.id)
    assert any(
        i.severity == IssueSeverity.ERROR and "反射" in i.message for i in report.issues
    )


# ── 剧本检查规则 ──────────────────────────────────────


def _checked(script: Script, bible: StoryBible, **kwargs):
    return consistency_service.validate_explainer_script(script, bible, **kwargs)


def test_fact_reference_rules():
    bible = StoryBible(
        facts=[
            Fact(id="fact_1", content="甲", status="confirmed"),
            Fact(id="fact_2", content="乙", status="rejected"),
            Fact(id="fact_3", content="丙"),
        ]
    )
    script = Script(
        scenes=[
            Scene(
                id="scene_1",
                summary="s",
                dialogue=[
                    DialogueLine(speaker=NARRATOR, line="一", fact_refs=["fact_1", "fact_9"]),
                    DialogueLine(speaker=NARRATOR, line="二", fact_refs=["fact_2", "fact_3"]),
                ],
            )
        ]
    )
    report = _checked(script, bible)
    errors = [i.message for i in report.issues if i.severity == IssueSeverity.ERROR]
    assert len(errors) == 2
    assert any("不存在的事实 fact_9" in m for m in errors)
    assert any("已驳回的事实 fact_2" in m for m in errors)
    unverified = [i.message for i in report.issues if "还没核对" in i.message]
    assert unverified == ["1 条被旁白引用的事实还没核对：fact_3"]


def test_source_warnings_only_with_material():
    bible = StoryBible(
        facts=[
            Fact(id="fact_1", content="甲", source=explainer_service.SOURCE_NOT_FOUND),
            Fact(id="fact_2", content="乙", source="蓝光"),
            Fact(id="fact_3", content="丙", source="空气分子对短波长的蓝光散射得更强烈"),
        ]
    )
    script = Script(
        scenes=[
            Scene(
                id="scene_1",
                summary="s",
                dialogue=[
                    DialogueLine(
                        speaker=NARRATOR, line="一", fact_refs=["fact_1", "fact_2", "fact_3"]
                    )
                ],
            )
        ]
    )
    with_material = [i.message for i in _checked(script, bible, source_material=MATERIAL).issues]
    assert any("fact_1 的出处在参考资料里找不到" in m for m in with_material)
    assert any("fact_2 的引文太短" in m for m in with_material)
    assert not any("fact_3" in m and "出处" in m for m in with_material)
    without = [i.message for i in _checked(script, bible).issues]
    assert not any("出处" in m or "引文" in m for m in without)


def test_drama_only_checks_are_skipped_and_length_is_checked():
    long_line = "牛顿说光是由不同颜色组成的，这个实验后来被很多人重复过很多次。"
    script = Script(
        scenes=[
            Scene(
                id=f"scene_{i}",
                summary=f"第 {i} 场",
                dialogue=[DialogueLine(speaker=NARRATOR, line=long_line)] * 4,
                duration_sec=10,
            )
            for i in range(1, 4)
        ]
    )
    report = _checked(script, StoryBible(), target_duration_sec=30)
    messages = [i.message for i in report.issues]
    assert not any("人名" in m for m in messages)  # 「牛顿说」不报未定义人名
    assert not any("冲突" in m or "悬念" in m for m in messages)
    assert any("旁白念完约" in m for m in messages)
    assert not any("旁白念完约" in m for m in [
        i.message for i in _checked(script, StoryBible(), target_duration_sec=120).issues
    ])


def test_known_character_line_is_only_a_warning():
    bible = StoryBible(characters=[Character(id="char_1", name="讲解员")])
    script = _narrated_script()
    script.scenes[0].dialogue.append(DialogueLine(speaker="char_1", line="我来补一句。"))
    report = _checked(script, bible)
    assert _speaker_errors(report) == []
    assert any("旁白以外的说话人" in i.message for i in report.issues)


def test_new_fact_with_existing_content_points_to_the_existing_one():
    bible = StoryBible(facts=[Fact(id="fact_1", content="甲"), Fact(id="fact_3", content="丙")])
    mapping = explainer_service.add_new_facts(
        bible,
        [NewFact(key="new_1", content=" 甲 "), NewFact(key="new_2", content="丁")],
        "",
    )
    assert mapping == {"new_1": "fact_1", "new_2": "fact_4"}
    assert [f.id for f in bible.facts] == ["fact_1", "fact_3", "fact_4"]
    assert bible.facts[-1].source == explainer_service.SOURCE_SELF


# ── 分镜：一句一镜 ────────────────────────────────────


async def _with_storyboard(store, make_pipeline, llm=None, **kwargs):
    project, pipeline, llm = await _with_bible(store, make_pipeline, llm, **kwargs)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id, force=True)
    storyboard = await pipeline.generate_storyboard(project.id)
    return project, pipeline, llm, storyboard


async def test_storyboard_is_one_shot_per_sentence(store, make_pipeline, settings):
    assert settings.video_segment_mode is False  # 测试基线关着段落分镜
    project, _, llm, storyboard = await _with_storyboard(store, make_pipeline)
    script = store.load_script(project.id)

    lines = [(s.id, d) for s in script.scenes for d in s.dialogue]
    assert len(storyboard.shots) == len(lines)
    for shot, (scene_id, line) in zip(storyboard.shots, lines, strict=True):
        assert shot.scene_id == scene_id
        # 旁白、事实引用、时长由代码按句子定，模型改的旁白和时长不认
        assert shot.narration == line.line
        assert (shot.dialogue_text, shot.dialogue_speaker) == (None, None)
        assert shot.fact_refs == line.fact_refs
        assert shot.duration_sec == explainer_service.line_seconds(line.line)
        assert shot.video_prompt.startswith("macro shot")
        assert shot.character_in_frame == []  # 不存在的角色 id 被丢掉
        assert shot.location == "loc_1"
    # 场次时长 = 各句镜头之和，没被归一化改动
    for scene in script.scenes:
        assert sum(s.duration_sec for s in storyboard.shots_for_scene(scene.id)) == (
            scene.duration_sec
        )
    # VIDEO_SEGMENT_MODE=false 对科普不生效：照样编段
    assert all(shot.segment_id for shot in storyboard.shots)
    assert not any("专业分镜师" in str(c[0].content) for c in llm.calls)
    assert len(llm.explainer_visual_prompts) == len(script.scenes)
    report = store.load_consistency(project.id)
    assert not any(
        i.severity == IssueSeverity.ERROR and i.shot_id for i in report.issues
    )


async def test_storyboard_visual_prompt_carries_the_rules(store, make_pipeline):
    _, _, llm, _ = await _with_storyboard(store, make_pipeline)
    system = next(
        str(c[0].content) for c in llm.calls if "科普画面分镜师" in str(c[0].content)
    )
    assert "不许合并、拆分、跳过句子" in system
    assert "no text, no labels" in system
    assert "1. " in llm.explainer_visual_prompts[0]


async def test_missing_visuals_are_flagged_not_invented(store, make_pipeline):
    llm = FakeLLM()
    llm.explainer_visuals_skip_last = True
    project, _, _, storyboard = await _with_storyboard(store, make_pipeline, llm)
    script = store.load_script(project.id)
    assert len(storyboard.shots) == sum(len(s.dialogue) for s in script.scenes)
    blank = [s.id for s in storyboard.shots if not s.video_prompt]
    assert blank == [storyboard.shots_for_scene(s.id)[-1].id for s in script.scenes]
    report = store.load_consistency(project.id)
    flagged = {i.shot_id for i in report.issues if "缺少 video_prompt" in i.message}
    assert flagged == set(blank)


async def test_explainer_storyboard_resumes_after_failure(store, make_pipeline):
    llm = FakeLLM()
    project, pipeline, _ = await _with_bible(store, make_pipeline, llm)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id, force=True)
    llm.fail_at = "explainer_visuals:scene_3"
    with pytest.raises(PipelineError):
        await pipeline.generate_storyboard(project.id)
    assert store.load_storyboard(project.id).scene_ids() == {"scene_1", "scene_2"}

    llm.fail_at = None
    before = len(llm.explainer_visual_prompts)
    storyboard = await pipeline.generate_storyboard(project.id)
    assert len(llm.explainer_visual_prompts) - before == 3
    assert len(storyboard.scene_ids()) == 5


async def test_revise_only_rewrites_visual_fields(store, make_pipeline):
    project, pipeline, _, storyboard = await _with_storyboard(store, make_pipeline)
    target = storyboard.shots_for_scene("scene_2")[1]

    result = await pipeline.propose_storyboard_revision(
        project.id, shot_id=target.id, feedback="换成日落的画面"
    )
    assert result["changed_shots"] == [target.id]
    shots = {s["id"]: s for s in result["storyboard"]["shots"]}
    fresh = shots[target.id]
    assert fresh["video_prompt"].endswith("no text, no labels")
    assert "revised" in fresh["video_prompt"]
    for field in ("narration", "fact_refs", "duration_sec", "segment_id"):
        assert fresh[field] == getattr(target, field)
    others = [s for s in storyboard.shots if s.id != target.id]
    assert all(shots[s.id]["video_prompt"] == s.video_prompt for s in others)

    result = await pipeline.propose_storyboard_revision(project.id, scene_id="scene_3")
    scene_shots = storyboard.shots_for_scene("scene_3")
    assert result["changed_shots"] == [s.id for s in scene_shots]
    revised = [s for s in result["storyboard"]["shots"] if s["scene_id"] == "scene_3"]
    assert [s["narration"] for s in revised] == [s.narration for s in scene_shots]
    assert len(result["storyboard"]["shots"]) == len(storyboard.shots)
    # 只是提案：分镜没被保存
    saved = store.load_storyboard(project.id)
    assert [s.video_prompt for s in saved.shots] == [s.video_prompt for s in storyboard.shots]


async def test_merge_shots_is_refused(store, make_pipeline):
    project, pipeline, _, _ = await _with_storyboard(store, make_pipeline)
    await pipeline.confirm_storyboard(project.id, force=True)
    with pytest.raises(PipelineError, match="一句旁白一个镜头"):
        await pipeline.merge_short_shots(project.id)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("off", "off"), ("perform", "perform"), ("narration", "perform"),
     ("polish", "perform"), ("full", "perform")],
)
def test_voice_script_mode_is_limited_for_explainer(mode, expected):
    actual, warning = explainer_service.voice_script_mode(_explainer(), mode)
    assert actual == expected
    assert bool(warning) == (mode != expected)
    assert explainer_service.voice_script_mode(Project(title="短剧"), mode) == (mode, "")


async def test_preflight_uses_the_limited_voice_script_mode(store, make_pipeline):
    project, pipeline, _, _ = await _with_storyboard(store, make_pipeline)
    plan = await pipeline.preflight(project.id, action="audio", script_mode="polish")
    assert plan["audio"]["script_mode"] == "perform"


def test_presenter_line_becomes_dialogue_in_frame():
    bible = StoryBible(characters=[Character(id="char_1", name="讲解员")])
    scene = Scene(
        id="scene_1",
        heading="开场",
        dialogue=[
            DialogueLine(speaker=NARRATOR, line="天为什么是蓝的？", fact_refs=["fact_1"]),
            DialogueLine(speaker="char_1", line="我们来做个实验。"),
            DialogueLine(speaker=NARRATOR, line="  "),
        ],
    )
    shots = explainer_service.shots_from_scene(scene, bible)
    assert [s.id for s in shots] == ["scene_1_shot_1", "scene_1_shot_2"]
    assert (shots[0].narration, shots[0].fact_refs) == ("天为什么是蓝的？", ["fact_1"])
    assert (shots[1].dialogue_text, shots[1].dialogue_speaker) == ("我们来做个实验。", "char_1")
    assert shots[1].narration is None and shots[1].character_in_frame == ["char_1"]


# ── 配音与合成 ────────────────────────────────────────


def test_ai_label_only_for_explainer_with_label_on(settings):
    assert explainer_service.ai_label_text(Project(title="短剧")) == ""
    assert explainer_service.ai_label_text(_explainer(ai_label=False)) == ""
    assert explainer_service.ai_label_text(_explainer()) == "本视频由 AI 生成"
    assert "AI-generated" in explainer_service.ai_label_text(_explainer(language="en"))
    assert explainer_service.ai_metadata(Project(title="短剧")) == {}
    assert "AIGC" in explainer_service.ai_metadata(_explainer())["comment"]


def test_ass_label_sits_on_top_for_the_first_seconds():
    segments = [
        ComposeSegment(shot_id="a", index=0, duration_sec=2.0, text="第一句旁白"),
        ComposeSegment(shot_id="b", index=1, duration_sec=2.0, text="第二句旁白"),
    ]
    look = subtitle_service.subtitle_look("box")[1]
    ass = subtitle_service.build_ass(
        segments, resolution=(1080, 1920), look=look, label="本视频由 AI 生成", label_sec=3.0
    )
    label_style = next(line for line in ass.splitlines() if line.startswith("Style: Label"))
    assert label_style.split(",")[18] == "8"  # 顶部居中，躲开底部字幕
    assert "Dialogue: 1,0:00:00.00,0:00:03.00,Label" in ass
    assert "第一句旁白" in ass

    only = subtitle_service.build_ass(
        segments, resolution=(1080, 1920), look=look, label="本视频由 AI 生成", cues=False
    )
    assert [line for line in only.splitlines() if line.startswith("Dialogue")] == [
        "Dialogue: 1,0:00:00.00,0:00:03.00,Label,,0,0,0,,本视频由 AI 生成"
    ]
    plain = subtitle_service.build_ass(segments, resolution=(1080, 1920), look=look)
    assert "Label" not in plain


def test_fit_to_voice_trims_to_speech_plus_gap(tmp_path, monkeypatch):
    lengths = {"a.mp4": 4.0, "b.mp4": 2.0, "c.mp4": 3.0}

    async def fake_probe(path, **_):
        return lengths[Path(path).name]

    monkeypatch.setattr(compose_service, "probe_duration", fake_probe)
    segments = [
        ComposeSegment(shot_id=sid, index=i, duration_sec=dur, video_path=f"{sid}.mp4",
                       audio_path=audio)
        for i, (sid, dur, audio) in enumerate(
            [("a", 4.0, "audio/a.wav"), ("b", 3.0, "audio/b.wav"), ("c", 3.0, None)]
        )
    ]
    audio = AudioManifest(
        clips=[
            AudioClip(id=sid, shot_id=sid, text="旁白", voice="v", status=ClipStatus.READY,
                      path=f"audio/{sid}.wav", duration_sec=dur)
            for sid, dur in (("a", 2.31), ("b", 2.6))
        ]
    )
    warnings: list[str] = []
    asyncio.run(compose_service._fit_to_voice(segments, audio, tmp_path, 0.2, warnings))
    assert [s.duration_sec for s in segments] == [2.51, 2.8, 3.0]
    assert len(warnings) == 1 and "b（0.8s）" in warnings[0]  # 画面 2 秒不够，冻结补足


def test_finish_writes_metadata_on_both_paths(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    async def fake_run(args, **_):
        seen.append(args)

    monkeypatch.setattr(compose_service, "run_ffmpeg", fake_run)
    for mode in (SubtitleMode.BURN, SubtitleMode.SOFT):
        asyncio.run(
            compose_service._finish(
                Path("base.mp4"), Path("final.mp4"), cwd=tmp_path, duration=5.0,
                bgm_path=None, bgm_gain_db=-20.0, fade_sec=1.0,
                subtitle_file=Path("subtitles.ass"), burn_file=Path("subtitles.ass"),
                mode=mode, caps={"subtitles", "mov_text"}, crf=23, preset="veryfast",
                metadata={"comment": "AIGC: 本视频由 AI 生成"},
            )
        )
    for args in seen:
        index = args.index("-metadata")
        assert args[index + 1] == "comment=AIGC: 本视频由 AI 生成"


@requires_ffmpeg  # 科普强制段落分镜：整段要用 ffmpeg 切回单镜
async def test_explainer_audio_does_not_write_back_durations(store, make_pipeline):
    llm = FakeLLM()
    project, _, _, storyboard = await _with_storyboard(store, make_pipeline, llm)
    pipeline = Pipeline(
        store,
        llm_factory=lambda: llm,
        video_factory=lambda: MockVideoProvider(),
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    await pipeline.confirm_storyboard(project.id, force=True)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)
    job = await pipeline.start_audio_generation(project.id, script_mode="polish")
    finished = await wait_for_job(pipeline, job.id)
    assert finished.status.value == "succeeded", finished.error

    after = store.load_storyboard(project.id)
    assert [s.duration_sec for s in after.shots] == [s.duration_sec for s in storyboard.shots]
    assert [s.narration for s in after.shots] == [s.narration for s in storyboard.shots]
    manifest = store.load_audio_manifest(project.id)
    assert manifest.adjustments == []
    assert manifest.script_mode == "perform"
    assert any("只允许 off / perform" in w for w in manifest.warnings)
    assert "perform" in llm.voice_modes and "polish" not in llm.voice_modes


def _explainer_media_project(store, *, ai_label: bool = True) -> Project:
    project = small_project(store, shots=2, shot_duration=3, dialogue="天之所以是蓝的。")
    project = project.model_copy(
        update={"content_type": ContentType.EXPLAINER, "ai_label": ai_label}
    )
    store.save_project(project)
    storyboard = store.load_storyboard(project.id)
    for shot in storyboard.shots:
        shot.narration, shot.dialogue_text, shot.dialogue_speaker = shot.dialogue_text, None, None
    store.save_storyboard(project.id, storyboard)
    return project


async def _compose_explainer(store, project, overrides=None):
    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: MockVideoProvider(),
        tts_factory=lambda: MockTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    for start in (pipeline.start_video_generation, pipeline.start_audio_generation):
        job = await start(project.id)
        await wait_for_job(pipeline, job.id)
    job = await pipeline.start_compose(project.id, overrides=overrides)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    return finished, store.load_compose_report(project.id)


@requires_ffmpeg
async def test_explainer_compose_trims_to_voice_and_labels(store, monkeypatch):
    project = _explainer_media_project(store)
    seen: dict = {}
    original = compose_service._render_all

    async def spy(*args, **kwargs):
        seen["align_mouth"] = kwargs.get("align_mouth")
        return await original(*args, **kwargs)

    monkeypatch.setattr(compose_service, "_render_all", spy)
    finished, report = await _compose_explainer(
        store, project, {"compose_subtitle_mode": "soft"}
    )
    assert finished.status.value == "succeeded", finished.error
    if not any("AI 生成」标识" in w for w in report.warnings):
        pytest.skip("本机没有带 libass 的 ffmpeg")

    assert seen["align_mouth"] is False
    audio = store.load_audio_manifest(project.id)
    for segment in report.segments:
        voice = audio.for_shot(segment.shot_id)
        assert segment.duration_sec == pytest.approx(voice.duration_sec + 0.2, abs=0.001)
    assert store.load_storyboard(project.id).shots[0].duration_sec == 3  # 分镜没被改

    assert report.ai_label == "本视频由 AI 生成"
    assert report.subtitle_mode_effective == SubtitleMode.BURN
    ass = (store.output_dir(project.id) / "subtitles.ass").read_text(encoding="utf-8")
    assert "本视频由 AI 生成" in ass and "天之所以是蓝的" in ass
    info = await describe_media(store.output_dir(project.id) / "final.mp4")
    assert "AIGC" in info


@requires_ffmpeg
async def test_explainer_label_is_burned_even_without_subtitles(store):
    project = _explainer_media_project(store)
    finished, report = await _compose_explainer(store, project, {"compose_subtitle_mode": "none"})
    if finished.status.value != "succeeded" and "libass" in (finished.error or ""):
        pytest.skip("本机没有带 libass 的 ffmpeg")
    assert finished.status.value == "succeeded", finished.error
    assert report.subtitle_mode_effective == SubtitleMode.NONE
    assert report.ai_label
    ass = (store.output_dir(project.id) / "subtitles.ass").read_text(encoding="utf-8")
    assert "天之所以是蓝的" not in ass  # 字幕不烧，只烧标识
    assert (store.output_dir(project.id) / "subtitles.srt").is_file()  # 侧挂照旧


@requires_ffmpeg
async def test_explainer_without_label_keeps_soft_subtitles(store):
    project = _explainer_media_project(store, ai_label=False)
    finished, report = await _compose_explainer(store, project, {"compose_subtitle_mode": "soft"})
    assert finished.status.value == "succeeded", finished.error
    assert report.ai_label == ""
    assert report.subtitle_mode_effective == SubtitleMode.SOFT
    assert "AIGC" not in await describe_media(store.output_dir(project.id) / "final.mp4")
