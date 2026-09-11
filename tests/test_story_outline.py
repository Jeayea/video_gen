"""长篇剧集大纲（20+ 章）+ 按章展开成剧本。

为什么单独测：这是「剧情太简略/太平淡」的解药 —— 先出全篇因果骨架，再挑章
展开成可拍摄的场次。它同时引入了一条新的状态流（章 -> 场次 -> 分镜），
所以「续写第二章」这类追加语义必须有回归保护。
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeLLM
from videogen.models.bible import Character, StoryBible
from videogen.models.project import Project, ProjectStatus
from videogen.services import script_service


async def build_text_project(store, pipeline) -> Project:
    """建项目 + 生成圣经（长篇大纲的前置条件）。"""
    project = Project(title="长篇测试", input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    return store.load_project(project.id)


@pytest.mark.asyncio
async def test_story_outline_spine_then_chunked_expansion(store, pipeline, fake_llm):
    """骨架一次给全 + 正文分批扩写（20 章一次写完必被输出长度截断）。"""
    project = await build_text_project(store, pipeline)
    outline = await pipeline.generate_story_outline(project.id, chapters=20)

    assert len(outline.chapters) == 20
    assert outline.title and outline.logline and outline.synopsis
    assert all(c.expanded_ready for c in outline.chapters)
    assert all(c.summary and c.beats and c.hook for c in outline.chapters)
    assert all(c.id == f"ch_{i}" for i, c in enumerate(outline.chapters, start=1))
    # 落盘了（续跑与「按章展开」都读这个文件）
    assert store.load_story_outline(project.id) is not None
    # 扩写是分批调用，不是一次 20 章
    spine_calls = sum(1 for c in fake_llm.calls if "你只写「骨架」" in str(c[0].content))
    assert spine_calls == 1


@pytest.mark.asyncio
async def test_story_outline_resume_skips_expanded_chapters(store, pipeline, fake_llm):
    """续写：已经扩写过的章不再重复花钱。"""
    project = await build_text_project(store, pipeline)
    first = await pipeline.generate_story_outline(project.id, chapters=6, force=True)
    # 手动把后两章打回「未扩写」，模拟中断
    first.chapters[4].summary = ""
    first.chapters[4].beats = []
    first.chapters[5].summary = ""
    first.chapters[5].beats = []
    store.save_story_outline(project.id, first)

    before = len(fake_llm.calls)
    outline = await pipeline.generate_story_outline(project.id, chapters=6)
    assert all(c.expanded_ready for c in outline.chapters)
    # 骨架没重出（用户没要求 force）：只有扩写的调用
    new_calls = fake_llm.calls[before:]
    assert new_calls
    assert not any("你只写「骨架」" in str(c[0].content) for c in new_calls)


@pytest.mark.asyncio
async def test_force_spine_keeps_matching_chapter_text(store, pipeline, fake_llm):
    """重出骨架时，章 id + 标题都对得上的旧正文要保留（别白花一遍钱）。"""
    project = await build_text_project(store, pipeline)
    await pipeline.generate_story_outline(project.id, chapters=4, force=True)
    again = await pipeline.generate_story_outline(project.id, chapters=4, force=True)
    assert len(again.chapters) == 4
    assert all(c.expanded_ready for c in again.chapters)


@pytest.mark.asyncio
async def test_expand_only_selected_chapters(store, pipeline, fake_llm):
    """选中 2 章 -> 只生成这 2 章的场次，且场次 id 从 scene_1 连续编号。"""
    project = await build_text_project(store, pipeline)
    await pipeline.generate_story_outline(project.id, chapters=8, force=True)

    script = await pipeline.generate_script(project.id, chapters=["ch_2", "ch_3"])
    assert [s.id for s in script.scenes] == ["scene_1", "scene_2", "scene_3", "scene_4"]

    outline = store.load_outline(project.id)
    assert outline is not None
    assert outline.chapter_ids() == {"ch_2", "ch_3"}
    # 环境线索与章归属都留在分场大纲里（背景丰富度与可追溯）
    assert all(s.chapter_id in {"ch_2", "ch_3"} for s in outline.scenes)
    assert all(s.environment for s in outline.scenes)

    # 项目目标时长按已展开的章对齐（成本估算/时长归一化都以它为准）
    saved = store.load_project(project.id)
    assert saved.target_duration_sec == sum(
        store.load_story_outline(project.id).get_chapter(c).duration_budget_sec
        for c in ("ch_2", "ch_3")
    )


@pytest.mark.asyncio
async def test_expanding_another_chapter_appends(store, pipeline, fake_llm):
    """续写第二章：追加而不是重来（已生成的场次与剧本原文保留）。"""
    project = await build_text_project(store, pipeline)
    await pipeline.generate_story_outline(project.id, chapters=8, force=True)
    await pipeline.generate_script(project.id, chapters=["ch_1"])
    first = store.load_script(project.id)
    assert len(first.scenes) == 2

    project = store.load_project(project.id)
    project.status = ProjectStatus.SCRIPT_REVIEW
    store.save_project(project)
    script = await pipeline.generate_script(project.id, chapters=["ch_2"])

    assert len(script.scenes) == 4
    # 第一章的场次原样保留
    assert [s.id for s in script.scenes] == ["scene_1", "scene_2", "scene_3", "scene_4"]
    assert script.scenes[0].summary == first.scenes[0].summary
    assert store.load_outline(project.id).chapter_ids() == {"ch_1", "ch_2"}


@pytest.mark.asyncio
async def test_expanding_same_chapter_twice_is_free(store, pipeline, fake_llm):
    """同一章重复点「生成」不该重复花钱（已经展开过的章直接跳过）。"""
    project = await build_text_project(store, pipeline)
    await pipeline.generate_story_outline(project.id, chapters=4, force=True)
    await pipeline.generate_script(project.id, chapters=["ch_1"])
    project = store.load_project(project.id)
    project.status = ProjectStatus.SCRIPT_REVIEW
    store.save_project(project)

    before = len(fake_llm.calls)
    script = await pipeline.generate_script(project.id, chapters=["ch_1"])
    assert len(script.scenes) == 2
    scene_calls = [c for c in fake_llm.calls[before:] if "逐场编写短剧剧本" in str(c[0].content)]
    assert scene_calls == []


@pytest.mark.asyncio
async def test_expand_rejects_unknown_chapters(store, pipeline):
    project = await build_text_project(store, pipeline)
    await pipeline.generate_story_outline(project.id, chapters=4, force=True)
    with pytest.raises(Exception) as exc:
        await pipeline.generate_script(project.id, chapters=["ch_404"])
    assert "没有可展开的章" in str(exc.value)


@pytest.mark.asyncio
async def test_legacy_path_without_story_outline_unchanged(store, pipeline):
    """没有长篇大纲的项目仍然走老路径（一次性分场大纲）。"""
    project = await build_text_project(store, pipeline)
    script = await pipeline.generate_script(project.id)
    assert script.scenes
    assert store.load_story_outline(project.id) is None
    assert store.load_outline(project.id).chapter_ids() == set()


@pytest.mark.asyncio
async def test_generate_story_outline_requires_draft_status(store, pipeline):
    project = await build_text_project(store, pipeline)
    await pipeline.generate_story_outline(project.id, chapters=2, force=True)
    await pipeline.generate_script(project.id)
    project = store.load_project(project.id)
    project.status = ProjectStatus.STORYBOARD_CONFIRMED
    store.save_project(project)
    with pytest.raises(Exception) as exc:
        await pipeline.generate_story_outline(project.id, chapters=2)
    assert "不允许" in str(exc.value)


@pytest.mark.asyncio
async def test_chapter_scene_claiming_when_model_omits_chapter_id(store, bible):
    """模型不回 chapter_id 时按各章场次预算顺序认领（不能把场次全记到第一章）。"""
    llm = FakeLLM(story_chapters=2, chapter_scenes=2)

    class NoIdLLM(FakeLLM):
        def _chapter_scenes(self, user: str) -> dict:
            payload = super()._chapter_scenes(user)
            for scene in payload["scenes"]:
                scene.pop("chapter_id", None)
            return payload

    noid = NoIdLLM(story_chapters=2, chapter_scenes=2)
    outline = await script_service.generate_story_outline(llm, _project(), bible, chapters=2)
    story = outline
    expanded = await script_service.expand_chapters(
        noid, _project(), bible, story, ["ch_1", "ch_2"]
    )
    ids = [s.chapter_id for s in expanded.scenes]
    assert ids == ["ch_1", "ch_1", "ch_2", "ch_2"]


def _project():
    return type(
        "P",
        (),
        {
            "id": "p1",
            "title": "t",
            "input_text": "x",
            "input_mode": type("M", (), {"value": "theme"})(),
            "target_duration_sec": 30,
        },
    )()


@pytest.mark.asyncio
async def test_partial_arc_flag_flips_when_all_chapters_expanded(store, pipeline):
    """选了几章 = partial（结尾悬念不算硬伤）；全展开完 = 不是 partial。"""
    project = await build_text_project(store, pipeline)
    await pipeline.generate_story_outline(project.id, chapters=4, force=True)
    await pipeline.generate_script(project.id, chapters=["ch_1"])
    assert pipeline._is_partial_arc(project.id) is True
    # partial 时报告里不能出现「结尾没落地」
    report = store.load_consistency(project.id)
    assert not any("没有落地" in issue.message for issue in report.issues)

    project = store.load_project(project.id)
    project.status = ProjectStatus.SCRIPT_REVIEW
    store.save_project(project)
    for chapter_id in ("ch_2", "ch_3", "ch_4"):
        await pipeline.generate_script(project.id, chapters=[chapter_id])
    assert pipeline._is_partial_arc(project.id) is False


def test_open_ending_is_only_a_warning_for_a_complete_arc():
    """确定性判据本身：`partial=True` 时不报「结尾悬着」，全片时才报。"""
    from videogen.models.script import DialogueLine, Scene, Script
    from videogen.services import consistency_service

    script = Script(
        scenes=[
            Scene(id="scene_1", summary="开场", duration_sec=10),
            Scene(
                id="scene_2",
                summary="结尾",
                duration_sec=10,
                dialogue=[DialogueLine(speaker="char_1", line="她究竟会不会回来？")],
            ),
        ]
    )
    bible = StoryBible(characters=[Character(id="char_1", name="林悦")])
    whole = consistency_service.validate_script(script, bible)
    assert any("没有落地" in issue.message for issue in whole.issues)

    partial = consistency_service.validate_script(script, bible, partial=True)
    assert not any("没有落地" in issue.message for issue in partial.issues)
