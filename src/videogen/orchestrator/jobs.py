from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field

from videogen.utils.redact import scrub_secrets

logger = logging.getLogger(__name__)

__all__ = ["Job", "JobContext", "JobManager", "JobStatus"]


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    project_id: str
    kind: str  # "video" | "audio" | "compose"
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    total: int = 0
    message: str = ""
    error: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status in {JobStatus.QUEUED, JobStatus.RUNNING}

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, round(self.progress / self.total * 100))

    def to_public(self) -> dict:
        return {**self.model_dump(), "percent": self.percent, "is_active": self.is_active}


class JobContext:
    """交给 runner 的句柄：更新进度 + 查询是否已被取消。"""

    def __init__(self, job: Job) -> None:
        self.job = job
        self._cancelled = False

    def set_progress(self, done: int, total: int, message: str = "") -> None:
        self.job.progress = max(0, done)
        self.job.total = max(0, total)
        if message:
            self.job.message = message

    def note(self, message: str) -> None:
        self.job.message = message

    def request_cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class JobManager:
    """进程内后台任务管理器。

    能力：提交、进度查询、取消、按项目/类型互斥、历史裁剪。
    局限：状态在内存里，多 worker / 重启后不保留 —— 重启时由
    `Pipeline.recover_interrupted_jobs()` 把中断的项目状态收回来。
    """

    def __init__(self, max_history: int = 50) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._contexts: dict[str, JobContext] = {}
        self._max_history = max_history

    # ── 查询 ──────────────────────────────────────────

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, project_id: str | None = None, limit: int = 20) -> list[Job]:
        jobs = [j for j in self._jobs.values() if project_id is None or j.project_id == project_id]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def active_for(self, project_id: str, kind: str | None = None) -> Job | None:
        for job in self._jobs.values():
            if job.project_id != project_id or not job.is_active:
                continue
            if kind is None or job.kind == kind:
                return job
        return None

    def busy_for(self, project_id: str, kind: str | None = None) -> Job | None:
        """该项目还有没跑完的任务协程吗（**含已标记取消、还在收尾的**）。

        `cancel()` 会立刻把状态置成 CANCELLED（`is_active` 变 False），但协程还要跑完取消路径
        （记账、落状态）。互斥要看协程，否则新任务会和旧任务的收尾交错（旧收尾覆盖新状态）。
        """
        for job_id, task in self._tasks.items():
            job = self._jobs.get(job_id)
            if job is None or task.done() or job.project_id != project_id:
                continue
            if kind is None or job.kind == kind:
                return job
        return None

    @property
    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.is_active)

    # ── 提交 / 取消 ────────────────────────────────────

    def submit(
        self,
        project_id: str,
        kind: str,
        runner: Callable[[JobContext], Awaitable[None]],
    ) -> Job:
        """提交后台任务。必须从事件循环里调用。"""
        job = Job(project_id=project_id, kind=kind)
        context = JobContext(job)
        self._jobs[job.id] = job
        self._contexts[job.id] = context

        task = asyncio.create_task(self._run(job, context, runner), name=f"job-{kind}-{job.id}")
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t, job_id=job.id: self._on_done(job_id))
        logger.info("任务已提交 job=%s kind=%s project=%s", job.id, kind, project_id)
        return job

    async def cancel(self, job_id: str) -> bool:
        """请求取消。返回是否真的发起了取消。"""
        job = self._jobs.get(job_id)
        if job is None or not job.is_active:
            return False

        context = self._contexts.get(job_id)
        if context:
            context.request_cancel()

        # 状态在这里就落定：任务可能还没被调度执行，一旦从未启动，
        # 协程体根本不会运行，只靠 _run 的 except 会把状态永远留在 queued。
        job.status = JobStatus.CANCELLED
        job.message = job.message or "已取消"
        job.finished_at = job.finished_at or datetime.now(UTC).isoformat()

        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        logger.info("任务取消请求 job=%s", job_id)
        return True

    async def cancel_project(self, project_id: str, kind: str | None = None) -> int:
        count = 0
        for job in list(self._jobs.values()):
            if job.project_id == project_id and job.is_active:
                if kind and job.kind != kind:
                    continue
                if await self.cancel(job.id):
                    count += 1
        return count

    async def shutdown(self) -> None:
        """进程退出时取消所有在跑的任务，避免留下孤儿 ffmpeg 进程。"""
        # 先把状态落定：从未被调度的任务在 cancel 后不会运行协程体
        for job in self._jobs.values():
            if job.is_active:
                job.status = JobStatus.CANCELLED
                job.message = job.message or "服务关闭，任务已取消"
                job.finished_at = job.finished_at or datetime.now(UTC).isoformat()

        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── 内部 ──────────────────────────────────────────

    async def _run(
        self,
        job: Job,
        context: JobContext,
        runner: Callable[[JobContext], Awaitable[None]],
    ) -> None:
        if job.status == JobStatus.CANCELLED:
            # 还没轮到执行就被取消了
            self._prune()
            return

        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC).isoformat()
        try:
            await runner(context)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.message = job.message or "已取消"
            logger.info("任务已取消 job=%s", job.id)
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = scrub_secrets(f"{type(exc).__name__}: {exc}")[:500]
            logger.exception("任务失败 job=%s", job.id)
        else:
            if job.status == JobStatus.RUNNING:
                job.status = JobStatus.SUCCEEDED
        finally:
            job.finished_at = job.finished_at or datetime.now(UTC).isoformat()
            self._prune()

    def _on_done(self, job_id: str) -> None:
        self._tasks.pop(job_id, None)
        self._contexts.pop(job_id, None)

    def _prune(self) -> None:
        if len(self._jobs) <= self._max_history:
            return
        finished = [j for j in self._jobs.values() if not j.is_active]
        finished.sort(key=lambda j: j.finished_at or j.created_at)
        for job in finished[: len(self._jobs) - self._max_history]:
            self._jobs.pop(job.id, None)
