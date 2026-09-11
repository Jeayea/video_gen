from __future__ import annotations

import json

import pytest

from tests.fakes import FakeLLM
from videogen.models.project import Project, ProjectStatus
from videogen.models.world_state import WorldState
from videogen.orchestrator.pipeline import PipelineError
from videogen.utils.file_store import FileStore


def stage_calls(llm: FakeLLM, marker: str) -> int:
    return sum(1 for messages in llm.calls if marker in messages[0].content)


def make_project(store: FileStore, **kwargs) -> Project:
    project = Project(input_text="一个被裁员的程序员靠 AI 逆袭创业", **kwargs)
    store.save_project(project)
    return project


# ── 全流程 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_text_pipeline(pipeline, store, fake_llm):
    project = make_project(store, target_duration_sec=45)

    bible = await pipeline.generate_bible(project.id)
    assert len(bible.characters) == 2
    assert bible.characters[0].age == 24  # "24岁" 脏数据被容错
    assert store.load_project(project.id).status == ProjectStatus.DRAFT

    script = await pipeline.generate_script(project.id)
    assert store.load_project(project.id).status == ProjectStatus.SCRIPT_REVIEW
    assert len(script.scenes) == 5

    # P0-2 回归：目标 45s，假 LLM 每场都返回 45s，必须被归一化回 45s
    assert script.total_duration_sec == 45
    assert script.duration_normalized is True

    # 舞台提示被清洗
    for scene in script.scenes:
        for line in scene.dialogue:
            assert "（" not in line.line and "）" not in line.line
            assert line.line.strip()

    # 逐场落盘 + 大纲落盘
    assert store.load_script(project.id) is not None
    assert store.load_outline(project.id) is not None
    assert store.load_world_state(project.id).current_scene == "scene_5"

    await pipeline.confirm_script(project.id)
    assert store.load_project(project.id).status == ProjectStatus.SCRIPT_CONFIRMED

    storyboard = await pipeline.generate_storyboard(project.id)
    assert store.load_project(project.id).status == ProjectStatus.STORYBOARD_REVIEW
    assert len(storyboard.shots) == 10

    # 每场镜头时长之和 == 该场剧本时长
    for scene in script.scenes:
        shots = storyboard.shots_for_scene(scene.id)
        assert sum(s.duration_sec for s in shots) == scene.duration_sec

    # video_prompt 全英文、无内部 id
    for shot in storyboard.shots:
        assert shot.video_prompt
        assert shot.video_prompt.isascii()
        assert "char_" not in shot.video_prompt

    await pipeline.confirm_storyboard(project.id)
    # P0-1 回归：确认分镜后是 storyboard_confirmed，不能虚报 video_ready
    assert store.load_project(project.id).status == ProjectStatus.STORYBOARD_CONFIRMED

    report = pipeline.get_report(project.id)
    assert report is not None
    assert report.stage == "script+storyboard"


# ── 状态机门控 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_then_regenerate_script_is_blocked(pipeline, store):
    """P0-5 回归：确认剧本后不允许悄悄重新生成。"""
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)

    with pytest.raises(PipelineError) as exc:
        await pipeline.generate_script(project.id)
    assert "不允许" in str(exc.value)
    assert store.load_project(project.id).status == ProjectStatus.SCRIPT_CONFIRMED


@pytest.mark.asyncio
async def test_reopen_script_allows_regeneration(pipeline, store, fake_llm):
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)

    await pipeline.reopen_script(project.id)
    assert store.load_project(project.id).status == ProjectStatus.DRAFT

    before = stage_calls(fake_llm, "大纲策划")
    await pipeline.generate_script(project.id)
    assert stage_calls(fake_llm, "大纲策划") == before + 1  # 退回后是全新生成
    assert store.load_project(project.id).status == ProjectStatus.SCRIPT_REVIEW


@pytest.mark.asyncio
async def test_storyboard_requires_confirmed_script(pipeline, store):
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)  # SCRIPT_REVIEW，尚未确认

    with pytest.raises(PipelineError):
        await pipeline.generate_storyboard(project.id)


@pytest.mark.asyncio
async def test_edit_guards_follow_status(pipeline, store):
    """P0-6 回归：已确认的剧本不能被 PUT 悄悄覆盖。"""
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    script = await pipeline.generate_script(project.id)

    # script_review 阶段允许编辑
    payload = json.loads(script.model_dump_json())
    payload["scenes"] = payload["scenes"][:2]
    updated = await pipeline.update_script(project.id, payload)
    assert len(updated.scenes) == 2

    await pipeline.confirm_script(project.id)
    with pytest.raises(PipelineError):
        await pipeline.update_script(project.id, payload)

    # 圣经在剧本确认后也应锁住
    with pytest.raises(PipelineError):
        await pipeline.update_bible(project.id, {"characters": [], "settings": [], "facts": []})


@pytest.mark.asyncio
async def test_reopen_storyboard(pipeline, store):
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)
    await pipeline.generate_storyboard(project.id)
    await pipeline.confirm_storyboard(project.id)

    await pipeline.reopen_storyboard(project.id)
    assert store.load_project(project.id).status == ProjectStatus.SCRIPT_CONFIRMED

    # 退回后可以重新生成分镜
    await pipeline.generate_storyboard(project.id)
    assert store.load_project(project.id).status == ProjectStatus.STORYBOARD_REVIEW


@pytest.mark.asyncio
async def test_confirm_requires_review_status(pipeline, store):
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    with pytest.raises(PipelineError):
        await pipeline.confirm_script(project.id)


# ── 断点续跑 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_script_keeps_progress_and_resumes(store, make_pipeline):
    llm = FakeLLM(fail_at="scene:scene_3")
    pipeline = make_pipeline(llm)
    project = make_project(store)

    await pipeline.generate_bible(project.id)
    with pytest.raises(PipelineError):
        await pipeline.generate_script(project.id)

    # 已生成的 2 场被保留下来，而不是整份丢弃
    assert store.load_project(project.id).status == ProjectStatus.FAILED
    partial = store.load_script(project.id)
    assert partial is not None
    assert len(partial.scenes) == 2

    outline_calls_before = stage_calls(llm, "大纲策划")
    scene_calls_before = stage_calls(llm, "逐场编写短剧剧本")

    # 续跑
    llm.fail_at = None
    script = await pipeline.generate_script(project.id)
    assert len(script.scenes) == 5
    assert store.load_project(project.id).status == ProjectStatus.SCRIPT_REVIEW
    # 大纲不重生成；已完成的 2 场不重生成（只补 3 场）
    assert stage_calls(llm, "大纲策划") == outline_calls_before
    assert stage_calls(llm, "逐场编写短剧剧本") == scene_calls_before + 3


@pytest.mark.asyncio
async def test_failed_storyboard_resumes(store, make_pipeline):
    llm = FakeLLM(fail_at="storyboard:scene_2")
    pipeline = make_pipeline(llm)
    project = make_project(store)

    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)

    with pytest.raises(PipelineError):
        await pipeline.generate_storyboard(project.id)
    assert store.load_project(project.id).status == ProjectStatus.FAILED
    assert len(store.load_storyboard(project.id).shots) == 2  # scene_1 已落盘

    llm.fail_at = None
    storyboard = await pipeline.generate_storyboard(project.id)
    assert len(storyboard.shots) == 10
    assert store.load_project(project.id).status == ProjectStatus.STORYBOARD_REVIEW


@pytest.mark.asyncio
async def test_incomplete_script_blocks_storyboard(store, make_pipeline):
    llm = FakeLLM(fail_at="scene:scene_5")
    pipeline = make_pipeline(llm)
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    with pytest.raises(PipelineError):
        await pipeline.generate_script(project.id)

    # 状态是 FAILED，分镜生成会尝试，但剧本不完整必须被拦下
    llm.fail_at = None
    with pytest.raises(PipelineError) as exc:
        await pipeline.generate_storyboard(project.id)
    assert "不完整" in str(exc.value)


# ── 一致性校验 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_storyboard_consistency_flags_speaker_not_in_frame(pipeline, store):
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    script = await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)
    storyboard = await pipeline.generate_storyboard(project.id)

    shots = json.loads(storyboard.model_dump_json())["shots"]
    shots[0]["character_in_frame"] = []
    shots[0]["dialogue_speaker"] = "char_1"
    shots[1]["video_prompt"] = "中文提示词"
    await pipeline.update_storyboard(project.id, {"shots": shots})

    report = pipeline.get_report(project.id)
    messages = " ".join(i.message for i in report.issues)
    assert "不在该镜头的画面角色里" in messages
    assert "非英文字符" in messages
    assert len(script.scenes) == 5


@pytest.mark.asyncio
async def test_bible_edit_recomputes_report(pipeline, store):
    project = make_project(store)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)

    await pipeline.update_bible(
        project.id,
        {
            "characters": [{"id": "char_1", "name": "只有主角"}],
            "settings": [],
            "facts": [],
            "style": {},
        },
    )
    report = pipeline.get_report(project.id)
    # char_2 已从圣经移除，剧本里的对白说话人应该报错
    assert report.has_errors


# ── 旧数据迁移 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_status_migration(store, tmp_path):
    project = make_project(store)
    meta_path = store.project_dir(project.id) / "meta.json"
    raw = json.loads(meta_path.read_text(encoding="utf-8"))

    raw["status"] = "script_generated"
    meta_path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load_project(project.id).status == ProjectStatus.SCRIPT_CONFIRMED

    raw["status"] = "video_ready"
    meta_path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load_project(project.id).status == ProjectStatus.STORYBOARD_CONFIRMED


def test_invalid_project_id_is_rejected(store):
    assert store.load_project("..") is None
    assert store.load_project("a/b") is None
    assert store.is_valid_id("abc123") is True
    assert store.load_project("abc123") is None


def test_atomic_write_leaves_no_tmp_files(store):
    project = make_project(store)
    store.save_world_state(project.id, WorldState(current_scene="scene_1"))
    leftovers = [p.name for p in store.project_dir(project.id).iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert store.load_world_state(project.id).current_scene == "scene_1"


# ── 媒体阶段门控（配音 / 合成 / 回退）─────────────────


def force_status(store: FileStore, project_id: str, value: str) -> None:
    """直接改 meta.json 的状态，用来构造「已经出片」这类终态。"""
    path = store.project_dir(project_id) / "meta.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = value
    path.write_text(json.dumps(raw), encoding="utf-8")


@pytest.mark.asyncio
async def test_audio_needs_video_and_compose_needs_audio(store, media_pipeline, ready_project):
    with pytest.raises(PipelineError):
        await media_pipeline.start_audio_generation(ready_project.id)
    with pytest.raises(PipelineError):
        await media_pipeline.start_compose(ready_project.id)


@pytest.mark.asyncio
async def test_reopen_storyboard_from_video_ready(store, media_pipeline, video_ready_project):
    # 已经生成过视频也要能退回分镜重做（否则只能删项目重来）
    await media_pipeline.reopen_storyboard(video_ready_project.id)
    assert store.load_project(video_ready_project.id).status == ProjectStatus.SCRIPT_CONFIRMED
    # 只是回退状态，产物要等「重新生成」才作废
    assert store.load_video_manifest(video_ready_project.id) is not None


@pytest.mark.asyncio
async def test_reopen_script_from_audio_ready(store, media_pipeline, audio_ready_project):
    await media_pipeline.reopen_script(audio_ready_project.id)
    assert store.load_project(audio_ready_project.id).status == ProjectStatus.DRAFT


@pytest.mark.asyncio
async def test_reopen_from_done(store, media_pipeline, video_ready_project):
    force_status(store, video_ready_project.id, ProjectStatus.DONE.value)

    await media_pipeline.reopen_storyboard(video_ready_project.id)
    assert store.load_project(video_ready_project.id).status == ProjectStatus.SCRIPT_CONFIRMED

    force_status(store, video_ready_project.id, ProjectStatus.DONE.value)
    await media_pipeline.reopen_script(video_ready_project.id)
    assert store.load_project(video_ready_project.id).status == ProjectStatus.DRAFT


@pytest.mark.asyncio
async def test_video_rerun_after_audio_ready_keeps_audio(
    store, media_pipeline, audio_ready_project
):
    job = await media_pipeline.start_video_generation(audio_ready_project.id)  # 允许补跑镜头
    assert store.load_project(audio_ready_project.id).status == ProjectStatus.VIDEO_GENERATING

    from tests.conftest import wait_for_job

    finished = await wait_for_job(media_pipeline, job.id)
    assert finished.status.value == "succeeded"
    # 配音与文本无关，补跑镜头不该把配音清掉
    assert store.load_audio_manifest(audio_ready_project.id) is not None
    assert store.load_project(audio_ready_project.id).status == ProjectStatus.AUDIO_READY


@pytest.mark.asyncio
async def test_regenerating_bible_after_reopen_invalidates_media(
    store, media_pipeline, audio_ready_project
):
    await media_pipeline.reopen_script(audio_ready_project.id)
    await media_pipeline.generate_bible(audio_ready_project.id)

    assert store.load_video_manifest(audio_ready_project.id) is None
    assert store.load_audio_manifest(audio_ready_project.id) is None
    assert not (store.project_dir(audio_ready_project.id) / "clips").exists()
    assert not (store.project_dir(audio_ready_project.id) / "audio").exists()


@pytest.mark.asyncio
async def test_editing_storyboard_invalidates_media(store, media_pipeline, video_ready_project):
    await media_pipeline.reopen_storyboard(video_ready_project.id)
    storyboard = store.load_storyboard(video_ready_project.id)
    data = storyboard.model_dump()
    data["shots"][0]["video_prompt"] = "close up, a different shot"

    await media_pipeline.update_storyboard(video_ready_project.id, data)
    assert store.load_video_manifest(video_ready_project.id) is None
