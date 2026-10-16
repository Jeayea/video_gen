"""知识讲解型科普（`content_type=explainer`，docs/EXPLAINER_DESIGN.md）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen import main
from videogen.models.bible import Character, Fact, StoryBible
from videogen.models.consistency import IssueSeverity
from videogen.models.project import ContentType, Project
from videogen.models.script import NARRATOR, DialogueLine, Scene, Script
from videogen.models.storyboard import Shot
from videogen.orchestrator.pipeline import Pipeline
from videogen.services import audio_service, consistency_service, video_service, voice_catalog
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
