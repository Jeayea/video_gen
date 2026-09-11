from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, Field

from videogen.models.project import AspectRatio, InputMode
from videogen.utils.redact import scrub_secrets

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，避免循环 import
    from videogen.orchestrator.pipeline import Pipeline

logger = logging.getLogger(__name__)

__all__ = ["Batch", "BatchItem", "BatchManager", "BatchStatus", "ItemStatus"]

#: 批量链路可以停在哪一步
STOP_AFTER_STAGES = ("storyboard", "video", "audio", "compose")


class BatchStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ItemStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchItem(BaseModel):
    index: int
    title: str = ""
    input_text: str = ""
    project_id: str = ""
    status: ItemStatus = ItemStatus.PENDING
    stage: str = ""
    error: str = ""
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Batch(BaseModel):
    """一次批量生产（多个主题自动跑完整条链路）。

    注意：批量会**跳过人工确认门控** —— 这正是批量的意义，所以建议先把单条链路调稳。
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    status: BatchStatus = BatchStatus.QUEUED
    items: list[BatchItem] = Field(default_factory=list)
    target_duration_sec: int = 45
    aspect_ratio: AspectRatio = AspectRatio.VERTICAL
    input_mode: InputMode = InputMode.THEME
    stop_after: str = "compose"
    model: str = ""
    #: 配音文案模式（"" = 用 .env 配置；off/narration/polish/full）
    audio_script_mode: str = ""
    concurrency: int = 1
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    message: str = ""

    @property
    def is_active(self) -> bool:
        return self.status in {BatchStatus.QUEUED, BatchStatus.RUNNING}

    @property
    def percent(self) -> int:
        if not self.items:
            return 0
        done = sum(1 for i in self.items if i.status in {
            ItemStatus.SUCCEEDED,
            ItemStatus.FAILED,
            ItemStatus.CANCELLED,
        })
        return min(100, round(done / len(self.items) * 100))

    def to_public(self) -> dict:
        return {
            **self.model_dump(),
            "percent": self.percent,
            "is_active": self.is_active,
            "summary": self.summary(),
        }

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.items),
            "succeeded": sum(1 for i in self.items if i.status == ItemStatus.SUCCEEDED),
            "failed": sum(1 for i in self.items if i.status == ItemStatus.FAILED),
            "pending": sum(1 for i in self.items if i.status == ItemStatus.PENDING),
            "running": sum(1 for i in self.items if i.status == ItemStatus.RUNNING),
            "cancelled": sum(1 for i in self.items if i.status == ItemStatus.CANCELLED),
        }


class BatchManager:
    """进程内批量生产管理器（与 JobManager 一样，状态在内存里）。"""

    def __init__(self, max_history: int = 20) -> None:
        self._batches: dict[str, Batch] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._max_history = max_history

    # ── 查询 ──────────────────────────────────────────

    def get(self, batch_id: str) -> Batch | None:
        return self._batches.get(batch_id)

    def list(self, limit: int = 20) -> list[Batch]:
        batches = sorted(self._batches.values(), key=lambda b: b.created_at, reverse=True)
        return batches[:limit]

    @property
    def active_count(self) -> int:
        return sum(1 for b in self._batches.values() if b.is_active)

    # ── 提交 / 取消 ────────────────────────────────────

    def submit(
        self,
        pipeline: Pipeline,
        items: list[dict],
        *,
        target_duration_sec: int = 45,
        aspect_ratio: AspectRatio = AspectRatio.VERTICAL,
        input_mode: InputMode = InputMode.THEME,
        stop_after: str = "compose",
        model: str = "",
        audio_script_mode: str = "",
        concurrency: int = 1,
        max_items: int = 20,
    ) -> Batch:
        """提交批量任务。必须从事件循环里调用。"""
        cleaned = [i for i in items if str(i.get("input_text", "")).strip()]
        if not cleaned:
            raise ValueError("批量任务至少要有一个非空的主题/输入")
        if len(cleaned) > max_items:
            raise ValueError(f"一次最多 {max_items} 条（当前 {len(cleaned)} 条）")
        if stop_after not in STOP_AFTER_STAGES:
            raise ValueError(f"stop_after 只能是 {', '.join(STOP_AFTER_STAGES)}")

        batch = Batch(
            items=[
                BatchItem(
                    index=index,
                    title=str(item.get("title") or "").strip() or f"批量项目 {index + 1}",
                    input_text=str(item["input_text"]).strip(),
                )
                for index, item in enumerate(cleaned)
            ],
            target_duration_sec=target_duration_sec,
            aspect_ratio=aspect_ratio,
            input_mode=input_mode,
            stop_after=stop_after,
            model=model,
            audio_script_mode=audio_script_mode,
            concurrency=max(1, min(4, concurrency)),
        )
        self._batches[batch.id] = batch
        task = asyncio.create_task(self._run(pipeline, batch), name=f"batch-{batch.id}")
        self._tasks[batch.id] = task
        task.add_done_callback(lambda _t, bid=batch.id: self._tasks.pop(bid, None))
        logger.info("批量任务已提交 batch=%s 共 %d 条", batch.id, len(batch.items))
        return batch

    async def cancel(self, batch_id: str) -> bool:
        batch = self._batches.get(batch_id)
        if batch is None or not batch.is_active:
            return False
        batch.status = BatchStatus.CANCELLED
        batch.message = "已取消"
        batch.finished_at = batch.finished_at or datetime.now(UTC).isoformat()
        for item in batch.items:
            if item.status in {ItemStatus.PENDING, ItemStatus.RUNNING}:
                item.status = ItemStatus.CANCELLED
                item.updated_at = datetime.now(UTC).isoformat()
        task = self._tasks.get(batch_id)
        if task and not task.done():
            task.cancel()
        logger.info("批量任务取消请求 batch=%s", batch_id)
        return True

    async def shutdown(self) -> None:
        for batch in self._batches.values():
            if batch.is_active:
                batch.status = BatchStatus.CANCELLED
                batch.message = batch.message or "服务关闭，批量任务已取消"
                batch.finished_at = batch.finished_at or datetime.now(UTC).isoformat()
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── 内部 ──────────────────────────────────────────

    async def _run(self, pipeline: Pipeline, batch: Batch) -> None:
        from videogen.models.project import Project

        batch.status = BatchStatus.RUNNING
        batch.started_at = datetime.now(UTC).isoformat()
        semaphore = asyncio.Semaphore(batch.concurrency)

        async def run_item(item: BatchItem) -> None:
            if batch.status == BatchStatus.CANCELLED:
                item.status = ItemStatus.CANCELLED
                return
            async with semaphore:
                if batch.status == BatchStatus.CANCELLED:
                    item.status = ItemStatus.CANCELLED
                    return
                item.status = ItemStatus.RUNNING
                item.updated_at = datetime.now(UTC).isoformat()
                project = Project(
                    title=item.title,
                    input_text=item.input_text,
                    input_mode=batch.input_mode,
                    aspect_ratio=batch.aspect_ratio,
                    target_duration_sec=batch.target_duration_sec,
                )
                pipeline.store.save_project(project)
                item.project_id = project.id
                try:
                    await pipeline.run_chain(
                        project.id,
                        stop_after=batch.stop_after,
                        model=batch.model,
                        script_mode=batch.audio_script_mode or None,
                        on_stage=_stage_recorder(item),
                    )
                except asyncio.CancelledError:
                    item.status = ItemStatus.CANCELLED
                    item.updated_at = datetime.now(UTC).isoformat()
                    raise
                except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞其它条目
                    item.status = ItemStatus.FAILED
                    item.error = scrub_secrets(f"{type(exc).__name__}: {exc}")[:400]
                    item.updated_at = datetime.now(UTC).isoformat()
                    logger.warning(
                        "批量条目失败 batch=%s project=%s：%s", batch.id, project.id, item.error
                    )
                    return
                item.status = ItemStatus.SUCCEEDED
                item.stage = batch.stop_after
                item.updated_at = datetime.now(UTC).isoformat()

        try:
            await asyncio.gather(*(run_item(item) for item in batch.items))
        except asyncio.CancelledError:
            batch.status = BatchStatus.CANCELLED
            batch.message = "已取消"
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，避免任务静默死掉
            batch.status = BatchStatus.FAILED
            batch.message = scrub_secrets(str(exc))[:300]
        else:
            summary = batch.summary()
            if summary["failed"] and summary["succeeded"]:
                batch.status = BatchStatus.PARTIAL
            elif summary["failed"]:
                batch.status = BatchStatus.FAILED
            elif batch.status != BatchStatus.CANCELLED:
                batch.status = BatchStatus.SUCCEEDED
            batch.message = (
                f"完成：成功 {summary['succeeded']}，失败 {summary['failed']}"
                + (f"，取消 {summary['cancelled']}" if summary["cancelled"] else "")
            )
        finally:
            batch.finished_at = batch.finished_at or datetime.now(UTC).isoformat()
            self._prune()
            logger.info("批量任务结束 batch=%s status=%s", batch.id, batch.status.value)

    def _prune(self) -> None:
        if len(self._batches) <= self._max_history:
            return
        finished = [b for b in self._batches.values() if not b.is_active]
        finished.sort(key=lambda b: b.finished_at or b.created_at)
        for batch in finished[: len(self._batches) - self._max_history]:
            self._batches.pop(batch.id, None)


def _stage_recorder(item: BatchItem):
    def record(stage: str) -> None:
        item.stage = stage
        item.updated_at = datetime.now(UTC).isoformat()
    return record
