from __future__ import annotations

import asyncio
import json
import re

import pytest

from tests.fakes import AlwaysFailVideoProvider, FakeVideoProvider
from videogen.models.bible import Setting
from videogen.models.project import Project, ProjectStatus
from videogen.models.video import ClipStatus, MediaKind, VideoManifest
from videogen.orchestrator.pipeline import Pipeline, PipelineError
from videogen.services import video_service


def build_pipeline(store, llm, video_provider) -> Pipeline:
    return Pipeline(store, llm_factory=lambda: llm, video_factory=lambda: video_provider)


async def wait_job(pipeline: Pipeline, job_id: str, timeout: float = 10.0):
    """等后台任务结束。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = pipeline.jobs.get(job_id)
        if job and not job.is_active:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务 {job_id} 未在 {timeout}s 内结束")


async def run_clips(
    store,
    project,
    provider,
    *,
    existing: VideoManifest | None = None,
    **kwargs,
) -> VideoManifest:
    """直接驱动 video_service（并像 pipeline 一样落盘，便于测续跑）。"""
    storyboard = store.load_storyboard(project.id)
    if existing is None:
        existing = store.load_video_manifest(project.id)
    manifest = await video_service.generate_clips(
        project,
        storyboard,
        provider,
        store.clips_dir(project.id),
        store.ensure_project_dir(project.id),
        existing=existing,
        retry_backoff_sec=0,
        **kwargs,
    )
    store.save_video_manifest(project.id, manifest)
    return manifest


def shot_count(store, project) -> int:
    return len(store.load_storyboard(project.id).shots)


# ── service 层 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_generates_all_clips(store, ready_project):
    provider = FakeVideoProvider()
    manifest = await run_clips(store, ready_project, provider, concurrency=3)
    expected = shot_count(store, ready_project)

    assert len(manifest.clips) == expected
    assert len(manifest.ready_clips) == expected
    assert not manifest.failed_clips
    assert manifest.is_complete and manifest.is_usable
    assert manifest.provider == "fake"
    assert manifest.aspect_ratio == ready_project.aspect_ratio.value

    for clip in manifest.clips:
        assert clip.path and not clip.path.startswith("/")
        assert (store.ensure_project_dir(ready_project.id) / clip.path).exists()
        assert clip.status == ClipStatus.READY
        assert clip.media_kind == MediaKind.VIDEO
        assert clip.duration_sec > 0
    assert manifest.total_duration_sec == sum(c.duration_sec for c in manifest.clips)


@pytest.mark.asyncio
async def test_concurrency_is_bounded(store, ready_project):
    provider = FakeVideoProvider(delay=0.05)
    await run_clips(store, ready_project, provider, concurrency=2)
    assert provider.max_active <= 2, "并发不能突破上限"
    assert provider.max_active >= 2, "应该真的并发跑，而不是串行"


@pytest.mark.asyncio
async def test_retry_then_success(store, ready_project):
    storyboard = store.load_storyboard(ready_project.id)
    first_shot = storyboard.shots[0].id
    provider = FakeVideoProvider(fail_shots={first_shot}, fail_times=1)

    manifest = await run_clips(store, ready_project, provider, max_retries=2)

    clip = manifest.get(first_shot)
    assert clip.status == ClipStatus.READY
    assert clip.attempts == 2
    assert provider.attempts_for(first_shot) == 2
    assert not manifest.failed_clips


@pytest.mark.asyncio
async def test_failure_is_isolated_to_one_shot(store, ready_project):
    storyboard = store.load_storyboard(ready_project.id)
    first_shot = storyboard.shots[0].id
    provider = FakeVideoProvider(fail_shots={first_shot}, fail_times=99)

    manifest = await run_clips(store, ready_project, provider, max_retries=1)

    assert [c.shot_id for c in manifest.failed_clips] == [first_shot]
    assert manifest.failed_clips[0].attempts == 2
    assert "fake 生成失败" in manifest.failed_clips[0].error
    assert len(manifest.ready_clips) == shot_count(store, ready_project) - 1
    assert not manifest.is_complete
    assert not (store.clips_dir(ready_project.id) / f"{first_shot}.mp4").exists()


@pytest.mark.asyncio
async def test_resume_skips_ready_clips(store, ready_project):
    provider = FakeVideoProvider()
    await run_clips(store, ready_project, provider)
    assert len(provider.calls) == shot_count(store, ready_project)

    again = FakeVideoProvider()
    manifest = await run_clips(store, ready_project, again)  # 自动读取已落盘的 manifest
    assert again.calls == []
    assert len(manifest.ready_clips) == shot_count(store, ready_project)


@pytest.mark.asyncio
async def test_resume_only_regenerates_failed(store, ready_project):
    storyboard = store.load_storyboard(ready_project.id)
    first_shot = storyboard.shots[0].id
    failing = FakeVideoProvider(fail_shots={first_shot}, fail_times=99)
    manifest = await run_clips(store, ready_project, failing, max_retries=0)
    assert len(manifest.failed_clips) == 1

    healthy = FakeVideoProvider()
    manifest2 = await run_clips(store, ready_project, healthy)
    assert healthy.calls == [first_shot]  # 只补失败的那一个
    assert len(manifest2.ready_clips) == shot_count(store, ready_project)


@pytest.mark.asyncio
async def test_switching_provider_regenerates_ready_clips(store, ready_project):
    """mock → 真实模型时不能沿用旧占位片段（否则看起来像换了 provider 没生效）。"""
    first = FakeVideoProvider()
    await run_clips(store, ready_project, first)
    assert len(first.calls) == shot_count(store, ready_project)

    switched = FakeVideoProvider()
    switched.name = "openrouter"
    switched.model = "minimax/hailuo-3"
    manifest = await run_clips(store, ready_project, switched)

    assert len(switched.calls) == shot_count(store, ready_project), "换了 provider 必须全部重跑"
    assert manifest.provider == "openrouter"
    assert manifest.model == "minimax/hailuo-3"
    assert len(manifest.ready_clips) == shot_count(store, ready_project)


@pytest.mark.asyncio
async def test_switching_model_regenerates_ready_clips(store, ready_project):
    first = FakeVideoProvider()
    first.model = "minimax/hailuo-3"
    await run_clips(store, ready_project, first)

    other = FakeVideoProvider()
    other.model = "minimax/hailuo-3-max"
    await run_clips(store, ready_project, other)
    assert len(other.calls) == shot_count(store, ready_project)


@pytest.mark.asyncio
async def test_placeholder_is_flagged(store, ready_project):
    provider = FakeVideoProvider(media_kind=MediaKind.PLACEHOLDER)
    manifest = await run_clips(store, ready_project, provider)

    assert len(manifest.ready_clips) == shot_count(store, ready_project)
    assert manifest.has_placeholders
    assert not manifest.is_usable  # 占位文件不能进合成
    assert manifest.summary()["placeholder"] == shot_count(store, ready_project)


def test_build_prompt_appends_constraints(store, ready_project):
    shot = store.load_storyboard(ready_project.id).shots[0]
    prompt = video_service.build_prompt(shot, ready_project)
    assert ready_project.aspect_ratio.value in prompt
    assert f"{shot.duration_sec} seconds" in prompt


def test_build_prompt_injects_bible_appearance_for_consistency(store, ready_project, bible):
    """在场角色的圣经外貌要确定性地进入每个镜头 prompt（脸不统一的文本侧兜底）。"""
    storyboard = store.load_storyboard(ready_project.id)
    shot = next(s for s in storyboard.shots if s.character_in_frame)
    char_id = shot.character_in_frame[0]
    character = bible.get_character(char_id)
    shot.video_prompt = "A medium shot of a young woman walking"  # 故意不写外貌

    prompt = video_service.build_prompt(shot, ready_project, bible)

    assert character.visual_prompt.split(",")[0][:30] in prompt
    assert "consistent face" in prompt
    # 但**名字不能进 prompt**：中文名会被模型画成画面上的文字（v0.17 修）
    assert character.name not in prompt


def test_appearance_anchor_skips_descriptions_already_in_the_prompt(store, ready_project, bible):
    storyboard = store.load_storyboard(ready_project.id)
    shot = next(s for s in storyboard.shots if s.character_in_frame)
    character = bible.get_character(shot.character_in_frame[0])
    # 分镜 LLM 已经抄过外貌 -> 不要重复拼一遍
    shot.video_prompt = f"Close-up. {character.visual_prompt}"

    prompt = video_service.build_prompt(shot, ready_project, bible)
    assert prompt.count(character.visual_prompt.split(",")[0][:30]) == 1


@pytest.mark.asyncio
async def test_resolve_references_returns_every_in_frame_character(store, tmp_path, bible):
    """双人镜头要同时锁两个人：去重、按 character_in_frame 顺序、越界/缺失文件跳过。"""
    from videogen.models.storyboard import Shot
    from videogen.providers.video.mock import MockVideoProvider

    project_dir = store.ensure_project_dir("p1")
    refs = store.refs_dir("p1")
    (refs / "char_1.png").write_bytes(b"a")
    (refs / "char_2.png").write_bytes(b"b")
    characters = {c.id: c for c in bible.characters}
    characters["char_1"] = characters["char_1"].model_copy(
        update={"reference_image": "refs/char_1.png"}
    )
    characters["char_2"] = characters["char_2"].model_copy(
        update={"reference_image": "refs/char_2.png"}
    )
    characters["char_3"] = characters["char_3"].model_copy(
        update={"reference_image": "../outside.png"}  # 越界，必须被忽略
    )
    characters["char_4"] = characters["char_4"].model_copy(
        update={"reference_image": "refs/missing.png"}  # 文件不存在，必须被忽略
    )
    bible.characters = [characters[c.id] for c in bible.characters]

    provider = MockVideoProvider()
    shot = Shot(
        id="s1",
        scene_id="scene_1",
        shot_no=1,
        duration_sec=3,
        character_in_frame=["char_2", "char_3", "char_4", "char_1", "char_2"],
    )
    paths, _seed = video_service.resolve_references(shot, bible, project_dir, provider)
    assert [p.name for p in paths] == ["char_2.png", "char_1.png"]  # 顺序 + 去重

    single, _ = video_service.resolve_reference(shot, bible, project_dir, provider)
    assert single is not None and single.name == "char_2.png"  # 老接口仍只取第一张


def test_build_prompt_strips_llm_label_prefix(store, ready_project):
    """真实样本：分镜 LLM 把 `English prompt:` 也写进了 video_prompt。"""
    shot = store.load_storyboard(ready_project.id).shots[0]
    shot.video_prompt = "English prompt: A woman walks into the office"
    prompt = video_service.build_prompt(shot, ready_project)
    assert prompt.startswith("A woman walks into the office")
    assert "English prompt" not in prompt

    shot.video_prompt = "Prompt: 英文提示词： A man turns around"
    assert video_service.build_prompt(shot, ready_project).startswith("A man turns around")


def test_build_prompt_translates_camera_and_mood_to_english(store, ready_project):
    """`camera`/`mood` 是中文：前者原来**根本没进 prompt**（运镜信息全丢），
    后者被直接拼进英文 prompt（模型读不懂，还有被画进画面的风险）。"""
    shot = store.load_storyboard(ready_project.id).shots[0]
    shot.camera = "中景 · 缓推"
    shot.mood = "震惊，失落"
    prompt = video_service.build_prompt(shot, ready_project)

    assert "medium shot" in prompt
    assert "slow dolly in" in prompt
    assert "shocked" in prompt and "crestfallen" in prompt
    # 唯一允许出现在英文 prompt 里的中文是**引号里的台词**（刻意给模型对口型用的）
    outside_quotes = re.sub(r'"[^"]*"', "", prompt)
    assert not re.search(r"[\u4e00-\u9fff]", outside_quotes), prompt


def test_build_prompt_carries_the_spoken_line_for_lip_sync(
    store, ready_project, settings, monkeypatch
):
    """口型对不上的根因：prompt 里没有台词，模型自己编一段话在说。

    台词进 prompt 后，原生表演与我们的配音才落在同一句话上。
    """
    shot = store.load_storyboard(ready_project.id).shots[0]
    shot.dialogue_text = "薇儿，为什么？"
    shot.dialogue_speaker = ""
    prompt = video_service.build_prompt(shot, ready_project)
    assert "薇儿，为什么？" in prompt
    assert "lip-sync" in prompt
    assert "no on-screen text" in prompt

    # 可以整体关掉（老行为：模型自己编话）
    monkeypatch.setattr(settings, "video_prompt_dialogue", False)
    assert "薇儿，为什么？" not in video_service.build_prompt(shot, ready_project)


def test_build_prompt_injects_location_background_anchor(store, ready_project, bible):
    """背景不够丰富：场景的环境描述以前从来没进过 prompt（分镜只写 "in an office"）。"""
    shot = store.load_storyboard(ready_project.id).shots[0]
    shot.video_prompt = "a woman walks through an office"
    bible.settings = [
        Setting(
            id="loc_1",
            name="杂志社办公室",
            visual_prompt="A bustling modern office with glass partitions",
        )
    ]
    shot.location = "loc_1"
    prompt = video_service.build_prompt(shot, ready_project, bible)
    assert "A bustling modern office with glass partitions" in prompt
    assert "setting:" in prompt

    # 背景丰富度规范也必须在（空墙是模型偷懒的默认解）
    assert "richly detailed lived-in environment" in prompt
    assert "no empty blank walls" in prompt

    # 没填 location 时不硬塞别的场景（宁可没有锚点，也不要张冠李戴）
    shot.location = ""
    assert "A bustling modern office" not in video_service.build_prompt(
        shot, ready_project, bible
    )


def test_location_anchor_skips_description_already_in_prompt(store, ready_project, bible):
    shot = store.load_storyboard(ready_project.id).shots[0]
    bible.settings = [
        Setting(
            id="loc_1",
            name="杂志社办公室",
            visual_prompt="A bustling modern office with glass partitions",
        )
    ]
    shot.location = "loc_1"
    base = "a woman types at a desk, a bustling modern office with glass partitions"
    assert video_service.location_anchor(shot, bible, base) == ""


def test_build_prompt_appends_performance_and_style_directive(
    store, ready_project, settings, monkeypatch
):
    """表情自然/动作流畅是靠这段统一规范兜的（可在 .env 覆盖）。"""
    shot = store.load_storyboard(ready_project.id).shots[0]
    prompt = video_service.build_prompt(shot, ready_project)
    assert "subtle expressions" in prompt
    assert "no cuts" in prompt
    assert "no morphing" in prompt

    monkeypatch.setattr(settings, "video_style_prompt", "cinematic realism, muted colors")
    styled = video_service.build_prompt(shot, ready_project)
    assert "cinematic realism" in styled


def test_camera_and_mood_directives_drop_unknown_tokens():
    assert video_service.camera_directive("") == ""
    assert video_service.camera_directive("看不懂的景别") == ""
    # 没收录的中文情绪词直接丢弃，绝不把中文塞进英文 prompt
    assert video_service.mood_directive("玄之又玄") == ""
    assert video_service.mood_directive("determined") == "determined"


def test_appearance_anchor_is_empty_without_bible(store, ready_project):
    shot = store.load_storyboard(ready_project.id).shots[0]
    assert video_service.appearance_anchor(shot, None, "x") == ""


# ── pipeline / job 层 ─────────────────────────────────


@pytest.mark.asyncio
async def test_video_job_end_to_end(video_pipeline, store, ready_project):
    expected = shot_count(store, ready_project)
    job = await video_pipeline.start_video_generation(ready_project.id)
    assert job.kind == "video"
    assert store.load_project(ready_project.id).status == ProjectStatus.VIDEO_GENERATING

    finished = await wait_job(video_pipeline, job.id)
    assert finished.status.value == "succeeded"
    assert finished.progress == finished.total == expected
    assert finished.percent == 100

    assert store.load_project(ready_project.id).status == ProjectStatus.VIDEO_READY
    manifest = store.load_video_manifest(ready_project.id)
    assert len(manifest.ready_clips) == expected
    assert manifest.summary()["usable"] is True


@pytest.mark.asyncio
async def test_video_job_partial_failure_still_ready(store, ready_project, fake_llm):
    expected = shot_count(store, ready_project)
    failing = FakeVideoProvider(fail_shots={store.load_storyboard(ready_project.id).shots[0].id},
                                fail_times=99)
    pipeline = build_pipeline(store, fake_llm, failing)

    job = await pipeline.start_video_generation(ready_project.id)
    finished = await wait_job(pipeline, job.id)

    assert finished.status.value == "succeeded"
    assert store.load_project(ready_project.id).status == ProjectStatus.VIDEO_READY
    manifest = store.load_video_manifest(ready_project.id)
    assert len(manifest.failed_clips) == 1
    assert len(manifest.ready_clips) == expected - 1
    assert "失败 1" in finished.message


@pytest.mark.asyncio
async def test_video_job_all_failed_fails_job_and_unblocks(store, ready_project, fake_llm):
    pipeline = build_pipeline(store, fake_llm, AlwaysFailVideoProvider())

    job = await pipeline.start_video_generation(ready_project.id)
    finished = await wait_job(pipeline, job.id)

    assert finished.status.value == "failed"
    assert "全部" in finished.error
    # 退回可重试状态，而不是卡在 VIDEO_GENERATING
    assert store.load_project(ready_project.id).status == ProjectStatus.STORYBOARD_CONFIRMED


@pytest.mark.asyncio
async def test_video_requires_confirmed_storyboard(store, pipeline, fake_llm):
    project = Project(input_text="x", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)
    await pipeline.generate_storyboard(project.id)  # 停在 STORYBOARD_REVIEW

    video_pipeline = build_pipeline(store, fake_llm, FakeVideoProvider())
    with pytest.raises(PipelineError):
        await video_pipeline.start_video_generation(project.id)


@pytest.mark.asyncio
async def test_duplicate_video_job_is_rejected(store, ready_project, fake_llm):
    pipeline = build_pipeline(store, fake_llm, FakeVideoProvider(delay=0.2))

    job = await pipeline.start_video_generation(ready_project.id)
    with pytest.raises(PipelineError) as exc:
        await pipeline.start_video_generation(ready_project.id)
    assert "已有视频生成任务" in str(exc.value)

    await pipeline.jobs.cancel(job.id)
    await wait_job(pipeline, job.id)


@pytest.mark.asyncio
async def test_cancel_video_job(store, ready_project, fake_llm):
    pipeline = build_pipeline(store, fake_llm, FakeVideoProvider(delay=0.3))

    job = await pipeline.start_video_generation(ready_project.id)
    await asyncio.sleep(0.05)  # 让任务真的跑起来
    assert await pipeline.cancel_video_generation(ready_project.id) == 1

    finished = await wait_job(pipeline, job.id)
    assert finished.status.value == "cancelled"
    assert store.load_project(ready_project.id).status == ProjectStatus.STORYBOARD_CONFIRMED
    assert list(store.clips_dir(ready_project.id).glob("*.mp4")) == []  # 不留半截文件


@pytest.mark.asyncio
async def test_new_job_waits_for_cancelled_job_to_finish_cleanup(store, ready_project, fake_llm):
    """回归：取消后立刻再点生成，新任务和旧任务的收尾交错（旧收尾覆盖新状态）。"""
    pipeline = build_pipeline(store, fake_llm, FakeVideoProvider(delay=0.3))
    job = await pipeline.start_video_generation(ready_project.id)
    await asyncio.sleep(0.05)
    await pipeline.jobs.cancel(job.id)
    assert pipeline.jobs.active_for(ready_project.id) is None  # 状态已经是 cancelled
    with pytest.raises(PipelineError) as exc:
        await pipeline.start_video_generation(ready_project.id)
    assert "收尾" in str(exc.value)
    await wait_job(pipeline, job.id)
    assert await wait_until(lambda: pipeline.jobs.busy_for(ready_project.id) is None)
    job2 = await pipeline.start_video_generation(ready_project.id)
    await wait_job(pipeline, job2.id)


async def wait_until(predicate, timeout: float = 10.0) -> bool:
    """等某个条件成立（比固定 sleep 稳：并发度、调度顺序都不影响结论）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_cancelled_video_job_still_records_cost(store, ready_project, fake_llm):
    """取消也要记账：已经落盘的片段 provider 已经按秒计过费了。

    真实事故：9 镜里跑完 3 镜时用户取消，片段随后被删掉，$1.95 一条都没进账本
    —— 账本少一笔比多一笔危险得多（预算是按账本放行的）。
    """
    provider = FakeVideoProvider(delay=0.3, actual_cost_usd=0.65)
    pipeline = build_pipeline(store, fake_llm, provider)

    def ready_count() -> int:
        manifest = store.load_video_manifest(ready_project.id)
        return len(manifest.ready_clips) if manifest else 0

    def video_usd() -> float:
        entries = pipeline.cost_report(ready_project.id).entries
        return sum(e.usd for e in entries if e.stage == "video")

    job = await pipeline.start_video_generation(ready_project.id)
    assert await wait_until(lambda: ready_count() >= 1), "一个片段都没跑完，无法验证记账"
    assert await pipeline.cancel_video_generation(ready_project.id) == 1
    finished = await wait_job(pipeline, job.id)
    assert finished.status.value == "cancelled"

    # 取消是「先落状态、后收尾」的（jobs.cancel 立刻置 cancelled 再 cancel 任务），
    # 记账在协程收尾时补上 —— 所以这里等它落地，而不是假设已经落地
    assert await wait_until(lambda: video_usd() > 0), "取消后视频花费没进账本"

    manifest = store.load_video_manifest(ready_project.id)
    ready = manifest.ready_clips
    assert video_usd() == pytest.approx(len(ready) * 0.65)
    assert all(c.cost_recorded for c in ready)  # 已打标记，续跑不会重复计

    # 续跑跑完剩下的镜头：账本只应该多出「新生成的」那部分，不能重复计
    provider.delay = 0.0
    assert await wait_until(lambda: pipeline.jobs.busy_for(ready_project.id) is None)
    job2 = await pipeline.start_video_generation(ready_project.id)
    await wait_job(pipeline, job2.id)
    assert video_usd() == pytest.approx(len(manifest.clips) * 0.65)


@pytest.mark.asyncio
async def test_video_status_shape(video_pipeline, store, ready_project):
    status = video_pipeline.video_status(ready_project.id)
    assert status["project_status"] == ProjectStatus.STORYBOARD_CONFIRMED.value
    assert status["manifest"] is None and status["job"] is None

    job = await video_pipeline.start_video_generation(ready_project.id)
    await wait_job(video_pipeline, job.id)

    status = video_pipeline.video_status(ready_project.id)
    assert status["summary"]["ready"] == shot_count(store, ready_project)
    assert status["job"]["status"] == "succeeded"
    assert status["manifest"]["clips"][0]["path"].startswith("clips/")


@pytest.mark.asyncio
async def test_recover_interrupted_status(store, video_pipeline, ready_project):
    def force_status(value: str) -> None:
        path = store.project_dir(ready_project.id) / "meta.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["status"] = value
        path.write_text(json.dumps(raw), encoding="utf-8")

    # 场景 1：还没生成任何片段就中断 → 回到 STORYBOARD_CONFIRMED
    force_status("video_generating")
    assert ready_project.id in await video_pipeline.recover_interrupted_jobs()
    assert store.load_project(ready_project.id).status == ProjectStatus.STORYBOARD_CONFIRMED

    # 场景 2：已经有片段了才中断 → 恢复到 VIDEO_READY
    job = await video_pipeline.start_video_generation(ready_project.id)
    await wait_job(video_pipeline, job.id)
    force_status("video_generating")
    await video_pipeline.recover_interrupted_jobs()
    assert store.load_project(ready_project.id).status == ProjectStatus.VIDEO_READY


# ── MockVideoProvider（真实 ffmpeg / 降级）────────────────


@pytest.mark.asyncio
async def test_mock_provider_produces_real_mp4(tmp_path):
    from videogen.providers.video.base import VideoRequest
    from videogen.providers.video.mock import MockVideoProvider
    from videogen.utils.ffmpeg import ffmpeg_available

    if not ffmpeg_available():
        pytest.skip("环境里没有 ffmpeg，跳过真实转码用例")

    provider = MockVideoProvider()
    clip = await provider.generate(
        VideoRequest(
            shot_id="scene_1_shot_1",
            scene_id="scene_1",
            prompt="a test clip",
            duration_sec=1,
            aspect_ratio="9:16",
            output_path=tmp_path / "clip.mp4",
        )
    )
    assert clip.media_kind == MediaKind.VIDEO
    assert clip.path.exists() and clip.path.stat().st_size > 0
    assert clip.duration_sec == 1


@pytest.mark.asyncio
async def test_mock_provider_falls_back_to_placeholder_without_ffmpeg(monkeypatch, tmp_path):
    from videogen.providers.video.base import VideoRequest
    from videogen.providers.video.mock import MockVideoProvider

    monkeypatch.setattr("videogen.providers.video.mock.ffmpeg_available", lambda: False)
    provider = MockVideoProvider()
    clip = await provider.generate(
        VideoRequest(
            shot_id="scene_1_shot_1",
            prompt="a test clip",
            duration_sec=4,
            output_path=tmp_path / "clip.mp4",
        )
    )
    # 明确标记为占位，绝不假装成视频
    assert clip.media_kind == MediaKind.PLACEHOLDER
    assert clip.path.suffix == ".json"
    assert clip.path.exists()
    assert "placeholder" in clip.path.read_text(encoding="utf-8")


def test_video_provider_factory(monkeypatch):
    from videogen.providers.video import get_video_provider
    from videogen.providers.video.mock import MockVideoProvider

    assert isinstance(get_video_provider(), MockVideoProvider)


@pytest.mark.asyncio
async def test_unexpected_error_in_one_segment_does_not_abort_others(
    store, ready_project, monkeypatch
):
    """回归：某段在提交前抛了意外异常，gather 以前直接抛出 —— 别的段还在跑、
    客户端却被关了（已扣费的段下载失败），这一轮也不记账。"""
    storyboard = store.load_storyboard(ready_project.id)
    bad = storyboard.shots[0].id
    original = video_service.build_prompt

    def flaky(shot, *args, **kwargs):
        if shot.id == bad:
            raise ValueError("boom")
        return original(shot, *args, **kwargs)

    monkeypatch.setattr(video_service, "build_prompt", flaky)
    manifest = await run_clips(store, ready_project, FakeVideoProvider())
    assert manifest.get(bad).status.value == "failed"
    assert "boom" in manifest.get(bad).error
    others = [c for c in manifest.clips if c.shot_id != bad]
    assert others and all(c.status.value == "ready" for c in others)
