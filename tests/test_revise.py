"""按修改意见重写：单场剧本（直接保存）/ 整场分镜或单个镜头（只出提案，确认后再 PUT）。"""

from __future__ import annotations

import pytest

from tests.conftest import build_confirmed_project
from videogen.models.project import Project, ProjectStatus
from videogen.models.video import ClipStatus
from videogen.orchestrator.pipeline import PipelineError


def _last_user_prompt(llm) -> str:
    return llm.calls[-1][-1].content


@pytest.mark.asyncio
async def test_regenerate_scene_sends_feedback_and_keeps_other_scenes(pipeline, store, fake_llm):
    project = Project(input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    before = store.load_script(project.id)
    target = before.scenes[1]

    original = fake_llm._scene

    def revised(scene_id: str) -> dict:
        data = original(scene_id)
        data["summary"] = "重写后的概述"
        data["duration_sec"] = 99  # 模型给的时长不作数，沿用这一场原来的时长
        return data

    fake_llm._scene = revised
    scene = await pipeline.regenerate_scene(project.id, target.id, feedback="让赵明远更阴险")

    prompt = _last_user_prompt(fake_llm)
    assert "修改意见" in prompt and "让赵明远更阴险" in prompt
    assert target.summary in prompt  # 当前版本给了模型
    after = store.load_script(project.id)
    assert scene.summary == "重写后的概述"
    assert after.scenes[1].summary == "重写后的概述"
    assert after.scenes[1].duration_sec == target.duration_sec
    assert [s.model_dump() for i, s in enumerate(after.scenes) if i != 1] == [
        s.model_dump() for i, s in enumerate(before.scenes) if i != 1
    ]


@pytest.mark.asyncio
async def test_regenerate_scene_requires_script_stage(ready_project, pipeline):
    with pytest.raises(PipelineError):
        await pipeline.regenerate_scene(ready_project.id, "scene_1", feedback="x")


@pytest.mark.asyncio
async def test_revise_single_shot_is_a_proposal_that_keeps_ids(
    video_ready_project, video_pipeline, store, fake_llm
):
    project_id = video_ready_project.id
    board = store.load_storyboard(project_id)
    target = board.shots[0]

    original = fake_llm._shots

    def revised(scene_id: str) -> dict:
        data = original(scene_id)
        data["shots"][0]["visual_description"] = "改过的画面"
        data["shots"][0]["duration_sec"] = 9
        return data

    fake_llm._shots = revised
    result = await video_pipeline.propose_storyboard_revision(
        project_id, shot_id=target.id, feedback="换成俯拍"
    )

    prompt = _last_user_prompt(fake_llm)
    assert f"只重写镜头 {target.id}" in prompt and "换成俯拍" in prompt
    assert result["changed_shots"] == [target.id]
    shots = result["storyboard"]["shots"]
    assert [s["id"] for s in shots] == [s.id for s in board.shots]
    fresh = shots[0]
    assert fresh["visual_description"] == "改过的画面"
    assert fresh["duration_sec"] == target.duration_sec
    # 其余镜头一个字都没动
    assert shots[1:] == [s.model_dump(mode="json") for s in board.shots[1:]]
    # 只是提案：分镜和片段都还在
    saved = store.load_storyboard(project_id).shots[0]
    assert saved.visual_description == target.visual_description
    video = store.load_video_manifest(project_id)
    assert all(c.status == ClipStatus.READY for c in video.clips)
    assert result["impact"]["stale_shots"]
    assert target.id in result["impact"]["stale_shots"]

    # 确认后保存：只作废这个镜头所在的段
    await video_pipeline.update_storyboard(project_id, result["storyboard"])
    stale = set(result["impact"]["stale_shots"])
    video = store.load_video_manifest(project_id)
    for clip in video.clips:
        expected = ClipStatus.PENDING if clip.shot_id in stale else ClipStatus.READY
        assert clip.status == expected


@pytest.mark.asyncio
async def test_revise_whole_scene_replaces_only_that_scene(store, pipeline, fake_llm):
    project = await build_confirmed_project(store, pipeline)
    board = store.load_storyboard(project.id)
    scene_id = board.shots[-1].scene_id
    scene_total = sum(s.duration_sec for s in board.shots_for_scene(scene_id))

    result = await pipeline.propose_storyboard_revision(
        project.id, scene_id=scene_id, feedback="节奏更快"
    )
    assert "重写本场的全部镜头" in _last_user_prompt(fake_llm)
    shots = result["storyboard"]["shots"]
    others = [s for s in shots if s["scene_id"] != scene_id]
    assert others == [s.model_dump(mode="json") for s in board.shots if s.scene_id != scene_id]
    mine = [s for s in shots if s["scene_id"] == scene_id]
    assert sum(s["duration_sec"] for s in mine) == scene_total
    assert set(result["changed_shots"]) == {s["id"] for s in mine}
    assert store.load_project(project.id).status == ProjectStatus.STORYBOARD_CONFIRMED


@pytest.mark.asyncio
async def test_revise_unknown_shot_is_rejected(ready_project, pipeline):
    with pytest.raises(PipelineError):
        await pipeline.propose_storyboard_revision(ready_project.id, shot_id="nope")
