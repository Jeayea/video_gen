from __future__ import annotations

import asyncio

import pytest

from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.project import Project
from videogen.orchestrator.pipeline import Pipeline
from videogen.utils.file_store import FileStore


@pytest.fixture(autouse=True)
def fast_video_settings(monkeypatch):
    """测试里不要真的等退避时间。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "video_retry_backoff_sec", 0.0)
    monkeypatch.setattr(settings, "tts_retry_backoff_sec", 0.0)
    return settings


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_video() -> FakeVideoProvider:
    return FakeVideoProvider()


@pytest.fixture
def fake_tts() -> FakeTTSProvider:
    return FakeTTSProvider()


@pytest.fixture
def fake_bgm() -> FakeBgmLibrary:
    return FakeBgmLibrary()


@pytest.fixture
def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / "projects")


@pytest.fixture
def pipeline(store: FileStore, fake_llm: FakeLLM) -> Pipeline:
    return Pipeline(store, llm_factory=lambda: fake_llm)


@pytest.fixture
def video_pipeline(
    store: FileStore, fake_llm: FakeLLM, fake_video: FakeVideoProvider
) -> Pipeline:
    """带假视频 provider 的流水线（与 `pipeline` 共享同一个 store）。"""
    return Pipeline(store, llm_factory=lambda: fake_llm, video_factory=lambda: fake_video)


@pytest.fixture
def media_pipeline(
    store: FileStore,
    fake_llm: FakeLLM,
    fake_video: FakeVideoProvider,
    fake_tts: FakeTTSProvider,
    fake_bgm: FakeBgmLibrary,
) -> Pipeline:
    """视频 + 配音 + BGM 全假的流水线，用来跑媒体链路。"""
    return Pipeline(
        store,
        llm_factory=lambda: fake_llm,
        video_factory=lambda: fake_video,
        tts_factory=lambda: fake_tts,
        bgm_factory=lambda: fake_bgm,
    )


@pytest.fixture
def make_pipeline(store: FileStore):
    """按需构造使用指定 FakeLLM 的 Pipeline。"""

    def _make(llm) -> Pipeline:
        return Pipeline(store, llm_factory=lambda: llm)

    return _make


@pytest.fixture
def make_video_pipeline(store: FileStore):
    """按需构造使用指定视频 provider 的 Pipeline。"""

    def _make(video_provider, llm: FakeLLM | None = None) -> Pipeline:
        return Pipeline(
            store,
            llm_factory=lambda: llm or FakeLLM(),
            video_factory=lambda: video_provider,
        )

    return _make


@pytest.fixture
def make_media_pipeline(store: FileStore):
    """按需构造媒体链路 Pipeline（可分别指定 tts / video / bgm）。"""

    def _make(tts, video_provider=None, llm: FakeLLM | None = None, bgm=None) -> Pipeline:
        return Pipeline(
            store,
            llm_factory=lambda: llm or FakeLLM(),
            video_factory=lambda: video_provider or FakeVideoProvider(),
            tts_factory=lambda: tts,
            bgm_factory=lambda: bgm or FakeBgmLibrary(),
        )

    return _make


async def build_confirmed_project(store: FileStore, pipeline: Pipeline) -> Project:
    """跑完文本链路并确认分镜。"""
    project = Project(title="媒体阶段测试", input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)
    await pipeline.generate_storyboard(project.id)
    await pipeline.confirm_storyboard(project.id)
    return store.load_project(project.id)


async def wait_for_job(pipeline: Pipeline, job_id: str, timeout: float = 30.0):
    """等后台任务结束（测试里轮询任务表）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = pipeline.jobs.get(job_id)
        if job and not job.is_active:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务 {job_id} 未在 {timeout}s 内结束")


@pytest.fixture
async def ready_project(store: FileStore, pipeline: Pipeline) -> Project:
    """跑完文本链路并确认分镜，返回可以直接生成视频的项目。"""
    return await build_confirmed_project(store, pipeline)


@pytest.fixture
async def video_ready_project(store: FileStore, video_pipeline: Pipeline) -> Project:
    """文本链路 + 视频片段都就绪的项目。"""
    project = await build_confirmed_project(store, video_pipeline)
    job = await video_pipeline.start_video_generation(project.id)
    await wait_for_job(video_pipeline, job.id)
    return store.load_project(project.id)


@pytest.fixture
async def audio_ready_project(store: FileStore, media_pipeline: Pipeline) -> Project:
    """文本 + 视频 + 配音都就绪的项目（BGM 用假曲库）。"""
    project = await build_confirmed_project(store, media_pipeline)
    video_job = await media_pipeline.start_video_generation(project.id)
    await wait_for_job(media_pipeline, video_job.id)
    audio_job = await media_pipeline.start_audio_generation(project.id)
    await wait_for_job(media_pipeline, audio_job.id)
    return store.load_project(project.id)
