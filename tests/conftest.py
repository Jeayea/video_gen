from __future__ import annotations

import pytest

from tests.fakes import FakeLLM
from videogen.orchestrator.pipeline import Pipeline
from videogen.utils.file_store import FileStore


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / "projects")


@pytest.fixture
def pipeline(store: FileStore, fake_llm: FakeLLM) -> Pipeline:
    return Pipeline(store, llm_factory=lambda: fake_llm)


@pytest.fixture
def make_pipeline(store: FileStore):
    """按需构造使用指定 FakeLLM 的 Pipeline。"""

    def _make(llm) -> Pipeline:
        return Pipeline(store, llm_factory=lambda: llm)

    return _make
