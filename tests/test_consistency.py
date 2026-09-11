"""角色一致性（参考图 / seed）与多模型切换。"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from tests.conftest import wait_for_job
from tests.fakes import FakeLLM, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.bible import Character, StoryBible
from videogen.models.project import Project, ProjectStatus
from videogen.models.storyboard import Shot, Storyboard
from videogen.orchestrator.pipeline import Pipeline
from videogen.services import video_service
from videogen.utils.ffmpeg import ffmpeg_available
from videogen.utils.file_store import FileStore

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes").decode()


def project_with_character(store: FileStore, *, reference: str = "") -> Project:
    project = Project(title="一致性", input_text="测试", target_duration_sec=10)
    project.status = ProjectStatus.STORYBOARD_CONFIRMED
    store.save_project(project)
    store.save_bible(
        project.id,
        StoryBible(
            characters=[
                Character(
                    id="char_1",
                    name="林晚",
                    role="主角",
                    description="女白领",
                    reference_image=reference,
                )
            ]
        ),
    )
    store.save_storyboard(
        project.id,
        Storyboard(
            shots=[
                Shot(
                    id="scene_1_shot_1",
                    scene_id="scene_1",
                    shot_no=1,
                    duration_sec=1,
                    video_prompt="medium shot",
                    character_in_frame=["char_1"],
                )
            ]
        ),
    )
    return project


def make_pipeline(store: FileStore, video) -> Pipeline:
    return Pipeline(store, llm_factory=lambda: FakeLLM(), video_factory=lambda: video)


# ── 参考图解析 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reference_image_is_passed_to_provider(store):
    project = project_with_character(store, reference="refs/char_1.png")
    ref = store.refs_dir(project.id) / "char_1.png"
    ref.write_bytes(b"reference-image")

    provider = FakeVideoProvider(supports_reference_image=True)
    pipeline = make_pipeline(store, provider)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.reference_image is not None
    assert request.reference_image.name == "char_1.png"
    assert request.reference_image.is_file()


@pytest.mark.asyncio
async def test_reference_image_ignored_when_provider_unsupported(store):
    project = project_with_character(store, reference="refs/char_1.png")
    (store.refs_dir(project.id) / "char_1.png").write_bytes(b"x")

    provider = FakeVideoProvider(supports_reference_image=False)
    pipeline = make_pipeline(store, provider)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert provider.requests[0].reference_image is None


@pytest.mark.asyncio
async def test_video_seed_is_passed(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "video_seed", 4242)
    project = project_with_character(store)

    provider = FakeVideoProvider()
    pipeline = make_pipeline(store, provider)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert provider.requests[0].seed == 4242


@pytest.mark.asyncio
async def test_character_seed_overrides_global(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "video_seed", 1)
    project = project_with_character(store, reference="refs/char_1.png")
    (store.refs_dir(project.id) / "char_1.png").write_bytes(b"x")
    bible = store.load_bible(project.id)
    bible.characters[0] = bible.characters[0].model_copy(update={"seed": 777})
    store.save_bible(project.id, bible)

    provider = FakeVideoProvider(supports_reference_image=True)
    pipeline = make_pipeline(store, provider)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert provider.requests[0].seed == 777


def test_missing_reference_file_falls_back(store):
    project = project_with_character(store, reference="refs/nope.png")
    bible = store.load_bible(project.id)
    shot = store.load_storyboard(project.id).shots[0]

    reference, seed = video_service.resolve_reference(
        shot,
        bible,
        store.ensure_project_dir(project.id),
        FakeVideoProvider(supports_reference_image=True),
        0,
    )
    assert reference is None and seed is None


def test_reference_outside_project_dir_is_rejected(store):
    project = project_with_character(store, reference="../../../etc/passwd")
    bible = store.load_bible(project.id)
    shot = store.load_storyboard(project.id).shots[0]

    reference, _ = video_service.resolve_reference(
        shot,
        bible,
        store.ensure_project_dir(project.id),
        FakeVideoProvider(supports_reference_image=True),
    )
    assert reference is None


def test_seed_is_passed_even_without_reference_support(store):
    """seed 是通用参数：provider 不支持参考图也不影响 seed 透传。"""
    project = project_with_character(store, reference="refs/char_1.png")
    (store.refs_dir(project.id) / "char_1.png").write_bytes(b"x")
    shot = store.load_storyboard(project.id).shots[0]

    reference, seed = video_service.resolve_reference(
        shot, store.load_bible(project.id), store.ensure_project_dir(project.id),
        FakeVideoProvider(supports_reference_image=False), 99,
    )
    assert reference is None
    assert seed == 99  # seed 是通用参数，与参考图无关


@pytest.mark.asyncio
async def test_mock_provider_tints_by_reference_image(tmp_path):
    """mock provider 用参考图取色：换了参考图，画面颜色必须变（证明参数真的传到了）。"""
    from videogen.providers.video.base import VideoRequest
    from videogen.providers.video.mock import MockVideoProvider

    if not ffmpeg_available():
        pytest.skip("环境里没有 ffmpeg")

    ref_a = tmp_path / "a.png"
    ref_b = tmp_path / "b.png"
    ref_a.write_bytes(b"reference-A")
    ref_b.write_bytes(b"reference-B")

    provider = MockVideoProvider()
    clip_a = await provider.generate(
        VideoRequest(
            shot_id="s1",
            prompt="p",
            duration_sec=1,
            output_path=tmp_path / "a.mp4",
            reference_image=ref_a,
        )
    )
    clip_b = await provider.generate(
        VideoRequest(
            shot_id="s1",
            prompt="p",
            duration_sec=1,
            output_path=tmp_path / "b.mp4",
            reference_image=ref_b,
        )
    )
    assert clip_a.path.read_bytes() != clip_b.path.read_bytes()


# ── 多模型切换 ────────────────────────────────────────


def test_fake_llm_records_model_switch():
    llm = FakeLLM()
    assert llm.with_model("google/gemini-2.5-pro") is llm
    assert llm.models_used == ["google/gemini-2.5-pro"]
    assert llm.model == "google/gemini-2.5-pro"


@pytest.mark.asyncio
async def test_generate_uses_requested_model(store, fake_llm):
    pipeline = Pipeline(store, llm_factory=lambda: fake_llm)
    project = Project(input_text="换模型", target_duration_sec=30)
    store.save_project(project)

    await pipeline.generate_bible(project.id, model="google/gemini-2.5-pro")
    assert fake_llm.models_used == ["google/gemini-2.5-pro"]
    # 成本账本要记下用的是哪个模型
    entry = store.load_cost_report(project.id).entries[0]
    assert entry.model == "google/gemini-2.5-pro"


@pytest.mark.asyncio
async def test_empty_model_keeps_default(store, fake_llm):
    pipeline = Pipeline(store, llm_factory=lambda: fake_llm)
    project = Project(input_text="默认模型", target_duration_sec=30)
    store.save_project(project)

    await pipeline.generate_bible(project.id)
    assert fake_llm.models_used == []
    assert fake_llm.model == "fake-model"


@pytest.mark.asyncio
async def test_project_model_is_used_by_default(store, fake_llm):
    """项目上保存的模型应该在不传参数时生效（前端下拉用）。"""
    pipeline = Pipeline(store, llm_factory=lambda: fake_llm)
    project = Project(input_text="项目模型", target_duration_sec=30)
    store.save_project(project)
    updated = await pipeline.update_project(
        project.id, {"llm_model": "anthropic/claude-3.5-haiku"}
    )
    assert updated.llm_model == "anthropic/claude-3.5-haiku"

    await pipeline.generate_bible(project.id, model=updated.llm_model)
    assert fake_llm.models_used == ["anthropic/claude-3.5-haiku"]


def test_candidate_models_from_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "llm_model_candidates", "a/model-1, b/model-2, a/model-1, ,"
    )
    monkeypatch.setattr(settings, "openrouter_model", "default/model")
    assert settings.candidate_models[0] == "default/model"
    assert settings.candidate_models == ["default/model", "a/model-1", "b/model-2"]


def test_reference_request_model_validation():
    from videogen.main import ReferenceImageRequest

    with pytest.raises(ValidationError):
        ReferenceImageRequest(content_base64="")
    assert ReferenceImageRequest(filename="a.png", content_base64=PNG).filename == "a.png"
