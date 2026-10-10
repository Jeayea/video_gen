"""开场关键帧：只认首帧的视频模型（Kling / Wan）锁角色的那条路。"""

from __future__ import annotations

import re

import pytest

from tests.fakes import FakeImageProvider, FakeVideoProvider
from videogen.models.bible import Setting
from videogen.models.project import Project
from videogen.models.storyboard import Shot, Storyboard
from videogen.services import cost_service, keyframe_service, preflight_service, video_service
from videogen.services.storyboard_service import Segment

_CJK = re.compile(r"[一-鿿]")


class FirstFrameVideo(FakeVideoProvider):
    """像 Kling / Wan：不收参考图，只认首帧。"""

    def __init__(self, **kwargs):
        super().__init__(supports_reference_image=True, **kwargs)

    async def frame_mode(self) -> str:
        return "first_frame"


def _shots() -> list[Shot]:
    return [
        Shot(
            id="scene_1_shot_1",
            scene_id="scene_1",
            shot_no=1,
            duration_sec=3,
            character_in_frame=["char_1"],
            location="loc_1",
            video_prompt="A young woman sits by the window",
            dialogue_speaker="char_1",
            dialogue_text="我不会再回来了。",
            segment_id="seg_1",
        ),
        Shot(
            id="scene_1_shot_2",
            scene_id="scene_1",
            shot_no=2,
            duration_sec=3,
            character_in_frame=["char_2"],
            location="loc_1",
            video_prompt="Another woman smirks at the door",
            segment_id="seg_1",
        ),
    ]


@pytest.fixture
def setup(store, bible):
    project = Project(title="关键帧")
    store.save_project(project)
    project_dir = store.ensure_project_dir(project.id)
    refs = store.refs_dir(project.id)
    refs.mkdir(parents=True, exist_ok=True)
    for name in ("char_1.png", "char_2.png", "loc_1.png"):
        (refs / name).write_bytes(name.encode())
    bible.characters[0].reference_image = "refs/char_1.png"
    bible.characters[1].reference_image = "refs/char_2.png"
    bible.settings = [
        Setting(id="loc_1", name="出租屋", reference_image="refs/loc_1.png")
    ]
    store.save_bible(project.id, bible)
    store.save_storyboard(project.id, Storyboard(shots=_shots()))
    return project, project_dir, bible


def _segment() -> Segment:
    return Segment(id="seg_1", scene_id="scene_1", shots=_shots())


def test_plan_uses_first_shot_refs_and_english_prompt(setup):
    project, project_dir, bible = setup
    jobs = keyframe_service.plan_jobs(
        [_segment()], project, bible, project_dir, FirstFrameVideo()
    )
    assert len(jobs) == 1
    names = [path.name for path in jobs[0].references]
    # 段里后面才出场的 char_2 不进开场帧
    assert names == ["char_1.png", "loc_1.png"]
    prompt = jobs[0].prompt
    assert "A young woman sits by the window" in prompt
    assert not _CJK.search(prompt)  # 台词 / 名字一个中文字都不许有
    assert "lip-sync" not in prompt
    assert jobs[0].cached is False


def test_no_references_means_no_keyframe(setup):
    project, project_dir, bible = setup
    for character in bible.characters:
        character.reference_image = ""
    bible.settings[0].reference_image = ""
    assert keyframe_service.plan_jobs(
        [_segment()], project, bible, project_dir, FirstFrameVideo()
    ) == []


@pytest.mark.asyncio
async def test_keyframer_generates_records_and_caches(store, setup):
    project, _project_dir, bible = setup
    image = FakeImageProvider(actual_cost_usd=0.04)
    keyframer = keyframe_service.Keyframer(
        store, project.id, project, bible, image_provider=image
    )
    video = FirstFrameVideo()

    first = await keyframer.first_frame(_segment(), video)
    assert first is not None and first.is_file()
    assert first.parent.name == keyframe_service.KEYFRAME_DIR
    request = image.requests[0]
    assert [p.name for p in request.all_references()] == ["char_1.png", "loc_1.png"]
    assert request.aspect_ratio == project.aspect_ratio.value
    report = cost_service.load_report(store, project.id)
    assert sum(e.usd for e in report.entries) == pytest.approx(0.04)

    # 第二次：同样的 prompt + 参考图 → 复用，不再花钱
    again = await keyframer.first_frame(_segment(), video)
    assert again == first
    assert len(image.requests) == 1
    assert sum(e.usd for e in cost_service.load_report(store, project.id).entries) == (
        pytest.approx(0.04)
    )

    # 换了定妆照 → 缓存失效，重画
    (store.refs_dir(project.id) / "char_1.png").write_bytes(b"new face")
    await keyframer.first_frame(_segment(), video)
    assert len(image.requests) == 2


@pytest.mark.asyncio
async def test_keyframe_failure_degrades_to_text_only(store, setup):
    project, _project_dir, bible = setup
    image = FakeImageProvider(fail_subjects={"seg_1"})
    keyframer = keyframe_service.Keyframer(
        store, project.id, project, bible, image_provider=image
    )
    assert await keyframer.first_frame(_segment(), FirstFrameVideo()) is None
    assert cost_service.load_report(store, project.id).entries == []


@pytest.mark.asyncio
async def test_generate_clips_sends_keyframe_as_first_frame(store, setup, monkeypatch, settings):
    project, project_dir, bible = setup
    monkeypatch.setattr(settings, "video_segment_mode", True)
    image = FakeImageProvider()
    keyframer = keyframe_service.Keyframer(
        store, project.id, project, bible, image_provider=image
    )
    video = FirstFrameVideo()
    await video_service.generate_clips(
        project,
        store.load_storyboard(project.id),
        video,
        store.clips_dir(project.id),
        project_dir,
        bible=bible,
        retry_backoff_sec=0,
        keyframer=keyframer,
    )
    assert video.requests, "视频模型应该被调用"
    first = video.requests[0].first_frame
    assert first is not None and first.parent.name == keyframe_service.KEYFRAME_DIR


@pytest.mark.asyncio
async def test_enabled_only_for_first_frame_models(settings, monkeypatch):
    assert await keyframe_service.enabled_for(FirstFrameVideo(), settings)
    assert not await keyframe_service.enabled_for(
        FakeVideoProvider(supports_reference_image=True), settings
    )
    monkeypatch.setattr(settings, "video_keyframe", "off")
    assert not await keyframe_service.enabled_for(FirstFrameVideo(), settings)


@pytest.mark.asyncio
async def test_preflight_counts_keyframes(store, setup):
    project, project_dir, bible = setup
    video = FirstFrameVideo(price_usd_per_sec=0.1)
    storyboard = store.load_storyboard(project.id)

    async def estimator(segments):
        return await keyframe_service.estimate(segments, project, bible, project_dir, video)

    plan = await preflight_service.video_plan(
        storyboard, None, video, keyframe_estimator=estimator
    )
    assert plan["keyframes"]["to_generate"] == 1
    # 6 秒 × $0.1 + 1 张关键帧 $0.04
    assert plan["estimated_usd"] == pytest.approx(0.64)
    assert any("关键帧" in warning for warning in plan["warnings"])


# ── 走 Pipeline：预算门控 / preflight / 真正生成用同一口径 ─────────────


def _give_everyone_a_portrait(store, project_id):
    bible = store.load_bible(project_id)
    refs = store.refs_dir(project_id)
    refs.mkdir(parents=True, exist_ok=True)
    for character in bible.characters:
        (refs / f"{character.id}.png").write_bytes(character.id.encode())
        character.reference_image = f"refs/{character.id}.png"
    store.save_bible(project_id, bible)


@pytest.mark.asyncio
async def test_pipeline_draws_keyframes_and_bills_them(store, ready_project, fake_llm):
    from tests.test_video import build_pipeline, wait_job

    _give_everyone_a_portrait(store, ready_project.id)
    video = FirstFrameVideo(price_usd_per_sec=0.0)
    pipeline = build_pipeline(store, fake_llm, video)

    preflight = await pipeline.preflight(ready_project.id, action="video")
    expected = preflight["video"]["keyframes"]["to_generate"]
    assert expected > 0

    job = await pipeline.start_video_generation(ready_project.id)
    assert (await wait_job(pipeline, job.id)).status.value == "succeeded"

    with_frame = [r for r in video.requests if r.first_frame is not None]
    assert len(with_frame) == expected
    image_usd = sum(
        e.usd for e in cost_service.load_report(store, ready_project.id).entries
        if e.stage.value == "image"
    )
    assert image_usd == pytest.approx(preflight["video"]["keyframes"]["estimated_usd"])

    # 再看一次账：关键帧都在缓存里了，不会再算钱
    again = await pipeline.preflight(ready_project.id, action="video")
    assert not again["video"]["keyframes"] or again["video"]["keyframes"]["to_generate"] == 0


@pytest.mark.asyncio
async def test_budget_gate_includes_keyframes(
    store, ready_project, fake_llm, settings, monkeypatch
):
    from tests.test_video import build_pipeline
    from videogen.services.cost_service import BudgetExceeded

    _give_everyone_a_portrait(store, ready_project.id)
    # 视频本身 $0，只有关键帧要钱：预算 1 美分装不下
    monkeypatch.setattr(settings, "budget_usd", 0.01)
    pipeline = build_pipeline(store, fake_llm, FirstFrameVideo(price_usd_per_sec=0.0))
    with pytest.raises(BudgetExceeded):
        await pipeline.start_video_generation(ready_project.id)
