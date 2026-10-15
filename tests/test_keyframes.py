"""开场关键帧：只认首帧的视频模型（Kling / Wan）锁角色的那条路。"""

from __future__ import annotations

import re

import pytest

from tests.fakes import FakeImageProvider, FakeVideoProvider
from videogen.models.bible import Setting
from videogen.models.project import Project
from videogen.models.storyboard import Shot, Storyboard
from videogen.orchestrator.pipeline import PipelineError
from videogen.providers.video.base import VideoReferenceRejected
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
async def test_keyframe_failure_interrupts_instead_of_text_only(store, setup):
    """回归：关键帧画失败时以前悄悄改成纯文生视频（一致性没了，钱照花）。"""
    project, _project_dir, bible = setup
    image = FakeImageProvider(fail_subjects={"seg_1"})
    keyframer = keyframe_service.Keyframer(
        store, project.id, project, bible, image_provider=image
    )
    with pytest.raises(video_service.KeyframeUnavailable):
        await keyframer.first_frame(_segment(), FirstFrameVideo())
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


# ── 首帧被视频模型拒收：中断、交给用户决定（不自动去图重试）──────────


class RejectingFirstFrameVideo(FirstFrameVideo):
    """像 Seedance：写实人脸的首帧被判成隐私信息，带首帧就 400。"""

    async def generate(self, request):
        if request.first_frame is not None:
            self.requests.append(request)
            raise VideoReferenceRejected(
                "视频模型拒收了首帧（InputImageSensitiveContentDetected.PrivacyInformation）",
                kind="first_frame",
                code="InputImageSensitiveContentDetected.PrivacyInformation",
            )
        return await super().generate(request)


@pytest.mark.asyncio
async def test_rejected_first_frame_stops_and_waits_for_user(
    store, ready_project, fake_llm, settings, monkeypatch
):
    from tests.test_video import build_pipeline, wait_job

    _give_everyone_a_portrait(store, ready_project.id)
    monkeypatch.setattr(settings, "video_concurrency", 1)
    video = RejectingFirstFrameVideo(price_usd_per_sec=0.0)
    pipeline = build_pipeline(store, fake_llm, video)
    pid = ready_project.id

    job = await pipeline.start_video_generation(pid)
    finished = await wait_job(pipeline, job.id)
    assert finished.status.value == "failed"
    assert "拒收" in finished.error

    manifest = store.load_video_manifest(pid)
    assert len(manifest.rejections) == 1
    rejection = manifest.rejections[0]
    assert rejection.kind == "first_frame"
    assert rejection.code.endswith("PrivacyInformation")
    assert rejection.images and rejection.images[0].startswith(keyframe_service.KEYFRAME_DIR)
    # 只提交了一次：同样的图不重试，也没有悄悄去掉首帧再交一次，后面的段也没开工
    assert len(video.requests) == 1
    assert all(r.first_frame is not None for r in video.requests)
    assert not manifest.ready_clips

    # 不处理就不许再开工
    with pytest.raises(PipelineError):
        await pipeline.start_video_generation(pid)

    # 用户选「这段不带图」：只有这一段不带首帧
    unit = rejection.unit_id
    await pipeline.resolve_reference_rejection(pid, unit, "without_reference")
    assert store.load_video_manifest(pid).reference_waived == [unit]
    video.requests.clear()
    job = await pipeline.start_video_generation(pid)
    await wait_job(pipeline, job.id)
    after = store.load_video_manifest(pid)
    waived_shots = set(rejection.shot_ids)
    assert all(after.get(sid).status.value == "ready" for sid in waived_shots)
    assert [r.first_frame for r in video.requests if r.shot_id == unit] == [None]
    assert unit not in after.reference_waived  # 只管这一次
    # 下一段照样先带首帧，照样被拒、照样停下来问
    if len(after.clips) > len(waived_shots):
        assert [r.unit_id for r in after.rejections] != [unit]
        others = [r for r in video.requests if r.shot_id != unit]
        assert others and all(r.first_frame is not None for r in others)


@pytest.mark.asyncio
async def test_resolve_regenerate_keyframe_forgets_cache(
    store, ready_project, fake_llm, settings, monkeypatch
):
    from tests.test_video import build_pipeline, wait_job

    _give_everyone_a_portrait(store, ready_project.id)
    monkeypatch.setattr(settings, "video_concurrency", 1)
    pipeline = build_pipeline(store, fake_llm, RejectingFirstFrameVideo(price_usd_per_sec=0.0))
    pid = ready_project.id
    await wait_job(pipeline, (await pipeline.start_video_generation(pid)).id)
    unit = store.load_video_manifest(pid).rejections[0].unit_id
    frames = store.ensure_project_dir(pid) / keyframe_service.KEYFRAME_DIR
    assert (frames / f"{unit}.json").is_file()

    with pytest.raises(PipelineError):
        await pipeline.resolve_reference_rejection(pid, unit, "nonsense")
    result = await pipeline.resolve_reference_rejection(pid, unit, "regenerate_keyframe")
    assert result["remaining"] == []
    assert not (frames / f"{unit}.json").exists()
    assert not list(frames.glob(f"{unit}.*"))
    manifest = store.load_video_manifest(pid)
    assert manifest.rejections == [] and manifest.reference_waived == []
    # 已经处理过的段不能再处理一次
    with pytest.raises(PipelineError):
        await pipeline.resolve_reference_rejection(pid, unit, "retry")


# ── 只画场景的开场帧（VIDEO_KEYFRAME_SUBJECT=scene）──────────────


def test_scene_only_keyframe_has_no_people(setup, settings, monkeypatch):
    project, project_dir, bible = setup
    monkeypatch.setattr(settings, "video_keyframe_subject", "scene")
    [job] = keyframe_service.plan_jobs([_segment()], project, bible, project_dir, FirstFrameVideo())
    assert [p.name for p in job.references] == ["loc_1.png"]  # 定妆照一张都不带
    assert "no people" in job.prompt
    assert "A young woman" not in job.prompt  # 不用以人为主语的 video_prompt
    assert not _CJK.search(job.prompt)

    # 没有场景图也照画（按文字锁房间）
    bible.settings[0].reference_image = ""
    [job] = keyframe_service.plan_jobs([_segment()], project, bible, project_dir, FirstFrameVideo())
    assert job.references == []


@pytest.mark.asyncio
async def test_scene_only_first_frame_tells_video_model_people_enter(
    store, setup, settings, monkeypatch
):
    project, project_dir, bible = setup
    monkeypatch.setattr(settings, "video_segment_mode", True)
    monkeypatch.setattr(settings, "video_keyframe_subject", "scene")
    keyframer = keyframe_service.Keyframer(
        store, project.id, project, bible, image_provider=FakeImageProvider()
    )
    video = FirstFrameVideo()
    await video_service.generate_clips(
        project, store.load_storyboard(project.id), video, store.clips_dir(project.id),
        project_dir, bible=bible, retry_backoff_sec=0, keyframer=keyframer,
    )
    [request] = video.requests
    assert request.first_frame is not None
    assert video_service.SCENE_OPENING_NOTE in request.prompt

    # 默认（锁人）模式不加这句
    monkeypatch.setattr(settings, "video_keyframe_subject", "characters")
    assert video_service.with_opening_frame_note("p", request.first_frame) == "p"


def test_appearance_anchor_adds_costume_missing_from_prompt(bible):
    shot = Shot(id="s", scene_id="scene_1", shot_no=1, duration_sec=3,
                character_in_frame=["char_1"], video_prompt="x")
    bible.characters[0].visual_prompt = (
        "A Chinese male high school student, around 17, wearing a blue-and-white tracksuit"
    )
    base = "A Chinese male high school student lifts his head"
    anchor = video_service.appearance_anchor(shot, bible, base.lower())
    assert "blue-and-white tracksuit" in anchor
    assert "A Chinese male high school student" not in anchor  # 已经写过的不重复
    full = bible.characters[0].visual_prompt.lower()
    assert "tracksuit" not in video_service.appearance_anchor(shot, bible, full)


def test_appearance_anchor_never_cuts_costume_mid_word(bible):
    # 真实事故：180 字符硬截成「royal blue body with white s」，白肩 / 袖条 / 白 T 从没发出去
    shot = Shot(id="s", scene_id="scene_1", shot_no=1, duration_sec=3,
                character_in_frame=["char_1"], video_prompt="x")
    bible.characters[0].visual_prompt = (
        "A Chinese male high school student, around 17 years old, with an average everyday "
        "Chinese face, determined expression, short black hair with a slightly messy fringe, "
        "wears the year-2000 Chinese high school uniform: a loose zip-up tracksuit jacket, "
        "royal blue body with white shoulders and two white stripes down each sleeve, over a "
        "plain white T-shirt, matching royal blue track pants, white canvas sneakers"
    )
    anchor = video_service.appearance_anchor(shot, bible, "a boy at a desk")
    for part in ("two white stripes down each sleeve", "plain white T-shirt",
                 "matching royal blue track pants", "white canvas sneakers"):
        assert part in anchor


def test_clip_clauses_drops_whole_parts_and_keeps_wardrobe():
    parts = ["a calm and thoughtful young person with a quiet presence", "wears a red scarf",
             "standing tall", "black hair in a bun"]
    text = video_service.clip_clauses(parts, limit=45)
    assert text == "wears a red scarf, black hair in a bun"
    assert video_service.clip_clauses(["word " * 30], limit=20).endswith("word")


@pytest.mark.asyncio
async def test_keyframe_failure_stops_segment(store, setup, monkeypatch, settings):
    """关键帧画不出来：这一段不买视频，记成待处理，等用户选重画 / 不带图。"""
    project, project_dir, bible = setup
    monkeypatch.setattr(settings, "video_segment_mode", True)
    keyframer = keyframe_service.Keyframer(
        store, project.id, project, bible, image_provider=FakeImageProvider(fail_subjects={"seg_1"})
    )
    video = FirstFrameVideo()
    manifest = await video_service.generate_clips(
        project,
        store.load_storyboard(project.id),
        video,
        store.clips_dir(project.id),
        project_dir,
        bible=bible,
        retry_backoff_sec=0,
        keyframer=keyframer,
    )
    assert video.requests == [], "关键帧没画出来就不该买视频"
    assert [r.kind for r in manifest.rejections] == [video_service.KEYFRAME_FAILED]
    assert all(c.status.value == "failed" for c in manifest.clips)


# ── 接戏帧（VIDEO_KEYFRAME_CHAIN）──────────────────────────


def _chain_shots() -> list[Shot]:
    def shot(shot_id: str, scene: str, no: int, location: str) -> Shot:
        return Shot(
            id=shot_id,
            scene_id=scene,
            shot_no=no,
            duration_sec=5,
            character_in_frame=["char_1"],
            location=location,
            video_prompt="A young woman walks across the room",
        )

    return [
        shot("scene_1_shot_1", "scene_1", 1, "loc_1"),
        shot("scene_1_shot_2", "scene_1", 2, "loc_1"),  # 同场同地点 → 接上一段
        shot("scene_1_shot_3", "scene_1", 3, "loc_2"),  # 换了地点 → 重新画
        shot("scene_2_shot_1", "scene_2", 1, "loc_1"),  # 换了场 → 重新画
    ]


def test_chain_predecessors_same_scene_same_location():
    from videogen.services.storyboard_service import chain_predecessors, segments_of

    segments = segments_of(Storyboard(shots=_chain_shots()))
    chain = chain_predecessors(segments)
    assert {k: v.id for k, v in chain.items()} == {"scene_1_shot_2": "scene_1_shot_1"}
    assert chain_predecessors(segments, waived=["scene_1_shot_2"]) == {}


@pytest.fixture
def fake_last_frame(monkeypatch):
    """假片段不是真视频：抽「最后一帧」时直接写个文件，并记下从哪个片段抽的。"""
    sources: list[str] = []

    async def extract(video, output):
        sources.append(video.name)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"last-frame")
        return output

    monkeypatch.setattr(keyframe_service, "extract_last_frame", extract)
    return sources


async def _run_chain(store, setup, video, settings, monkeypatch):
    project, project_dir, bible = setup
    monkeypatch.setattr(settings, "video_keyframe_chain", True)
    store.save_storyboard(project.id, Storyboard(shots=_chain_shots()))
    image = FakeImageProvider()
    keyframer = keyframe_service.Keyframer(
        store, project.id, project, bible, image_provider=image
    )
    manifest = await video_service.generate_clips(
        project,
        store.load_storyboard(project.id),
        video,
        store.clips_dir(project.id),
        project_dir,
        bible=bible,
        concurrency=4,
        retry_backoff_sec=0,
        keyframer=keyframer,
        existing=store.load_video_manifest(project.id),
    )
    store.save_video_manifest(project.id, manifest)
    return manifest, image


@pytest.mark.asyncio
async def test_chain_uses_previous_last_frame(store, setup, settings, monkeypatch, fake_last_frame):
    video = FirstFrameVideo(delay=0.02)
    manifest, image = await _run_chain(store, setup, video, settings, monkeypatch)

    assert all(clip.status.value == "ready" for clip in manifest.clips)
    frames = {r.shot_id: r.first_frame for r in video.requests}
    assert frames["scene_1_shot_2"].name == "scene_1_shot_2" + keyframe_service.CHAIN_SUFFIX
    for drawn in ("scene_1_shot_1", "scene_1_shot_3", "scene_2_shot_1"):
        assert frames[drawn].suffix == ".png"
    assert fake_last_frame == ["scene_1_shot_1.mp4"]
    # 接戏的段等上一段生成完才提交；只画了 3 张，接戏帧不花钱
    assert video.calls.index("scene_1_shot_2") > video.calls.index("scene_1_shot_1")
    assert len(image.requests) == 3
    chained = next(r for r in video.requests if r.shot_id == "scene_1_shot_2")
    assert video_service.CHAIN_OPENING_NOTE in chained.prompt


@pytest.mark.asyncio
async def test_chain_falls_back_to_drawn_keyframe_when_previous_failed(
    store, setup, settings, monkeypatch, fake_last_frame
):
    video = FirstFrameVideo(fail_shots={"scene_1_shot_1"}, fail_times=99)
    manifest, _image = await _run_chain(store, setup, video, settings, monkeypatch)
    assert manifest.get("scene_1_shot_1").status.value == "failed"
    frames = {r.shot_id: r.first_frame for r in video.requests}
    assert frames["scene_1_shot_2"].suffix == ".png"  # 上一段没有片段：照常画
    assert fake_last_frame == []


class RejectingChainVideo(FirstFrameVideo):
    """接戏帧（正脸）被拒，画出来的关键帧能过。"""

    async def generate(self, request):
        if request.first_frame and request.first_frame.name.endswith(
            keyframe_service.CHAIN_SUFFIX
        ):
            self.requests.append(request)
            raise VideoReferenceRejected(
                "视频模型拒收了首帧", kind="first_frame", code="PrivacyInformation"
            )
        return await super().generate(request)


@pytest.mark.asyncio
async def test_rejected_chain_frame_can_switch_to_drawn_keyframe(
    store, setup, settings, monkeypatch, fake_last_frame
):
    from tests.test_video import build_pipeline

    project, _project_dir, _bible = setup
    video = RejectingChainVideo()
    manifest, _image = await _run_chain(store, setup, video, settings, monkeypatch)
    rejection = manifest.rejections[0]
    assert rejection.unit_id == "scene_1_shot_2" and rejection.chained is True

    pipeline = build_pipeline(store, None, video)
    await pipeline.resolve_reference_rejection(project.id, "scene_1_shot_2", "regenerate_keyframe")
    assert store.load_video_manifest(project.id).chain_waived == ["scene_1_shot_2"]

    video.requests.clear()
    manifest, _image = await _run_chain(store, setup, video, settings, monkeypatch)
    assert manifest.get("scene_1_shot_2").status.value == "ready"
    redo = [r.first_frame.suffix for r in video.requests if r.shot_id == "scene_1_shot_2"]
    assert redo == [".png"]  # 不再接戏，改用画的关键帧


@pytest.mark.asyncio
async def test_preflight_does_not_charge_for_chain_frames(
    store, setup, settings, monkeypatch, fake_llm
):
    from tests.test_video import build_pipeline

    project, _project_dir, _bible = setup
    store.save_storyboard(project.id, Storyboard(shots=_chain_shots()))
    pipeline = build_pipeline(store, fake_llm, FirstFrameVideo())
    segments = keyframe_service.video_service.segments_of(store.load_storyboard(project.id))

    plan = await pipeline._keyframe_estimator(project, FirstFrameVideo())(segments)
    assert plan["to_generate"] == 4
    monkeypatch.setattr(settings, "video_keyframe_chain", True)
    plan = await pipeline._keyframe_estimator(project, FirstFrameVideo())(segments)
    assert plan["to_generate"] == 3 and plan["chained"] == 1


@pytest.mark.asyncio
async def test_extract_last_frame_from_real_video(tmp_path):
    from videogen.utils.ffmpeg import ffmpeg_available, run_ffmpeg, run_ffmpeg_bytes

    if not ffmpeg_available():
        pytest.skip("需要 ffmpeg")
    video = tmp_path / "clip.mp4"
    # 前 1 秒红、后 1 秒蓝：最后一帧应该是蓝的
    await run_ffmpeg([
        "-y", "-f", "lavfi", "-i", "color=red:s=64x64:d=1:r=24",
        "-f", "lavfi", "-i", "color=blue:s=64x64:d=1:r=24",
        "-filter_complex", "[0][1]concat=n=2:v=1", "-pix_fmt", "yuv420p", str(video),
    ])
    frame = await keyframe_service.extract_last_frame(video, tmp_path / "out" / "x.chain.jpg")
    assert frame is not None and frame.stat().st_size > 0
    pixel = await run_ffmpeg_bytes(
        ["-i", str(frame), "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    )
    red, _green, blue = pixel[:3]
    assert blue > 150 and red < 80, f"取到的不是最后一帧：rgb={tuple(pixel[:3])}"
    assert not list((tmp_path / "out").glob("*.part*"))
    assert await keyframe_service.extract_last_frame(tmp_path / "missing.mp4", frame) is None
