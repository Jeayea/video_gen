from __future__ import annotations

import asyncio

import pytest

from videogen.orchestrator.jobs import JobContext, JobManager, JobStatus


async def wait_done(manager: JobManager, job_id: str, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = manager.get(job_id)
        if job and not job.is_active:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务 {job_id} 未结束")


@pytest.mark.asyncio
async def test_job_runs_and_reports_progress():
    manager = JobManager()

    async def runner(ctx: JobContext) -> None:
        ctx.set_progress(0, 3, "开始")
        for i in range(1, 4):
            await asyncio.sleep(0)
            ctx.set_progress(i, 3, f"第 {i} 步")
        ctx.note("收尾")

    job = manager.submit("proj-1", "video", runner)
    assert job.status == JobStatus.QUEUED

    done = await wait_done(manager, job.id)
    assert done.status == JobStatus.SUCCEEDED
    assert (done.progress, done.total, done.percent) == (3, 3, 100)
    assert done.message == "收尾"
    assert done.started_at and done.finished_at
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_failed_job_records_scrubbed_error():
    manager = JobManager()

    async def runner(ctx: JobContext) -> None:
        raise RuntimeError("调用失败 bearer sk-or-v1-DUMMYKEYFORTEST0000000000")

    job = manager.submit("proj-1", "video", runner)
    done = await wait_done(manager, job.id)

    assert done.status == JobStatus.FAILED
    assert "sk-or-v1-DUMMYKEYFORTEST0000000000" not in done.error
    assert "***" in done.error
    assert not done.is_active


@pytest.mark.asyncio
async def test_cancel_running_job():
    manager = JobManager()
    started = asyncio.Event()

    async def runner(ctx: JobContext) -> None:
        started.set()
        await asyncio.sleep(10)

    job = manager.submit("proj-1", "video", runner)
    await started.wait()
    assert await manager.cancel(job.id) is True

    done = await wait_done(manager, job.id)
    assert done.status == JobStatus.CANCELLED
    assert done.message == "已取消"


@pytest.mark.asyncio
async def test_cancel_unknown_or_finished_job_returns_false():
    manager = JobManager()

    async def runner(ctx: JobContext) -> None:
        return None

    assert await manager.cancel("nope") is False
    job = manager.submit("proj-1", "video", runner)
    await wait_done(manager, job.id)
    assert await manager.cancel(job.id) is False


@pytest.mark.asyncio
async def test_active_for_and_project_isolation():
    manager = JobManager()
    gate = asyncio.Event()

    async def slow(ctx: JobContext) -> None:
        await gate.wait()

    async def quick(ctx: JobContext) -> None:
        return None

    slow_job = manager.submit("proj-1", "video", slow)
    other = manager.submit("proj-2", "video", quick)
    await wait_done(manager, other.id)

    assert manager.active_for("proj-1", "video") is slow_job
    assert manager.active_for("proj-2", "video") is None
    assert manager.active_for("proj-1", "audio") is None
    assert manager.active_count == 1

    gate.set()
    await wait_done(manager, slow_job.id)
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_cancel_project_cancels_all_kinds():
    manager = JobManager()
    gate = asyncio.Event()

    async def slow(ctx: JobContext) -> None:
        await gate.wait()

    first = manager.submit("proj-1", "video", slow)
    second = manager.submit("proj-1", "audio", slow)
    third = manager.submit("proj-2", "video", slow)

    assert await manager.cancel_project("proj-1") == 2
    for job in (first, second):
        await wait_done(manager, job.id)
    assert first.status == JobStatus.CANCELLED
    assert second.status == JobStatus.CANCELLED
    assert third.is_active  # 别的项目不受影响

    await manager.shutdown()
    await asyncio.sleep(0)
    assert third.status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_list_is_filtered_and_sorted():
    manager = JobManager()

    async def runner(ctx: JobContext) -> None:
        return None

    for project_id in ("a", "b", "a"):
        manager.submit(project_id, "video", runner)
    await asyncio.sleep(0.05)

    assert len(manager.list(project_id="a")) == 2
    assert len(manager.list()) == 3
    assert all(j.project_id == "a" for j in manager.list(project_id="a"))


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_tasks():
    manager = JobManager()
    gate = asyncio.Event()

    async def slow(ctx: JobContext) -> None:
        await gate.wait()

    job = manager.submit("proj-1", "video", slow)
    await asyncio.sleep(0.01)
    await manager.shutdown()
    assert not job.is_active


@pytest.mark.asyncio
async def test_history_is_pruned():
    manager = JobManager(max_history=3)

    async def runner(ctx: JobContext) -> None:
        return None

    for _ in range(6):
        manager.submit("proj", "video", runner)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while manager.active_count and loop.time() < deadline:
        await asyncio.sleep(0.01)

    assert manager.active_count == 0
    assert len(manager.list(limit=100)) <= 3
