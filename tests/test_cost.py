from __future__ import annotations

import pytest

from tests.conftest import wait_for_job
from tests.fakes import FakeTTSProvider, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.cost import CostEntry, CostReport, CostStage
from videogen.models.project import Project, ProjectStatus
from videogen.orchestrator.pipeline import Pipeline
from videogen.providers.llm.base import TokenUsage
from videogen.services import cost_service
from videogen.services.cost_service import BudgetExceeded, PriceTable


def test_price_table_math():
    prices = PriceTable(
        llm_input_per_mtok=0.30, llm_output_per_mtok=2.50, video_per_second=0.05, tts_per_mchar=15.0
    )
    usage = TokenUsage(calls=1, input_tokens=1_000_000, output_tokens=200_000)
    assert prices.llm_usd(usage) == pytest.approx(0.30 + 0.5)
    assert prices.video_usd(10) == pytest.approx(0.5)
    assert prices.tts_usd(2_000_000) == pytest.approx(30.0)
    # 负值/零值不能算出负数
    assert prices.video_usd(-5) == 0.0
    assert prices.tts_usd(-1) == 0.0


def test_price_table_reads_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "video_price_per_second", 0.12)
    monkeypatch.setattr(settings, "tts_price_per_mchar", 3.0)
    prices = PriceTable.from_settings()
    assert prices.video_per_second == 0.12
    assert prices.tts_per_mchar == 3.0


def test_cost_report_aggregations():
    report = CostReport(budget_usd=1.0)
    report.append(CostEntry(stage=CostStage.BIBLE, usd=0.1, input_tokens=10, output_tokens=5))
    report.append(CostEntry(stage=CostStage.BIBLE, usd=0.2, input_tokens=1, output_tokens=1))
    report.append(CostEntry(stage=CostStage.VIDEO, usd=0.5, units=10, unit_kind="second"))

    assert report.total_usd == pytest.approx(0.8)
    assert report.total_tokens == 17
    assert report.by_stage() == {"bible": 0.3, "video": 0.5}
    assert report.remaining_usd == pytest.approx(0.2)
    assert report.over_budget is False

    report.append(CostEntry(stage=CostStage.AUDIO, usd=0.3))
    assert report.over_budget is True
    assert report.remaining_usd == pytest.approx(-0.1)
    assert report.summary()["over_budget"] is True
    assert report.stage_totals()["video"]["units"] == 10


def test_cost_report_without_budget_is_unlimited():
    report = CostReport()
    assert report.remaining_usd is None
    assert report.over_budget is False
    cost_service.check_budget(report, upcoming_usd=999.0, action="测试")  # 不抛异常


def test_check_budget_blocks_and_explains():
    report = CostReport(budget_usd=0.5)
    report.append(CostEntry(stage=CostStage.SCRIPT, usd=0.4))

    with pytest.raises(BudgetExceeded) as exc:
        cost_service.check_budget(report, upcoming_usd=0.2, action="生成视频")
    message = str(exc.value)
    assert "预算不足" in message and "生成视频" in message and "BUDGET_USD=0" in message

    # 刚好够 / 不花钱的操作都应该放行
    cost_service.check_budget(report, upcoming_usd=0.1, action="生成视频")
    cost_service.check_budget(report, upcoming_usd=0.0, action="生成视频")


def test_estimates_from_storyboard(store):
    from videogen.models.storyboard import Shot, Storyboard

    storyboard = Storyboard(
        shots=[
            Shot(id="s1", scene_id="s", shot_no=1, duration_sec=5, dialogue_text="你好世界"),
            Shot(id="s2", scene_id="s", shot_no=2, duration_sec=5, narration="旁白内容"),
        ]
    )
    assert cost_service.estimate_video_usd(storyboard, PriceTable(video_per_second=1.0)) == 10.0
    # 一句 4 字 + 一句 4 字 = 8 字 -> 每百万字 1000 元
    assert cost_service.estimate_audio_usd(
        storyboard, PriceTable(tts_per_mchar=1_000_000)
    ) == pytest.approx(8.0)


# ── pipeline 记账 ─────────────────────────────────────


def build_pipeline(store, llm, video=None, tts=None, bgm=None) -> Pipeline:
    from tests.fakes import FakeBgmLibrary

    return Pipeline(
        store,
        llm_factory=lambda: llm,
        video_factory=lambda: video or FakeVideoProvider(),
        tts_factory=lambda: tts or FakeTTSProvider(),
        bgm_factory=lambda: bgm or FakeBgmLibrary(),
    )


@pytest.mark.asyncio
async def test_text_stages_are_recorded(store, fake_llm):
    pipeline = build_pipeline(store, fake_llm)
    project = Project(input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)

    await pipeline.generate_bible(project.id)
    report = store.load_cost_report(project.id)
    assert [e.stage for e in report.entries] == [CostStage.BIBLE]
    assert report.entries[0].total_tokens > 0
    assert report.entries[0].model == "fake-model"
    assert report.entries[0].provider == "fake"

    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)
    await pipeline.generate_storyboard(project.id)

    stages = [e.stage for e in store.load_cost_report(project.id).entries]
    assert stages == [CostStage.BIBLE, CostStage.SCRIPT, CostStage.STORYBOARD]
    # 每次 take_usage 都要清零，避免重复计费
    assert fake_llm.usage.total_tokens == 0


@pytest.mark.asyncio
async def test_media_stages_are_recorded(
    store, media_pipeline, fake_video, fake_tts, ready_project
):
    job = await media_pipeline.start_video_generation(ready_project.id)
    await wait_for_job(media_pipeline, job.id)
    report = store.load_cost_report(ready_project.id)
    video_entry = next(e for e in report.entries if e.stage == CostStage.VIDEO)
    storyboard = store.load_storyboard(ready_project.id)
    assert video_entry.units == float(storyboard.total_duration_sec)
    assert video_entry.calls == len(storyboard.shots)
    assert video_entry.unit_kind == "second"

    # 续跑（片段都在）不应该重复记账
    job = await media_pipeline.start_video_generation(ready_project.id)
    await wait_for_job(media_pipeline, job.id)
    report = store.load_cost_report(ready_project.id)
    assert len([e for e in report.entries if e.stage == CostStage.VIDEO]) == 1

    job = await media_pipeline.start_audio_generation(ready_project.id)
    await wait_for_job(media_pipeline, job.id)
    report = store.load_cost_report(ready_project.id)
    audio_entry = next(e for e in report.entries if e.stage == CostStage.AUDIO)
    assert audio_entry.unit_kind == "char"
    assert audio_entry.units > 0
    assert audio_entry.calls == len(store.load_audio_manifest(ready_project.id).clips)


@pytest.mark.asyncio
async def test_compose_stage_is_recorded(
    store, monkeypatch, media_pipeline, audio_ready_project
):
    """合成阶段用假 compose 记账，避免依赖真实 ffmpeg。"""
    from tests.test_compose import SlowCompose
    from videogen.orchestrator import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module.compose_service, "compose", SlowCompose(delay=0))
    job = await media_pipeline.start_compose(audio_ready_project.id)
    await wait_for_job(media_pipeline, job.id)

    report = store.load_cost_report(audio_ready_project.id)
    entry = next(e for e in report.entries if e.stage == CostStage.COMPOSE)
    assert entry.usd == 0.0
    assert entry.unit_kind == "second"
    assert entry.calls == 2


@pytest.mark.asyncio
async def test_budget_blocks_second_stage(store, fake_llm, monkeypatch):
    settings = get_settings()
    # 先跑一次圣经，拿到真实花费，再把预算压到刚好不够
    pipeline = build_pipeline(store, fake_llm)
    project = Project(input_text="预算测试", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)

    spent = store.load_cost_report(project.id).total_usd
    monkeypatch.setattr(settings, "budget_usd", spent)

    with pytest.raises(BudgetExceeded):
        await pipeline.generate_script(project.id)
    # 被拒绝时状态不能被改成 FAILED
    assert store.load_project(project.id).status == ProjectStatus.DRAFT


@pytest.mark.asyncio
async def test_video_budget_gate_uses_price(store, monkeypatch, ready_project, fake_llm):
    settings = get_settings()
    monkeypatch.setattr(settings, "video_price_per_second", 1.0)
    monkeypatch.setattr(settings, "budget_usd", 0.5)
    pipeline = build_pipeline(store, fake_llm)

    storyboard = store.load_storyboard(ready_project.id)
    assert storyboard.total_duration_sec > 0

    with pytest.raises(BudgetExceeded) as exc:
        await pipeline.start_video_generation(ready_project.id)
    assert "视频" in str(exc.value)
    assert store.load_project(ready_project.id).status == ProjectStatus.STORYBOARD_CONFIRMED


@pytest.mark.asyncio
async def test_zero_budget_never_blocks(store, ready_project, fake_llm):
    pipeline = build_pipeline(store, fake_llm)
    job = await pipeline.start_video_generation(ready_project.id)  # budget_usd 默认 0
    finished = await wait_for_job(pipeline, job.id)
    assert finished.status.value == "succeeded"


@pytest.mark.asyncio
async def test_cost_status_shape(store, fake_llm):
    pipeline = build_pipeline(store, fake_llm)
    project = Project(input_text="成本状态", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)

    status = pipeline.cost_status(project.id)
    assert status["summary"]["total_usd"] > 0
    assert status["stages"]["bible"]["tokens"] > 0
    assert status["budget_usd"] == get_settings().budget_usd
    assert set(status["estimate"]) == {"video_usd", "audio_usd"}
    assert "llm_input_per_mtok" in status["prices"]
