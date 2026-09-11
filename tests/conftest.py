from __future__ import annotations

import pytest

from tests.fakes import FakeLLM, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.project import Project
from videogen.orchestrator.pipeline import Pipeline
from videogen.utils.file_store import FileStore


@pytest.fixture(autouse=True)
def fast_video_settings(monkeypatch):
    """测试里不要真的等退避时间。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "video_retry_backoff_sec", 0.0)
    return settings


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_video() -> FakeVideoProvider:
    return FakeVideoProvider()


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
async def ready_project(store: FileStore, pipeline: Pipeline) -> Project:
    """跑完文本链路并确认分镜，返回可以直接生成视频的项目。"""
    project = Project(title="视频阶段测试", input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id)
    await pipeline.generate_storyboard(project.id)
    await pipeline.confirm_storyboard(project.id)
    return store.load_project(project.id)
