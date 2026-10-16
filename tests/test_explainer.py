"""知识讲解型科普（`content_type=explainer`，docs/EXPLAINER_DESIGN.md）。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen import main
from videogen.models.bible import Character, Fact, StoryBible
from videogen.models.consistency import IssueSeverity
from videogen.models.explainer import KnowledgeFact, KnowledgeResponse
from videogen.models.project import ContentType, Project
from videogen.models.script import NARRATOR, DialogueLine, Scene, Script
from videogen.models.storyboard import Shot
from videogen.orchestrator.pipeline import Pipeline, PipelineError
from videogen.services import (
    audio_service,
    consistency_service,
    explainer_service,
    video_service,
    voice_catalog,
)
from videogen.utils.file_store import FileStore

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
    assert _speaker_errors(consistency_service.validate_script(script, bible, explainer=True)) == []
    assert _speaker_errors(consistency_service.validate_script(script, bible))


def test_explainer_still_flags_unknown_speakers():
    script = _narrated_script()
    script.scenes[0].dialogue.append(DialogueLine(speaker="char_9", line="我来补一句。"))
    report = consistency_service.validate_script(script, StoryBible(), explainer=True)
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
    project = _explainer(input_text="为什么天是蓝的", **kwargs)
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
