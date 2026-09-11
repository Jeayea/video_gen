from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ConsistencyIssue(BaseModel):
    severity: IssueSeverity
    scene_id: str = ""
    shot_id: str = ""
    category: str  # name / number / timeline / presence / alias / duration / storyboard
    message: str
    suggestion: str = ""


class ConsistencyReport(BaseModel):
    #: 报告覆盖到的阶段："script" 或 "script+storyboard"
    stage: str = "script"
    issues: list[ConsistencyIssue] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == IssueSeverity.ERROR for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == IssueSeverity.WARNING for i in self.issues)

    def count(self, severity: IssueSeverity) -> int:
        return sum(1 for i in self.issues if i.severity == severity)
