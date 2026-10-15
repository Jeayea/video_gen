"""成本前置（preflight）与短镜头合并的测试。

这两件事都直接关系到「会不会白花钱」，所以用例的重点是**口径**：
计费秒数是否按模型档位算、换 provider 会不会重跑、预算够不够、合并有没有丢台词。
"""

from __future__ import annotations

import pytest

from tests.conftest import build_confirmed_project, wait_for_job
from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.compose import ComposeReport
from videogen.models.cost import CostReport, CostStage
from videogen.models.project import Project, ProjectStatus
from videogen.models.script import Scene, Script
from videogen.models.storyboard import DurationTier, Shot, Storyboard
from videogen.models.video import ClipStatus, MediaKind, VideoClip, VideoManifest
from videogen.orchestrator.pipeline import Pipeline, PipelineError
from videogen.services import cost_service, preflight_service, storyboard_service


def make_shot(
    shot_id: str,
    *,
    seconds: int = 3,
    scene_id: str = "scene_1",
    shot_no: int = 1,
    dialogue: str = "",
    narration: str = "",
    speaker: str = "char_1",
) -> Shot:
    return Shot(
        id=shot_id,
        scene_id=scene_id,
        shot_no=shot_no,
        duration_sec=seconds,
        dialogue_text=dialogue or None,
        dialogue_speaker=speaker if dialogue else None,
        narration=narration or None,
    )


def storyboard_of(*shots: Shot) -> Storyboard:
    return Storyboard(shots=list(shots))


# ── 合并短镜头（纯逻辑）──────────────────────────────


def test_merge_absorbs_silent_shots_into_dialogue_shot():
    storyboard = storyboard_of(
        make_shot("s1", seconds=4, dialogue="这个项目是我做的。"),
        make_shot("s2", seconds=3, shot_no=2),
        make_shot("s3", seconds=3, shot_no=3),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)

    assert report.from_shots == 3
    assert report.to_shots == 2  # s1 吃掉 s2；s3 没人可吃
    assert [shot.id for shot in storyboard.shots] == ["s1", "s3"]
    assert storyboard.shots[0].duration_sec == 7
    assert storyboard.shots[0].dialogue_text == "这个项目是我做的。"  # 对白绝不丢
    assert [shot.shot_no for shot in storyboard.shots] == [1, 2]  # 重新编号
    assert report.merged[0]["absorbed"] == ["s2"]


def test_merge_never_merges_two_dialogue_shots():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3, dialogue="你？别开玩笑了。", speaker="char_2"),
        make_shot("s2", seconds=3, shot_no=2, dialogue="我说的都是真的。"),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)

    assert report.to_shots == 2  # 不同说话人必须各自一条配音
    assert len(storyboard.shots) == 2
    assert any("没有可以合并的镜头" in w for w in report.warnings)


def test_merge_carries_narration_when_primary_has_none():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3),
        make_shot("s2", seconds=3, shot_no=2, narration="她攥紧了拳头。"),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)

    assert report.to_shots == 1
    assert storyboard.shots[0].narration == "她攥紧了拳头。"
    assert report.dropped_lines == []


def test_merge_joins_same_speaker_lines_into_one_clip():
    """同一说话人的连续两句可以并成一条配音（不同说话人才不行）。"""
    storyboard = storyboard_of(
        make_shot("s1", seconds=3, dialogue="哎呀，小悦。"),
        make_shot("s2", seconds=3, shot_no=2, dialogue="你以为谁都能上封面吗？"),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)

    assert report.to_shots == 1
    assert report.dropped_lines == []
    shot = storyboard.shots[0]
    assert shot.dialogue_text == "哎呀，小悦。你以为谁都能上封面吗？"
    assert shot.dialogue_speaker == "char_1"


def test_merge_joins_lines_with_punctuation_when_missing():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3, dialogue="我能做到"),
        make_shot("s2", seconds=3, shot_no=2, dialogue="再来一次"),
    )
    storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)
    assert storyboard.shots[0].dialogue_text == "我能做到。再来一次"


def test_merge_lets_dialogue_take_over_text_less_shot():
    """主镜头一个字都没有时，后面那句对白接管这条配音 —— 什么都不丢。"""
    storyboard = storyboard_of(
        make_shot("s1", seconds=4),
        make_shot("s2", seconds=3, shot_no=2, dialogue="这…这真的是林悦？", speaker="char_4"),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)

    assert report.to_shots == 1
    assert report.dropped_lines == []
    shot = storyboard.shots[0]
    assert shot.dialogue_text == "这…这真的是林悦？"
    assert shot.dialogue_speaker == "char_4"


def test_merge_refuses_to_swallow_dialogue_over_narration():
    """主镜头已有旁白时不能吸收对白镜头：一条配音只能有一个声音，旁白会丢。"""
    storyboard = storyboard_of(
        make_shot("s1", seconds=3, narration="她攥紧了拳头。"),
        make_shot("s2", seconds=3, shot_no=2, dialogue="我不会让给你。"),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)

    assert report.to_shots == 2
    assert clean_text(storyboard.shots[0].narration) == "她攥紧了拳头。"
    assert storyboard.shots[1].dialogue_text == "我不会让给你。"


def clean_text(value) -> str:
    return (value or "").strip()


def test_merge_reports_dropped_narration():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3, dialogue="我不会让给你。"),
        make_shot("s2", seconds=3, shot_no=2, narration="她攥紧了拳头。"),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)

    assert report.to_shots == 1
    assert report.dropped_lines == ["s2: 她攥紧了拳头。"]
    assert any("旁白" in w for w in report.warnings)


def test_merge_dry_run_does_not_touch_storyboard():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3), make_shot("s2", seconds=3, shot_no=2)
    )
    before = storyboard.model_dump()
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=True)

    assert report.to_shots == 1
    assert report.dry_run is True
    assert storyboard.model_dump() == before


def test_merge_respects_max_seconds():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3),
        make_shot("s2", seconds=3, shot_no=2),
        make_shot("s3", seconds=3, shot_no=3),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, max_sec=6, dry_run=True)

    # 5~6 秒一档：3+3 正好 6，第三个另起一个桶
    assert report.to_shots == 2


def test_merge_keeps_scene_order_and_length_prefix():
    storyboard = storyboard_of(
        make_shot("a1", seconds=3, scene_id="scene_a", shot_no=1),
        make_shot("a2", seconds=3, scene_id="scene_a", shot_no=2),
        make_shot("b1", seconds=3, scene_id="scene_b", shot_no=1),
        make_shot("b2", seconds=3, scene_id="scene_b", shot_no=2),
    )
    report = storyboard_service.merge_shots(storyboard, min_sec=5, dry_run=False)

    assert report.to_shots == 2  # 每场合并成 1 个（不会跨场合并）
    assert [shot.id for shot in storyboard.shots] == ["a1", "b1"]


# ── 计费口径（preflight）──────────────────────────────


class FakeReportProvider(FakeVideoProvider):
    """带单价与档位的假 provider（模拟 H3：最短 5 秒、$0.13/秒）。"""

    def __init__(self, **kwargs):
        super().__init__(price_usd_per_sec=0.13, duration_options=[5, 10, 15], **kwargs)


@pytest.mark.asyncio
async def test_video_plan_bills_by_model_tier():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3), make_shot("s2", seconds=3, shot_no=2)
    )
    plan = await preflight_service.video_plan(storyboard, None, FakeReportProvider())

    assert plan["to_generate"] == 2
    assert plan["min_sec"] == 5
    assert plan["billed_seconds"] == 10  # 3 秒 -> 5 秒，两个镜头
    assert plan["estimated_usd"] == pytest.approx(1.30)
    assert plan["price_source"] == "provider"
    assert any("最短 5" in w for w in plan["warnings"])


@pytest.mark.asyncio
async def test_video_plan_suggests_merging_and_quantifies_saving():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3), make_shot("s2", seconds=3, shot_no=2)
    )
    plan = await preflight_service.video_plan(storyboard, None, FakeReportProvider())

    merge = plan["merge_plan"]
    assert merge["to_shots"] == 1
    assert merge["after_seconds"] == 6
    assert merge["saving_usd"] == pytest.approx(0.52)  # 10s -> 6s 计费
    assert merge["worth_it"] is True


@pytest.mark.asyncio
async def test_video_plan_detects_provider_switch_regenerating_everything():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3), make_shot("s2", seconds=3, shot_no=2)
    )
    manifest = VideoManifest(provider="mock", model="mock")
    for shot in storyboard.shots:
        manifest.upsert(
            VideoClip(
                shot_id=shot.id,
                status=ClipStatus.READY,
                media_kind=MediaKind.VIDEO,
                path=f"clips/{shot.id}.mp4",
                duration_sec=shot.duration_sec,
            )
        )
    provider = FakeReportProvider()
    provider.name = "openrouter"

    plan = await preflight_service.video_plan(storyboard, manifest, provider)

    assert plan["regenerate_all"] is True
    assert plan["to_generate"] == 2
    assert "mock → openrouter" in plan["regenerate_reason"]
    assert any("全部重新生成" in w for w in plan["warnings"])


@pytest.mark.asyncio
async def test_video_plan_only_counts_missing_shots():
    storyboard = storyboard_of(
        make_shot("s1", seconds=5), make_shot("s2", seconds=5, shot_no=2)
    )
    manifest = VideoManifest(provider="fake", model="fake")
    manifest.upsert(
        VideoClip(
            shot_id="s1",
            status=ClipStatus.READY,
            media_kind=MediaKind.VIDEO,
            path="clips/s1.mp4",
            duration_sec=5,
        )
    )
    plan = await preflight_service.video_plan(storyboard, manifest, FakeReportProvider())

    assert plan["regenerate_all"] is False
    assert plan["to_generate"] == 1
    assert plan["existing_ready"] == 1
    assert plan["billed_seconds"] == 5


@pytest.mark.asyncio
async def test_video_plan_falls_back_to_config_price():
    storyboard = storyboard_of(make_shot("s1", seconds=3))
    settings = get_settings()
    settings_price = settings.video_price_per_second
    try:
        settings.video_price_per_second = 0.05
        plan = await preflight_service.video_plan(storyboard, None, FakeVideoProvider())
    finally:
        settings.video_price_per_second = settings_price

    assert plan["price_source"] == "config"
    assert plan["estimated_usd"] == pytest.approx(0.15)  # 3 秒 × 0.05（没有档位）


@pytest.mark.asyncio
async def test_video_plan_warns_when_paid_model_has_no_price():
    storyboard = storyboard_of(make_shot("s1", seconds=3))
    provider = FakeVideoProvider()
    provider.paid = True
    plan = await preflight_service.video_plan(storyboard, None, provider)
    assert plan["price_unknown"] is True
    assert any("VIDEO_PRICE_PER_SECOND" in w for w in plan["warnings"])

    provider.paid = False  # mock / 假 provider 本来就不花钱，不该告警
    plan = await preflight_service.video_plan(storyboard, None, provider)
    assert "price_unknown" not in plan


@pytest.mark.asyncio
async def test_video_plan_handles_unavailable_provider():
    storyboard = storyboard_of(make_shot("s1", seconds=4))
    plan = await preflight_service.video_plan(
        storyboard, None, preflight_service.UnavailableVideoProvider()
    )
    assert plan["provider"] == "unavailable"
    assert plan["duration_options"] == []
    assert plan["billed_seconds"] == 4


def test_budget_projection_reports_shortfall():
    report = CostReport(budget_usd=12.0)
    report.append(
        cost_service.CostEntry(stage=CostStage.VIDEO, usd=11.0, units=80, unit_kind="second")
    )
    projection = preflight_service.budget_projection(report, 2.5)

    assert projection["spent_usd"] == pytest.approx(11.0)
    assert projection["projected_usd"] == pytest.approx(13.5)
    assert projection["shortfall_usd"] == pytest.approx(1.5)
    assert projection["over_budget"] is True


def test_budget_projection_unlimited_when_zero():
    projection = preflight_service.budget_projection(CostReport(budget_usd=0), 5.0)
    assert projection["limited"] is False
    assert projection["over_budget"] is False
    assert projection["remaining_usd"] is None


@pytest.mark.asyncio
async def test_budget_gate_blocks_full_regeneration_after_provider_switch(store, monkeypatch):
    """换 provider 后旧片段会全部重跑，预算门控必须按「全部」拦。

    这是真实场景里的钱坑：mock 跑通后切 openrouter 再点「生成缺失片段」，
    15 个镜头其实要全部重买 —— 若不按全部估，BUDGET_USD 根本拦不住。
    """
    cheap = media_pipeline(store, FakeVideoProvider())
    project = await build_confirmed_project(store, cheap)
    job = await cheap.start_video_generation(project.id)
    await wait_for_job(cheap, job.id)
    assert store.load_video_manifest(project.id).ready_clips

    expensive = FakeReportProvider()
    expensive.name = "openrouter"  # 与清单里的 mock 不同 -> 判定为换 provider
    pipeline = media_pipeline(store, expensive)
    monkeypatch.setattr(get_settings(), "budget_usd", 0.5)  # 只够买一个镜头

    plan = await pipeline.preflight(project.id, action="video")
    assert plan["video"]["regenerate_all"] is True
    assert plan["video"]["to_generate"] == plan["video"]["shots_total"]

    with pytest.raises(cost_service.BudgetExceeded) as excinfo:
        await pipeline.start_video_generation(project.id)
    assert "预算不足" in str(excinfo.value)
    # 一个片段都没生成（请求被挡在提交之前）
    assert pipeline.jobs.active_for(project.id, "video") is None


@pytest.mark.asyncio
async def test_budget_basis_is_pending_only_when_provider_unchanged(store):
    """provider 没变时预算只算「缺的那部分」，不会把已付费片段再算一遍。"""
    pipeline = media_pipeline(store, FakeReportProvider())
    project = await build_confirmed_project(store, pipeline)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    storyboard = store.load_storyboard(project.id)
    video = store.load_video_manifest(project.id)
    assert await pipeline._budget_shots(storyboard, video) == []

    # 删掉一个片段 -> 只补那一个
    video.clips[0].status = ClipStatus.FAILED
    store.save_video_manifest(project.id, video)
    targets = await pipeline._budget_shots(storyboard, store.load_video_manifest(project.id))
    assert [shot.id for shot in targets] == [storyboard.shots[0].id]


def test_audio_plan_charges_llm_script_separately():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3, dialogue="这个项目是我做的。"),
        make_shot("s2", seconds=3, shot_no=2),
    )
    settings = get_settings()
    off = preflight_service.audio_plan(storyboard, None, settings, script_mode="off")
    on = preflight_service.audio_plan(storyboard, None, settings, script_mode="narration")

    assert off["script_shots"] == 0 and off["script_usd"] == 0
    assert on["script_mode"] == "narration"
    assert on["script_shots"] == 1  # 只有没台词的 s2
    assert on["script_usd"] > 0
    assert on["estimated_usd"] >= off["estimated_usd"] + on["script_usd"] - 1e-9


def test_compose_plan_is_free_but_documented():
    plan = preflight_service.compose_plan()
    assert plan["estimated_usd"] == 0
    assert plan["warnings"]


@pytest.mark.asyncio
async def test_preflight_bundles_sections_and_budget(store):
    project = Project(title="preflight", input_text="x", target_duration_sec=30)
    store.save_project(project)
    storyboard = storyboard_of(make_shot("s1", seconds=3))
    report = cost_service.load_report(store, project.id)

    result = await preflight_service.preflight(
        project=project,
        storyboard=storyboard,
        video=None,
        audio=None,
        provider=FakeReportProvider(),
        report=report,
    )

    assert result["action"] == "all"
    assert result["video"]["action"] == "video"
    assert result["audio"]["action"] == "audio"
    assert result["compose"]["estimated_usd"] == 0
    assert result["upcoming_usd"] == pytest.approx(
        result["video"]["estimated_usd"] + result["audio"]["estimated_usd"]
    )
    assert result["budget"]["limited"] is False


# ── 视频记账（真实扣过的钱必须进账本）────────────────


class CostReportingProvider(FakeVideoProvider):
    """带单价、并且每段都报「实际扣费」的假 provider（模拟真实 API 的 usage.cost）。

    名字刻意叫 real：和 mock 片段放一起时会被判定为「换了 provider → 全部重跑」。
    """

    name = "real"

    def __init__(self, *, actual_cost_usd: float = 0.65, **kwargs):
        # 刻意不给时长档位：分镜保持 10 镜，账目算式好对（档位另有专门的用例）
        super().__init__(price_usd_per_sec=0.13, **kwargs)
        self.actual_cost_usd = actual_cost_usd

    async def generate(self, request):
        clip = await super().generate(request)
        clip.actual_cost_usd = self.actual_cost_usd
        return clip


@pytest.mark.asyncio
async def test_video_cost_is_recorded_after_provider_switch(store):
    """换 provider 全部重跑时，同 id 的旧片段不该让新花费「看起来不是新的」。

    真实事故：mock 片段已就绪 → 切 openrouter 全部重跑 15 段（$9.75），
    账本里一条 video 记录都没有，成本面板显示 $0.0011。
    """
    cheap = media_pipeline(store, FakeVideoProvider())
    project = await build_confirmed_project(store, cheap)
    job = await cheap.start_video_generation(project.id)
    await wait_for_job(cheap, job.id)
    assert store.load_video_manifest(project.id).ready_clips  # 先有 mock 片段

    pipeline = media_pipeline(store, CostReportingProvider())
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    manifest = store.load_video_manifest(project.id)
    assert len(manifest.ready_clips) == 10  # 全部重跑过
    report = cost_service.load_report(store, project.id)
    video_entries = [e for e in report.entries if e.stage == CostStage.VIDEO]
    assert video_entries, "换 provider 重跑的 $6.50 必须进账本"
    assert sum(e.usd for e in video_entries) == pytest.approx(10 * 0.65)
    billable = [e for e in video_entries if e.usd > 0]
    assert len(billable) == 1 and billable[0].calls == 10
    assert billable[0].provider == "real"
    # mock 那一轮的 $0 记录仍在（两次运行 = 两条账）
    assert len(video_entries) == 2
    # 每条片段的实测花费都标记为「已记账」，避免下次重跑重复记
    assert all(clip.cost_recorded for clip in manifest.ready_clips)


@pytest.mark.asyncio
async def test_video_cost_is_not_recorded_twice(store):
    pipeline = media_pipeline(store, CostReportingProvider())
    project = await build_confirmed_project(store, pipeline)
    for _ in range(2):
        job = await pipeline.start_video_generation(project.id)
        await wait_for_job(pipeline, job.id)

    report = cost_service.load_report(store, project.id)
    video_entries = [e for e in report.entries if e.stage == CostStage.VIDEO]
    assert len(video_entries) == 1
    assert sum(e.usd for e in video_entries) == pytest.approx(10 * 0.65)


@pytest.mark.asyncio
async def test_rerun_without_regeneration_keeps_finished_film(store):
    """没真的重跑片段时不该把成片作废（否则「补记账」会把成片删掉）。"""
    pipeline = media_pipeline(store, CostReportingProvider())
    project = await build_confirmed_project(store, pipeline)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    output_dir = store.output_dir(project.id)
    film = output_dir / "final.mp4"
    film.write_bytes(b"fake-film")
    store.save_compose_report(project.id, ComposeReport(output_path="output/final.mp4"))

    job = await pipeline.start_video_generation(project.id)  # 无待生成片段
    await wait_for_job(pipeline, job.id)

    assert film.exists(), "没有重新生成片段就不该删成片"
    assert store.load_compose_report(project.id) is not None


@pytest.mark.asyncio
async def test_reconcile_backfills_missing_video_cost(store):
    """账本漏了视频记账时，能从片段实测花费补记（不再调 API、不动媒体）。"""
    pipeline = media_pipeline(store, CostReportingProvider())
    project = await build_confirmed_project(store, pipeline)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    # 模拟老版本留下的状态：账本被清空 + 片段标记为未记账
    store.save_cost_report(project.id, CostReport(budget_usd=0.0))
    manifest = store.load_video_manifest(project.id)
    for clip in manifest.clips:
        clip.cost_recorded = False
    store.save_video_manifest(project.id, manifest)

    result = await pipeline.reconcile_video_cost(project.id)

    assert result["recorded_usd"] == pytest.approx(10 * 0.65)
    assert result["clips"] == 10
    report = cost_service.load_report(store, project.id)
    assert sum(e.usd for e in report.entries if e.stage == CostStage.VIDEO) == pytest.approx(6.5)
    # 幂等：再补一次不会重复记
    again = await pipeline.reconcile_video_cost(project.id)
    assert again["recorded_usd"] == 0


# ── 分镜阶段的计费档位（v0.11「一开始就少而长」）──────


def test_duration_constraints_text_mentions_shot_count_and_floor():
    tier = DurationTier(min_sec=5, max_sec=15, source="provider")
    text = storyboard_service.duration_constraints(9, tier)

    assert "只拆 **1** 个镜头" in text
    assert "5~15 秒" in text
    assert "正好等于 9 秒" in text
    assert storyboard_service.duration_constraints(9, None) == ""
    # 最短 1 秒 = 没有档位，不该改分镜策略
    assert storyboard_service.duration_constraints(9, DurationTier(min_sec=1)) == ""


def test_apply_duration_tier_collapses_scene_and_keeps_dialogue():
    storyboard = storyboard_of(
        make_shot("s1", seconds=3, dialogue="这个项目是我做的。"),
        make_shot("s2", seconds=3, shot_no=2),
        make_shot("s3", seconds=3, shot_no=3),
    )
    tier = DurationTier(min_sec=5, max_sec=15, source="provider")
    script = Script(scenes=[Scene(id="scene_1", heading="", duration_sec=9)])

    warnings = storyboard_service.apply_duration_tier(storyboard, script, tier)

    assert len(storyboard.shots) == 1
    shot = storyboard.shots[0]
    assert shot.duration_sec == 9  # 一场 9 秒 → 一个 9 秒镜头
    assert shot.dialogue_text == "这个项目是我做的。"
    assert shot.shot_no == 1
    assert storyboard.duration_tier is tier
    assert storyboard.total_duration_sec == 9
    assert warnings == []


def test_apply_duration_tier_keeps_extra_shots_when_dialogue_blocks_merging():
    """不同说话人的镜头合不动就保留（宁可多几个镜头，也不丢谁的声音）。"""
    storyboard = storyboard_of(
        make_shot("s1", seconds=5, dialogue="甲说的话。", speaker="char_1"),
        make_shot("s2", seconds=5, shot_no=2, dialogue="乙说的话。", speaker="char_2"),
        make_shot("s3", seconds=5, shot_no=3, dialogue="丙说的话。", speaker="char_3"),
        make_shot("s4", seconds=5, shot_no=4, dialogue="丁说的话。", speaker="char_4"),
    )
    tier = DurationTier(min_sec=5, max_sec=15, source="provider")
    script = Script(scenes=[Scene(id="scene_1", heading="", duration_sec=20)])

    warnings = storyboard_service.apply_duration_tier(storyboard, script, tier)

    assert len(storyboard.shots) == 4  # 计划 3 个，但合不动
    assert any("无法再合并" in w for w in warnings)
    assert [shot.duration_sec for shot in storyboard.shots] == [5, 5, 5, 5]
    assert [shot.dialogue_speaker for shot in storyboard.shots] == [
        "char_1",
        "char_2",
        "char_3",
        "char_4",
    ]


def test_apply_duration_tier_lifts_shot_to_min_and_warns():
    """3 秒的镜头短于最短档位：反正也是花 5 秒的钱，就按 5 秒排。"""
    storyboard = storyboard_of(make_shot("s1", seconds=3))
    tier = DurationTier(min_sec=5, max_sec=15, source="provider")
    script = Script(scenes=[Scene(id="scene_1", heading="", duration_sec=3)])

    warnings = storyboard_service.apply_duration_tier(storyboard, script, tier)

    assert storyboard.shots[0].duration_sec == 5
    assert any("不够" in w and "最短" in w for w in warnings)


@pytest.mark.asyncio
async def test_storyboard_generation_uses_tier_from_provider(store):
    """有档位的 provider：生成阶段就直接「少而长」，每个镜头都 ≥ 最短档位。"""
    pipeline = media_pipeline(store, FakeReportProvider())
    project = await build_confirmed_project(store, pipeline)
    storyboard = store.load_storyboard(project.id)

    assert storyboard.duration_tier is not None
    assert storyboard.duration_tier.min_sec == 5
    assert storyboard.duration_tier.source == "provider"
    assert all(shot.duration_sec >= 5 for shot in storyboard.shots)
    # 假分镜 5 场 × 6 秒 → 每场 1 个镜头（未受档位约束时是每场 2 个）
    assert len(storyboard.shots) == 5
    assert storyboard.total_duration_sec == 30
    for scene_id in storyboard.scene_ids():
        assert len(storyboard.shots_for_scene(scene_id)) == 1


@pytest.mark.asyncio
async def test_storyboard_generation_survives_broken_video_provider(store):
    """视频 provider 建不起来（没 key / 没依赖）不能挡住分镜生成 —— 只是不受档位约束。"""

    def boom():
        raise RuntimeError("视频 provider 初始化失败（模拟没配 key）")

    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=boom,
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    project = await build_confirmed_project(store, pipeline)
    storyboard = store.load_storyboard(project.id)

    assert storyboard.duration_tier is None
    assert len(storyboard.shots) == 10  # 老行为


@pytest.mark.asyncio
async def test_duration_tier_falls_back_to_config_when_provider_unavailable(store, monkeypatch):
    """provider 问不到档位时，VIDEO_MIN_SHOT_SEC 仍能当兜底（配置 > 无约束）。"""
    monkeypatch.setattr(get_settings(), "video_min_shot_sec", 5.0)
    monkeypatch.setattr(get_settings(), "video_shot_target_sec", 8.0)

    def boom():
        raise RuntimeError("没有 key")

    pipeline = Pipeline(store, llm_factory=lambda: FakeLLM(), video_factory=boom)
    tier = await pipeline._duration_tier()

    assert tier is not None
    assert tier.min_sec == 5.0 and tier.source == "config"


@pytest.mark.asyncio
async def test_storyboard_generation_without_tier_keeps_old_behavior(store):
    pipeline = media_pipeline(store, FakeVideoProvider())
    project = await build_confirmed_project(store, pipeline)
    storyboard = store.load_storyboard(project.id)

    assert storyboard.duration_tier is None
    assert len(storyboard.shots) == 10  # 5 场 × 2 镜（原有行为不变）


# ── pipeline / API ────────────────────────────────────


def media_pipeline(store, video) -> Pipeline:
    return Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: video,
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )


@pytest.mark.asyncio
async def test_pipeline_preflight_and_merge_flow(store):
    # 先用**没有档位**的 provider 建项目（分镜是「多而短」的 10 镜），
    # 再换成有档位的 provider —— 模拟「先用 mock 跑通，再切真实模型」的真实流程
    pipeline = media_pipeline(store, FakeVideoProvider())
    project = await build_confirmed_project(store, pipeline)
    pipeline = media_pipeline(store, FakeReportProvider())

    plan = await pipeline.preflight(project.id, action="video")
    assert plan["video"]["to_generate"] > 0
    assert plan["video"]["min_sec"] == 5
    assert plan["audio"] is None  # 只问 video

    preview = await pipeline.merge_short_shots(project.id)
    assert preview["dry_run"] is True
    assert preview["to_shots"] < preview["from_shots"]
    before = store.load_storyboard(project.id)
    assert len(before.shots) == preview["from_shots"]  # 预览不写盘

    applied = await pipeline.merge_short_shots(project.id, dry_run=False)
    assert applied["purged_media"] is True
    after = store.load_storyboard(project.id)
    assert len(after.shots) == applied["to_shots"]
    assert store.load_project(project.id).status == ProjectStatus.STORYBOARD_CONFIRMED


@pytest.mark.asyncio
async def test_merge_requires_force_when_clips_are_paid_for(store):
    pipeline = media_pipeline(store, FakeVideoProvider())
    project = await build_confirmed_project(store, pipeline)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)
    assert store.load_video_manifest(project.id).ready_clips
    # 换成有档位的模型（最短 5 秒）→ 现在的分镜有一堆 3 秒镜头可以合并
    pipeline = media_pipeline(store, FakeReportProvider())

    preview = await pipeline.merge_short_shots(project.id)
    assert preview["force_required"] is True

    with pytest.raises(PipelineError) as excinfo:
        await pipeline.merge_short_shots(project.id, dry_run=False)
    assert "作废" in str(excinfo.value)

    await pipeline.merge_short_shots(project.id, dry_run=False, force=True)
    assert store.load_video_manifest(project.id) is None  # 已付费片段被作废（用户确认过）
    assert not(store.project_dir(project.id) / "clips").exists()


@pytest.mark.asyncio
async def test_merge_min_sec_explicit_overrides_provider_tier(store):
    pipeline = media_pipeline(store, FakeReportProvider())
    project = await build_confirmed_project(store, pipeline)
    preview = await pipeline.merge_short_shots(project.id, min_sec=1)
    assert preview["min_sec"] == 1
    assert preview["to_shots"] == preview["from_shots"]  # 1 秒没人需要合并


@pytest.mark.asyncio
async def test_generate_video_uses_same_billing_basis_as_preflight(store):
    """preflight 说多少钱，预算门控就按多少钱拦 —— 两个口径必须一致。"""
    provider = FakeReportProvider()
    pipeline = media_pipeline(store, provider)
    project = await build_confirmed_project(store, pipeline)
    plan = await pipeline.preflight(project.id, action="video")
    expected = plan["video"]["estimated_usd"]

    settings = get_settings()
    original = settings.budget_usd
    try:
        settings.budget_usd = 0.0
        upcoming = await pipeline._estimate_video_usd(
            pipeline.store.load_storyboard(project.id).shots
        )
    finally:
        settings.budget_usd = original
    assert upcoming == pytest.approx(expected)


@pytest.mark.asyncio
async def test_audio_cost_is_recorded_when_text_changes(store):
    """LLM 改写文案后重新合成同一条配音，也要重新记账（同 id 重跑不能漏记）。"""
    llm = FakeLLM(voice_text="第一版")
    # 必须固定同一个 LLM 实例：这样才能在第二轮改文案，验证「同 id 重跑要重新记账」
    pipeline = Pipeline(
        store,
        llm_factory=lambda: llm,
        video_factory=lambda: CostReportingProvider(),
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    project = await build_confirmed_project(store, pipeline)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    def audio_entries():
        report = cost_service.load_report(store, project.id)
        return [e for e in report.entries if e.stage == CostStage.AUDIO]

    job = await pipeline.start_audio_generation(
        project.id, script_mode="narration", use_vision=False
    )
    await wait_for_job(pipeline, job.id)
    first = len(audio_entries())
    assert first == 2  # 配音文案的 LLM token + TTS 字符各一条

    # 强制重跑文案 -> 部分条目重新合成 -> 必须再记一条（char 数）
    llm.voice_text = "第二版"
    job = await pipeline.start_audio_generation(
        project.id, script_mode="narration", use_vision=False, force_script=True
    )
    await wait_for_job(pipeline, job.id)
    assert len(audio_entries()) == first + 2  # 又一条 LLM + 又一条 TTS（重跑就有新花费）
    assert all(clip.cost_recorded for clip in store.load_audio_manifest(project.id).ready_clips)
