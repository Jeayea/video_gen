from __future__ import annotations

import asyncio

import pytest

from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen.models.project import AspectRatio, ProjectStatus
from videogen.orchestrator.batch import BatchManager, BatchStatus, ItemStatus
from videogen.orchestrator.pipeline import Pipeline


class SelectiveLLM(FakeLLM):
    """遇到带「炸弹」字样的输入就失败：用来验证批量里的部分失败。"""

    def _respond(self, messages):
        if "炸弹" in messages[-1].content and "资深短剧策划" in messages[0].content:
            from videogen.providers.llm.base import LLMError

            raise LLMError("故意失败的项目")
        return super()._respond(messages)


def build(store, llm=None, *, video=None) -> Pipeline:
    return Pipeline(
        store,
        llm_factory=lambda: llm or FakeLLM(),
        video_factory=lambda: video or FakeVideoProvider(),
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )


async def wait_batch(manager: BatchManager, batch_id: str, timeout: float = 30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        batch = manager.get(batch_id)
        if batch and not batch.is_active:
            return batch
        await asyncio.sleep(0.01)
    raise AssertionError(f"批量任务 {batch_id} 未在 {timeout}s 内结束")


def test_submit_validates_input(store):
    manager = BatchManager()
    pipeline = build(store)

    with pytest.raises(ValueError):
        manager.submit(pipeline, [{"input_text": "   "}])
    with pytest.raises(ValueError):
        manager.submit(pipeline, [{"input_text": "x"}], stop_after="乱填")
    with pytest.raises(ValueError):
        manager.submit(
            pipeline, [{"input_text": f"主题 {i}"} for i in range(3)], max_items=2
        )


@pytest.mark.asyncio
async def test_submit_defaults(store):
    manager = BatchManager()
    batch = manager.submit(
        build(store),
        [{"input_text": "逆袭故事", "title": "第一条"}, {"input_text": "复仇故事"}],
        target_duration_sec=30,
        aspect_ratio=AspectRatio.SQUARE,
        stop_after="video",
    )
    assert batch.status == BatchStatus.QUEUED
    assert [i.title for i in batch.items] == ["第一条", "批量项目 2"]
    assert batch.aspect_ratio == AspectRatio.SQUARE
    assert batch.stop_after == "video"
    assert batch.summary()["pending"] == 2
    assert manager.active_count == 1

    await manager.shutdown()


@pytest.mark.asyncio
async def test_batch_runs_storyboard_only(store):
    """stop_after=storyboard：批量只要剧本 + 分镜，不碰视频。"""
    manager = BatchManager()
    pipeline = build(store)
    batch = manager.submit(
        pipeline,
        [{"input_text": f"主题 {i}"} for i in range(3)],
        stop_after="storyboard",
        target_duration_sec=30,
    )

    finished = await wait_batch(manager, batch.id)
    assert finished.status == BatchStatus.SUCCEEDED
    assert finished.percent == 100
    assert finished.summary()["succeeded"] == 3
    assert finished.message.startswith("完成")

    for item in finished.items:
        project = store.load_project(item.project_id)
        assert project is not None
        assert project.status == ProjectStatus.STORYBOARD_CONFIRMED
        assert store.load_storyboard(item.project_id).shots
        assert store.load_video_manifest(item.project_id) is None


@pytest.mark.asyncio
async def test_batch_runs_video_stage(store):
    manager = BatchManager()
    pipeline = build(store)
    batch = manager.submit(
        pipeline, [{"input_text": "逆袭", "title": "A"}, {"input_text": "复仇", "title": "B"}],
        stop_after="video",
        target_duration_sec=30,
    )

    finished = await wait_batch(manager, batch.id)
    assert finished.status == BatchStatus.SUCCEEDED
    for item in finished.items:
        assert store.load_project(item.project_id).status == ProjectStatus.VIDEO_READY
        manifest = store.load_video_manifest(item.project_id)
        assert manifest.ready_clips


@pytest.mark.asyncio
async def test_batch_partial_failure(store):
    manager = BatchManager()
    pipeline = build(store, SelectiveLLM())
    batch = manager.submit(
        pipeline,
        [{"input_text": "正常主题"}, {"input_text": "炸弹主题"}, {"input_text": "另一个正常"}],
        stop_after="storyboard",
    )

    finished = await wait_batch(manager, batch.id)
    assert finished.status == BatchStatus.PARTIAL
    assert finished.summary()["succeeded"] == 2
    assert finished.summary()["failed"] == 1

    failed = [i for i in finished.items if i.status == ItemStatus.FAILED]
    assert len(failed) == 1
    assert "故意失败的项目" in failed[0].error
    assert failed[0].project_id  # 项目还是建出来了，方便人工接管


@pytest.mark.asyncio
async def test_batch_cancel(store):
    manager = BatchManager()
    pipeline = build(store, video=FakeVideoProvider(delay=0.2))
    batch = manager.submit(
        pipeline,
        [{"input_text": f"主题 {i}"} for i in range(4)],
        stop_after="video",
    )
    await asyncio.sleep(0.05)

    assert await manager.cancel(batch.id) is True
    finished = await wait_batch(manager, batch.id)
    assert finished.status == BatchStatus.CANCELLED
    assert finished.summary()["cancelled"] >= 1
    # 重复取消是 no-op
    assert await manager.cancel(batch.id) is False


@pytest.mark.asyncio
async def test_batch_shutdown_cancels_running(store):
    manager = BatchManager()
    batch = manager.submit(
        build(store, video=FakeVideoProvider(delay=0.3)),
        [{"input_text": "长任务"}],
        stop_after="video",
    )
    await asyncio.sleep(0.05)
    await manager.shutdown()
    finished = await wait_batch(manager, batch.id)
    assert finished.status == BatchStatus.CANCELLED
    assert finished.finished_at is not None


@pytest.mark.asyncio
async def test_batch_history_is_pruned(store):
    manager = BatchManager(max_history=2)
    for index in range(3):
        batch = manager.submit(
            build(store), [{"input_text": f"主题 {index}"}], stop_after="storyboard"
        )
        await wait_batch(manager, batch.id)
    assert len(manager.list(limit=10)) == 2


@pytest.mark.asyncio
async def test_run_chain_from_scratch(store):
    """run_chain 是批量执行体：一条命令从文本跑到成片（这里停在视频）。"""
    pipeline = build(store)
    from videogen.models.project import Project

    project = Project(title="链路", input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)

    stages: list[str] = []
    result = await pipeline.run_chain(
        project.id, stop_after="video", on_stage=stages.append
    )

    assert stages == ["bible", "script", "storyboard", "video"]
    assert result.status == ProjectStatus.VIDEO_READY
    assert store.load_script(project.id).scenes
    assert store.load_video_manifest(project.id).ready_clips
    # 成本账本也要有记录
    report = store.load_cost_report(project.id)
    assert {e.stage.value for e in report.entries} >= {"bible", "script", "storyboard", "video"}
