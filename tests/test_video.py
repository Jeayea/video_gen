from __future__ import annotations

import asyncio
import json

import pytest

from tests.fakes import AlwaysFailVideoProvider, FakeVideoProvider
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
