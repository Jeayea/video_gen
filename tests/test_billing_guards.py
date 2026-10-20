"""花钱入口的护栏：预算门不能被绕过、同一批片段不能被买两遍、账不能记两遍。

每条都对应一个审查里复现过的问题（2026-10-20）：
- 问价失败时预算门按 $0 放行
- 批量链路直接调 `_run_*_job`，预算门和任务互斥都被绕过
- 视频任务在跑时「补齐漏记」会重复记账
- 删项目不取消任务，付费请求照发、目录被重建
- 配音任务在跑时「重配这几句」的修改被覆盖
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import wait_for_job
from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.project import Project, ProjectStatus
from videogen.orchestrator.batch import BatchManager
from videogen.orchestrator.jobs import JobCancelled, JobManager
from videogen.orchestrator.pipeline import (
    AUDIO_JOB_KIND,
    Pipeline,
    PipelineError,
)
from videogen.services.cost_service import BudgetExceeded


def build(store, video=None) -> Pipeline:
    return Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: video or FakeVideoProvider(),
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )


class PriceLookupFails(FakeVideoProvider):
    """付费模型、问价接口挂了（真实场景：/videos/models 超时）。"""

    paid = True

    async def price_per_second(self, *, resolution: str = "") -> float | None:
        raise RuntimeError("价目表取不到")


async def _wait_status(store, project_id: str, status: ProjectStatus, timeout: float = 10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if store.load_project(project_id).status == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"项目没有进入 {status.value}")


# ── 预算门 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_price_lookup_failure_is_refused_when_budget_is_set(
    store, ready_project, monkeypatch
):
    """问价失败 + 配置单价 0 + 设了预算：拒绝开工，而不是按 $0 放行。"""
    monkeypatch.setattr(get_settings(), "budget_usd", 5.0)
    video = PriceLookupFails()
    pipeline = build(store, video)

    with pytest.raises(BudgetExceeded):
        await pipeline.start_video_generation(ready_project.id)
    assert video.calls == []
    assert store.load_project(ready_project.id).status == ProjectStatus.STORYBOARD_CONFIRMED


@pytest.mark.asyncio
async def test_price_lookup_failure_falls_back_to_configured_price(
    store, ready_project, monkeypatch
):
    """问价失败但配置了单价：按配置单价 × 分镜秒数估，超预算照样拦。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "budget_usd", 0.5)
    monkeypatch.setattr(settings, "video_price_per_second", 1.0)
    video = PriceLookupFails()
    pipeline = build(store, video)

    with pytest.raises(BudgetExceeded):
        await pipeline.start_video_generation(ready_project.id)
    assert video.calls == []


@pytest.mark.asyncio
async def test_batch_chain_goes_through_the_video_budget_gate(store, monkeypatch):
    """批量链路也要过视频预算门（以前直接调 `_run_video_job`，预算设了等于没设）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "budget_usd", 0.5)
    monkeypatch.setattr(settings, "video_price_per_second", 1.0)
    video = FakeVideoProvider()
    pipeline = build(store, video)
    project = Project(title="批量预算", input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)

    with pytest.raises(BudgetExceeded):
        await pipeline.run_chain(project.id, stop_after="video")
    assert video.calls == []
    assert store.load_project(project.id).status == ProjectStatus.STORYBOARD_CONFIRMED


# ── 任务互斥 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_video_stage_blocks_a_manual_second_run(store):
    """批量跑视频时，手动再点「生成视频」要被拒绝（以前每个镜头会被买两遍）。"""
    video = FakeVideoProvider(delay=0.2)
    pipeline = build(store, video)
    manager = BatchManager()
    batch = manager.submit(pipeline, [{"input_text": "一个逆袭故事"}], stop_after="video")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not batch.items[0].project_id and loop.time() < deadline:
        await asyncio.sleep(0.01)
    project_id = batch.items[0].project_id
    await _wait_status(store, project_id, ProjectStatus.VIDEO_GENERATING)

    with pytest.raises(PipelineError, match="已有视频生成任务"):
        await pipeline.start_video_generation(project_id)

    while batch.is_active and loop.time() < deadline + 20:
        await asyncio.sleep(0.01)
    assert batch.items[0].status.value == "succeeded"
    assert video.calls and len(video.calls) == len(set(video.calls))  # 每个单元只买一次


@pytest.mark.asyncio
async def test_reconcile_is_refused_while_video_is_generating(store, ready_project):
    video = FakeVideoProvider(delay=0.3, actual_cost_usd=1.3)
    pipeline = build(store, video)
    job = await pipeline.start_video_generation(ready_project.id)
    while not video.calls:
        await asyncio.sleep(0.01)

    # 生成期间不提示「漏记」，也不许补记
    assert pipeline.cost_status(ready_project.id)["video_unrecorded_usd"] == 0
    assert pipeline.cost_status(ready_project.id)["video_job_running"] is True
    with pytest.raises(PipelineError, match="视频生成"):
        await pipeline.reconcile_video_cost(ready_project.id)

    await wait_for_job(pipeline, job.id)
    ledger = pipeline.cost_status(ready_project.id)["video_ledger_usd"]
    actual = pipeline.cost_status(ready_project.id)["video_actual_usd"]
    assert ledger == pytest.approx(actual)
    # 任务结束后补记是幂等的
    assert (await pipeline.reconcile_video_cost(ready_project.id))["recorded_usd"] == 0


@pytest.mark.asyncio
async def test_redub_is_refused_while_audio_job_runs(store, ready_project):
    pipeline = build(store)
    gate = asyncio.Event()

    async def slow(_ctx):
        await gate.wait()

    job = pipeline.jobs.submit(ready_project.id, AUDIO_JOB_KIND, slow)
    await asyncio.sleep(0)
    with pytest.raises(PipelineError, match="配音"):
        await pipeline.redub_lines(ready_project.id, ["scene_1_shot_1"])
    gate.set()
    await pipeline.jobs.wait(job.id)


@pytest.mark.asyncio
async def test_delete_project_cancels_its_jobs_first(store, ready_project):
    """删项目要先停掉视频任务：否则付费请求照发，落盘还会把目录重建出来。"""
    video = FakeVideoProvider(delay=0.3)
    pipeline = build(store, video)
    job = await pipeline.start_video_generation(ready_project.id)
    while not video.calls:
        await asyncio.sleep(0.01)

    assert await pipeline.delete_project(ready_project.id)
    calls = len(video.calls)
    await asyncio.sleep(0.5)

    assert pipeline.jobs.get(job.id).status.value == "cancelled"
    assert len(video.calls) == calls  # 删完不再提交
    assert not store.project_dir(ready_project.id).exists()


# ── JobManager.wait ───────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_reraises_the_original_exception():
    jobs = JobManager()

    async def boom(_ctx):
        raise BudgetExceeded("超预算")

    job = jobs.submit("p", "video", boom)
    with pytest.raises(BudgetExceeded, match="超预算"):
        await jobs.wait(job.id)


@pytest.mark.asyncio
async def test_wait_reports_a_cancelled_job():
    jobs = JobManager()

    async def slow(_ctx):
        await asyncio.sleep(10)

    job = jobs.submit("p", "video", slow)
    await asyncio.sleep(0)
    await jobs.cancel(job.id)
    with pytest.raises(JobCancelled):
        await jobs.wait(job.id)


@pytest.mark.asyncio
async def test_cancelling_the_waiter_cancels_the_job_and_propagates():
    """批量被取消：等待方要收到 CancelledError（任务自己的取消路径会吞掉它）。"""
    jobs = JobManager()

    async def slow(_ctx):
        await asyncio.sleep(10)

    job = jobs.submit("p", "video", slow)
    waiter = asyncio.create_task(jobs.wait(job.id))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert jobs.get(job.id).status.value == "cancelled"


@pytest.mark.asyncio
async def test_audio_job_error_does_not_leave_project_stuck(store, monkeypatch):
    """配音任务里冒出普通异常：状态要退回可重试的位置（以前卡在 AUDIO_GENERATING）。"""
    from tests.conftest import build_confirmed_project
    from videogen.orchestrator import pipeline as pipeline_module

    pipeline = build(store)
    project = await build_confirmed_project(store, pipeline)
    await wait_for_job(pipeline, (await pipeline.start_video_generation(project.id)).id)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("磁盘满了")

    original = pipeline_module.audio_service.synthesize_all
    monkeypatch.setattr(pipeline_module.audio_service, "synthesize_all", boom)
    job = await pipeline.start_audio_generation(project.id)
    finished = await wait_for_job(pipeline, job.id)
    assert finished.status.value == "failed" and "磁盘满了" in finished.error
    assert store.load_project(project.id).status == ProjectStatus.VIDEO_READY

    # 只还原这一处（undo() 会连 conftest 的安全基线一起撤掉）
    monkeypatch.setattr(pipeline_module.audio_service, "synthesize_all", original)
    job = await pipeline.start_audio_generation(project.id)  # 能重试
    assert (await wait_for_job(pipeline, job.id)).status.value == "succeeded"


def test_unreported_units_are_estimated_not_booked_at_zero(store):
    """一批里有的片段报了实际扣费、有的没报：没报的按单价估（以前整批按实报合计，没报的记 $0）。"""
    from videogen.models.video import ClipStatus, VideoClip, VideoManifest

    pipeline = build(store)
    project = Project(title="混合记账")
    store.save_project(project)
    manifest = VideoManifest(
        provider="openrouter",
        model="m",
        clips=[
            # 一段两镜：扣费只记在第一个镜头上，第二个是 0 —— 这是「有实报」
            VideoClip(shot_id="a1", segment_id="seg_a", status=ClipStatus.READY,
                      duration_sec=3, cost_usd=1.3),
            VideoClip(shot_id="a2", segment_id="seg_a", status=ClipStatus.READY, duration_sec=3),
            # 单镜、provider 没报扣费
            VideoClip(shot_id="b1", status=ClipStatus.READY, duration_sec=5),
        ],
    )
    recorded = pipeline._record_video_cost(
        project.id, manifest, price_per_second=0.1, settings=get_settings()
    )
    assert recorded == pytest.approx(1.3 + 5 * 0.1)
    assert store.load_cost_report(project.id).total_usd == pytest.approx(1.8)


@pytest.mark.asyncio
async def test_bible_item_rewrite_keeps_references_registered_meanwhile(store, monkeypatch):
    """重写一个圣经条目要等 LLM 几秒：这期间别的角色刚登记的定妆照不能被整份写回抹掉。"""
    from videogen.orchestrator import pipeline as pipeline_module
    from videogen.services import character_image_service

    pipeline = build(store)
    project = Project(title="参考图别丢", input_text="一个逆袭故事")
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    bible = store.load_bible(project.id)
    target, other = bible.characters[0], bible.characters[1]
    original = pipeline_module.bible_service.regenerate_item

    async def slow_rewrite(*args, **kwargs):
        # LLM 还在想的时候，定妆照生成完、登记进了圣经
        await character_image_service._write_reference(
            store, project.id, other.id, f"refs/{other.id}.png"
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(pipeline_module.bible_service, "regenerate_item", slow_rewrite)
    await pipeline.regenerate_bible_item(project.id, "characters", target.id, feedback="更凶一点")

    saved = {c.id: c for c in store.load_bible(project.id).characters}
    assert saved[other.id].reference_image == f"refs/{other.id}.png"
