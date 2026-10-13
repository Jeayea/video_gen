"""局部重做：圣经单条重写 / 已展开的章重做 / 单句重配。"""

from __future__ import annotations

import pytest

from tests.conftest import wait_for_job
from videogen.models.project import Project, ProjectStatus
from videogen.models.video import ClipStatus
from videogen.orchestrator.pipeline import PipelineError


async def _bible_project(store, pipeline) -> Project:
    project = Project(title="局部重做", input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    return store.load_project(project.id)


# ── 圣经单条 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_regenerate_one_character_keeps_others_and_pinned_fields(store, pipeline, fake_llm):
    project = await _bible_project(store, pipeline)
    bible = store.load_bible(project.id)
    target = bible.characters[1]
    target.reference_image = "refs/char_2.png"
    target.voice_id = "zh-CN-YunyangNeural"
    store.save_bible(project.id, bible)

    result = await pipeline.regenerate_bible_item(
        project.id, "characters", target.id, feedback="更阴险，四十多岁"
    )

    prompt = fake_llm.calls[-1][-1].content
    assert "更阴险，四十多岁" in prompt and f"条目 id: {target.id}" in prompt
    after = store.load_bible(project.id)
    fresh = next(c for c in after.characters if c.id == target.id)
    assert fresh.description.endswith("（已修订）")
    # 参考图 / 音色不交给模型改
    assert fresh.reference_image == "refs/char_2.png"
    assert fresh.voice_id == "zh-CN-YunyangNeural"
    # 外貌变了、又有参考图 → 提示重画
    assert result["reference_stale"] is True
    others = [c.model_dump() for c in after.characters if c.id != target.id]
    assert others == [c.model_dump() for c in bible.characters if c.id != target.id]


@pytest.mark.asyncio
async def test_regenerate_setting_and_unknown_item(store, pipeline):
    project = await _bible_project(store, pipeline)
    loc = store.load_bible(project.id).settings[0]
    result = await pipeline.regenerate_bible_item(project.id, "settings", loc.id)
    assert result["item"]["id"] == loc.id
    assert result["reference_stale"] is False  # 没有参考图就不用重画
    with pytest.raises(PipelineError):
        await pipeline.regenerate_bible_item(project.id, "characters", "char_99")


@pytest.mark.asyncio
async def test_bible_item_is_locked_after_script_confirmed(ready_project, pipeline):
    with pytest.raises(PipelineError):
        await pipeline.regenerate_bible_item(ready_project.id, "characters", "char_1")


# ── 重做已展开的章 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_redo_chapter_replaces_only_its_scenes(store, pipeline, fake_llm):
    project = await _bible_project(store, pipeline)
    await pipeline.generate_story_outline(project.id, chapters=6, force=True)
    await pipeline.generate_script(project.id, chapters=["ch_1", "ch_2"])
    before = store.load_script(project.id)
    assert [s.id for s in before.scenes] == ["scene_1", "scene_2", "scene_3", "scene_4"]
    ch2 = [s.summary for s in before.scenes[2:]]

    fake_llm.chapter_scenes = 3  # 按意见重做后第一章变成 3 场
    script = await pipeline.generate_script(
        project.id, redo_chapters=["ch_1"], feedback="开场先上冲突"
    )

    redo_prompt = next(
        c[-1].content for c in fake_llm.calls if "重做这一章" in str(c[-1].content)
    )
    assert "开场先上冲突" in redo_prompt and "ch_1 第 1 场" in redo_prompt
    assert [s.id for s in script.scenes] == [f"scene_{i}" for i in range(1, 6)]
    # 第二章的剧本原样保留（只是编号顺延）
    assert [s.summary for s in script.scenes[3:]] == ch2
    outline = store.load_outline(project.id)
    assert [s.chapter_id for s in outline.scenes] == ["ch_1"] * 3 + ["ch_2"] * 2
    # 只重做时不会顺手展开别的章
    assert outline.chapter_ids() == {"ch_1", "ch_2"}


# ── 单句重配 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_redub_one_line_resynthesizes_only_that_clip(
    audio_ready_project, media_pipeline, store, fake_tts
):
    project_id = audio_ready_project.id
    manifest = store.load_audio_manifest(project_id)
    target = manifest.clips[0].shot_id
    fake_tts.calls.clear()

    job = await media_pipeline.redub_lines(project_id, [target], instruction="压低声音，冷笑")
    await wait_for_job(media_pipeline, job.id)

    assert len(fake_tts.calls) == 1
    assert fake_tts.requests[-1].instruction == "压低声音，冷笑"
    shot = next(s for s in store.load_storyboard(project_id).shots if s.id == target)
    assert shot.voice_direction.instruction == "压低声音，冷笑"
    after = store.load_audio_manifest(project_id)
    assert all(c.status == ClipStatus.READY for c in after.clips)


@pytest.mark.asyncio
async def test_redub_reopens_a_finished_film(audio_ready_project, media_pipeline, store):
    project = store.load_project(audio_ready_project.id)
    project.status = ProjectStatus.DONE
    store.save_project(project)
    shot = store.load_audio_manifest(project.id).clips[0].shot_id
    job = await media_pipeline.redub_lines(project.id, [shot])
    await wait_for_job(media_pipeline, job.id)
    assert store.load_project(project.id).status == ProjectStatus.AUDIO_READY


@pytest.mark.asyncio
async def test_redub_rejects_native_projects_and_unknown_shots(
    audio_ready_project, media_pipeline, store
):
    with pytest.raises(PipelineError):
        await media_pipeline.redub_lines(audio_ready_project.id, ["nope"])
    project = store.load_project(audio_ready_project.id)
    project.dub_mode = "native"
    store.save_project(project)
    with pytest.raises(PipelineError):
        await media_pipeline.redub_lines(project.id, ["scene_1_shot_1"])
