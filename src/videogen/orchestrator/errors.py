"""编排层的业务异常（`pipeline` 里也能照旧 import 到）。"""

from __future__ import annotations

__all__ = ["ExplainerHeld", "PipelineError", "PipelineNotFound", "UnknownVideoPrice"]


class PipelineError(Exception):
    """流水线业务错误（状态不允许、前置产物缺失、生成失败等）。"""


class ExplainerHeld(PipelineError):
    """批量链路里的科普项目没过放行条件：停在剧本阶段，等人在知识底稿里核对。"""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("停在剧本阶段等人工核对：" + "；".join(reasons))


class PipelineNotFound(PipelineError):
    """项目或产物不存在。"""


class UnknownVideoPrice(RuntimeError):
    """真实视频模型读不到单价、配置也没填：预估会是 $0。"""
