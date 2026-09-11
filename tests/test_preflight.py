"""成本前置（preflight）与短镜头合并的测试。

这两件事都直接关系到「会不会白花钱」，所以用例的重点是**口径**：
计费秒数是否按模型档位算、换 provider 会不会重跑、预算够不够、合并有没有丢台词。
"""

from __future__ import annotations

import pytest

from tests.conftest import build_confirmed_project, wait_for_job
from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.cost import CostReport, CostStage
from videogen.models.project import Project, ProjectStatus
from videogen.models.storyboard import Shot, Storyboard
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
    provider = FakeReportProvider()
    pipeline = media_pipeline(store, provider)
    project = await build_confirmed_project(store, pipeline)

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
    provider = FakeReportProvider()
    pipeline = media_pipeline(store, provider)
    project = await build_confirmed_project(store, pipeline)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)
    assert store.load_video_manifest(project.id).ready_clips

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
