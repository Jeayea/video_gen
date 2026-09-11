from __future__ import annotations

import logging

from pydantic import BaseModel

from videogen.config import Settings, get_settings
from videogen.models.cost import CostEntry, CostReport, CostStage
from videogen.models.storyboard import Storyboard
from videogen.providers.llm.base import TokenUsage
from videogen.utils.file_store import FileStore
from videogen.utils.text import count_speech_chars

logger = logging.getLogger(__name__)

__all__ = [
    "BudgetExceeded",
    "PriceTable",
    "audio_entry",
    "check_budget",
    "compose_entry",
    "estimate_audio_usd",
    "estimate_video_usd",
    "llm_entry",
    "load_report",
    "record",
    "video_entry",
]


class BudgetExceeded(RuntimeError):
    """超出预算，拒绝开工（消息面向用户，可直接展示）。"""


class PriceTable(BaseModel):
    """单价表。第三方 provider 的价格都会变，所以单价全部走配置。"""

    llm_input_per_mtok: float = 0.30
    llm_output_per_mtok: float = 2.50
    video_per_second: float = 0.0
    tts_per_mchar: float = 0.0

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> PriceTable:
        settings = settings or get_settings()
        return cls(
            llm_input_per_mtok=max(0.0, settings.llm_input_price_per_mtok),
            llm_output_per_mtok=max(0.0, settings.llm_output_price_per_mtok),
            video_per_second=max(0.0, settings.video_price_per_second),
            tts_per_mchar=max(0.0, settings.tts_price_per_mchar),
        )

    def llm_usd(self, usage: TokenUsage) -> float:
        return round(
            usage.input_tokens / 1_000_000 * self.llm_input_per_mtok
            + usage.output_tokens / 1_000_000 * self.llm_output_per_mtok,
            6,
        )

    def video_usd(self, seconds: float) -> float:
        return round(max(0.0, seconds) * self.video_per_second, 6)

    def tts_usd(self, chars: int) -> float:
        return round(max(0, chars) / 1_000_000 * self.tts_per_mchar, 6)


# ── 账本读写 ──────────────────────────────────────────


def load_report(store: FileStore, project_id: str, settings: Settings | None = None) -> CostReport:
    settings = settings or get_settings()
    report = store.load_cost_report(project_id) or CostReport()
    # 预算以配置为准（改配置后立刻生效），但要保留账本
    report.budget_usd = max(0.0, settings.budget_usd)
    return report


def record(store: FileStore, project_id: str, entry: CostEntry) -> CostReport:
    report = load_report(store, project_id)
    report.append(entry)
    store.save_cost_report(project_id, report)
    logger.info(
        "成本记账 project=%s stage=%s usd=%.4f tokens=%d units=%.1f%s",
        project_id,
        entry.stage.value,
        entry.usd,
        entry.total_tokens,
        entry.units,
        entry.unit_kind,
    )
    return report


# ── 账目构造 ──────────────────────────────────────────


def llm_entry(
    stage: CostStage,
    usage: TokenUsage,
    *,
    provider: str,
    model: str,
    prices: PriceTable | None = None,
) -> CostEntry:
    prices = prices or PriceTable.from_settings()
    return CostEntry(
        stage=stage,
        provider=provider,
        model=model,
        calls=usage.calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        unit_kind="token",
        usd=prices.llm_usd(usage),
    )


def video_entry(
    provider: str,
    model: str,
    seconds: float,
    calls: int,
    prices: PriceTable | None = None,
    *,
    price_per_second: float | None = None,
) -> CostEntry:
    """记一条视频花费。

    `price_per_second` 非空时以它为准（真实 provider 会从自己的价目表读，
    例如 OpenRouter 的 `pricing_skus.duration_seconds`），否则用配置里的单价。
    """
    prices = prices or PriceTable.from_settings()
    unit = prices.video_per_second if price_per_second is None else max(0.0, price_per_second)
    seconds = max(0.0, seconds)
    return CostEntry(
        stage=CostStage.VIDEO,
        provider=provider,
        model=model,
        calls=calls,
        units=round(seconds, 3),
        unit_kind="second",
        usd=round(seconds * unit, 6),
    )


def audio_entry(
    provider: str, chars: int, calls: int, prices: PriceTable | None = None
) -> CostEntry:
    prices = prices or PriceTable.from_settings()
    return CostEntry(
        stage=CostStage.AUDIO,
        provider=provider,
        model="",
        calls=calls,
        units=float(chars),
        unit_kind="char",
        usd=prices.tts_usd(chars),
    )


def compose_entry(provider: str, seconds: float, segments: int) -> CostEntry:
    # 本地 ffmpeg 不额外花钱，但把耗时/时长记下来便于统计
    return CostEntry(
        stage=CostStage.COMPOSE,
        provider=provider,
        calls=segments,
        units=round(max(0.0, seconds), 3),
        unit_kind="second",
        usd=0.0,
    )


# ── 预估与预算门控 ────────────────────────────────────


def estimate_video_usd(storyboard: Storyboard | None, prices: PriceTable | None = None) -> float:
    if not storyboard:
        return 0.0
    prices = prices or PriceTable.from_settings()
    return prices.video_usd(float(storyboard.total_duration_sec))


def estimate_audio_usd(
    storyboard: Storyboard | None, prices: PriceTable | None = None
) -> float:
    """按台词字数预估配音成本（Edge-TTS 免费，但真实 TTS 按字符计费）。"""
    if not storyboard:
        return 0.0
    prices = prices or PriceTable.from_settings()
    chars = 0
    for shot in storyboard.shots:
        chars += count_speech_chars(shot.dialogue_text or "") or count_speech_chars(
            shot.narration or ""
        )
    return prices.tts_usd(chars)


def check_budget(
    report: CostReport,
    *,
    upcoming_usd: float = 0.0,
    action: str = "该操作",
) -> None:
    """花之前先问一句：还够不够？超了就抛 `BudgetExceeded`。

    - 知道大概要花多少（`upcoming_usd > 0`）：允许「已花 + 预计 == 预算」
    - 不知道要花多少（`upcoming_usd == 0`，比如 LLM 阶段）：只要还有余额就放行，
      已经顶到预算就拒绝 —— 否则最后一点点额度会被无限刷下去
    """
    if report.budget_usd <= 0:
        return
    # 浮点数直接比大小会踩 0.5-0.4=0.0999999 这种坑，留一点容差
    epsilon = 1e-9
    remaining = report.budget_usd - report.total_usd
    projected = max(0.0, upcoming_usd)
    enough = projected <= remaining + epsilon if projected > 0 else remaining > epsilon
    if enough:
        return
    raise BudgetExceeded(
        f"预算不足，已拒绝{action}：已花 ${report.total_usd:.4f} / 预算 ${report.budget_usd:.4f}"
        f"，本次预计还需 ${max(0.0, upcoming_usd):.4f}。"
        "可在 .env 调整 BUDGET_USD=0 关闭限制。"
    )


def warn_ratio(report: CostReport, settings: Settings | None = None) -> float | None:
    """返回已用比例（超过告警阈值时调用方自行提示）。"""
    if report.budget_usd <= 0:
        return None
    ratio = report.total_usd / report.budget_usd
    settings = settings or get_settings()
    if ratio >= max(0.0, settings.budget_warn_ratio):
        logger.warning(
            "预算已用 %.0f%%（$%.4f / $%.4f）", ratio * 100, report.total_usd, report.budget_usd
        )
    return ratio
