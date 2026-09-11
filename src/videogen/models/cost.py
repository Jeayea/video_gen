from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class CostStage(str, Enum):
    """记账的粒度：按流水线阶段。"""

    BIBLE = "bible"
    SCRIPT = "script"
    STORYBOARD = "storyboard"
    #: AI 生成角色参考图（角色一致性）
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    COMPOSE = "compose"
    OTHER = "other"


class CostEntry(BaseModel):
    stage: CostStage = CostStage.OTHER
    provider: str = ""
    model: str = ""
    #: 调用次数（LLM 请求数 / 生成的片段数）
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: 计量单位：秒（视频/成片）或字符（配音）
    units: float = 0.0
    unit_kind: str = ""
    usd: float = 0.0
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class CostReport(BaseModel):
    """项目级成本账本（落盘 `cost_report.json`）。"""

    entries: list[CostEntry] = Field(default_factory=list)
    #: 0 = 不限制
    budget_usd: float = 0.0
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def total_usd(self) -> float:
        return round(sum(e.usd for e in self.entries), 6)

    @property
    def total_tokens(self) -> int:
        return sum(e.total_tokens for e in self.entries)

    @property
    def remaining_usd(self) -> float | None:
        if self.budget_usd <= 0:
            return None
        return round(self.budget_usd - self.total_usd, 6)

    @property
    def over_budget(self) -> bool:
        return self.budget_usd > 0 and self.total_usd >= self.budget_usd

    def by_stage(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for entry in self.entries:
            key = entry.stage.value
            result[key] = round(result.get(key, 0.0) + entry.usd, 6)
        return result

    def stage_totals(self) -> dict[str, dict[str, float]]:
        """每个阶段的 usd / tokens / units，给前端表格用。"""
        result: dict[str, dict[str, float]] = {}
        for entry in self.entries:
            row = result.setdefault(
                entry.stage.value,
                {"usd": 0.0, "tokens": 0.0, "units": 0.0, "calls": 0.0},
            )
            row["usd"] = round(row["usd"] + entry.usd, 6)
            row["tokens"] += entry.total_tokens
            row["units"] = round(row["units"] + entry.units, 3)
            row["calls"] += entry.calls
        return result

    def append(self, entry: CostEntry) -> None:
        self.entries.append(entry)
        self.updated_at = datetime.now(UTC).isoformat()

    def summary(self) -> dict[str, object]:
        return {
            "total_usd": self.total_usd,
            "total_tokens": self.total_tokens,
            "budget_usd": self.budget_usd,
            "remaining_usd": self.remaining_usd,
            "over_budget": self.over_budget,
            "entries": len(self.entries),
            "by_stage": self.by_stage(),
        }


__all__ = ["CostEntry", "CostReport", "CostStage"]
