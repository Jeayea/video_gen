from __future__ import annotations

import asyncio

import pytest

from tests.fakes import (
    FakeBgmLibrary,
    FakeImageProvider,
    FakeLLM,
    FakeTTSProvider,
    FakeVideoProvider,
)
from videogen.config import get_settings
from videogen.models.bible import Character, StoryBible, StyleGuide
from videogen.models.project import Project
from videogen.orchestrator.pipeline import Pipeline
from videogen.utils.file_store import FileStore


@pytest.fixture(autouse=True)
def fast_video_settings(monkeypatch):
    """所有测试共用的安全/加速基线。

    关键点：**测试绝不能碰真实付费服务**。开发者本地 `.env` 可能已经切到
    `VIDEO_PROVIDER=openrouter`（甚至设了预算），如果测试跟着用默认工厂就会真的提交
    付费任务 —— 所以这里强制把 provider 拉回 mock、预算清零、退避归零。
    需要真实 provider 的测试自己显式构造/猴补参数（例如 test_video_openrouter.py
    用 httpx.MockTransport 打桩）。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "video_retry_backoff_sec", 0.0)
    monkeypatch.setattr(settings, "tts_retry_backoff_sec", 0.0)
    monkeypatch.setattr(settings, "image_retry_backoff_sec", 0.0)
    monkeypatch.setattr(settings, "video_provider", "mock")
    # 配音：开发者本地 .env 可能已经切到云端 TTS（真实付费），测试基线必须锁回 edge
    monkeypatch.setattr(settings, "tts_provider", "edge")
    # 角色图同理：默认工厂绝不能真的去打图片模型
    monkeypatch.setattr(settings, "image_provider", "mock")
    monkeypatch.setattr(settings, "budget_usd", 0.0)
    # 合成基线：本地 .env 可能已经设成 burn（会真的改走 libass 分支），
    # 需要烧字幕的用例自己显式传 overrides
    monkeypatch.setattr(settings, "compose_subtitle_mode", "soft")
    monkeypatch.setattr(settings, "compose_ambient_mode", "off")
    # 口型对齐会给每个片段多跑两次 ffmpeg（分析画面里的人几秒开口），
    # 测试里默认关掉；专门测它的用例自己打开
    monkeypatch.setattr(settings, "compose_align_dub_to_mouth", False)
    # 配音文案：默认关掉，防止本地 .env 打开后让无关用例多调 LLM / 抽帧
    monkeypatch.setattr(settings, "audio_llm_script", "off")
    monkeypatch.setattr(settings, "audio_llm_vision", False)
    # 段落分镜：老用例按「一镜一次生成」写断言（假 provider 也只写小文件、切不了），
    # 默认关掉；段落相关用例（test_segments.py）自己打开
    monkeypatch.setattr(settings, "video_segment_mode", False)
    # 人声来源：老用例都在测 TTS 配音链路（默认已改成 native = 视频原声），锁回 tts；
    # 原声模式的用例自己打开
    monkeypatch.setattr(settings, "audio_dub_mode", "tts")
    # 开场关键帧画什么：本机 .env 可能是 scene（Seedance），用例按默认的锁人模式写
    monkeypatch.setattr(settings, "video_keyframe_subject", "characters")
    # 同理：跑 Seedance 时 .env 里会有这些，用例按代码默认值写
    monkeypatch.setattr(settings, "video_prompt_narration", False)
    monkeypatch.setattr(settings, "video_price_per_second", 0.0)
    monkeypatch.setattr(settings, "video_generate_audio", False)
    monkeypatch.setattr(settings, "video_resolution", "")
    # 本机 .env 可能为某个模型手调过这两项
    monkeypatch.setattr(settings, "video_frame_mode", "auto")
    monkeypatch.setattr(settings, "video_prompt_limits", "kwaivgi/=2500")
    # TestClient 发的 Host 是 testserver；没设 token 时 Host 白名单只认本机名
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "api_allowed_hosts", "testserver")
    # 有些用例直接 `settings.xxx = ...` 改全局配置（没走 monkeypatch）：
    # 用例结束整份还原，否则会串到后面的用例里（真实症状：单跑通过、和别的文件一起跑失败）
    snapshot = dict(settings.__dict__)
    yield settings
    settings.__dict__.clear()
    settings.__dict__.update(snapshot)


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
def fake_image() -> FakeImageProvider:
    return FakeImageProvider()


@pytest.fixture
def settings(fast_video_settings):
    """当前（已被测试基线改写过的）配置对象。"""
    return fast_video_settings


@pytest.fixture
def bible() -> StoryBible:
    """一个小而完整的故事圣经：4 个角色 + 风格，够测角色图与参考图链路。"""
    return StoryBible(
        characters=[
            Character(
                id="char_1",
                name="林悦",
                age=20,
                role="主角",
                visual_prompt=(
                    "A young woman with long straight dark brown hair, soft round face, "
                    "wearing a simple grey top"
                ),
            ),
            Character(
                id="char_2",
                name="李薇儿",
                age=22,
                role="反派",
                visual_prompt="A sharp-featured young woman with a high ponytail and a smirk",
            ),
            Character(
                id="char_3",
                name="张姐",
                age=38,
                role="导师",
                visual_prompt="A poised woman in her late thirties wearing a tailored suit",
            ),
            Character(
                id="char_4",
                name="赵总",
                age=45,
                role="配角",
                visual_prompt="A middle-aged man with slicked-back hair and a flashy suit",
            ),
        ],
        style=StyleGuide(genre="都市励志", tone="冷峻克制"),
    )


def _casting_storyboard():
    """选角测试用的（圣经, 分镜）：4 个不同性别/年龄的说话人 + 旁白。"""
    from videogen.models.storyboard import Shot, Storyboard

    bible = StoryBible(
        characters=[
            Character(id="char_1", name="林悦", age=20, role="主角"),
            Character(id="char_2", name="李薇儿", age=22, role="反派"),
            Character(id="char_3", name="张姐", age=38, role="导师"),
            Character(id="char_4", name="赵总", age=45, role="配角"),
        ],
        style=StyleGuide(genre="都市励志"),
    )
    storyboard = Storyboard(
        shots=[
            Shot(id="s1", scene_id="sc1", shot_no=1, dialogue_text="a", dialogue_speaker="char_1"),
            Shot(id="s2", scene_id="sc1", shot_no=2, dialogue_text="b", dialogue_speaker="char_2"),
            Shot(id="s3", scene_id="sc1", shot_no=3, dialogue_text="c", dialogue_speaker="char_3"),
            Shot(id="s4", scene_id="sc1", shot_no=4, dialogue_text="d", dialogue_speaker="char_4"),
            Shot(id="s5", scene_id="sc1", shot_no=5, narration="n"),
        ]
    )
    return bible, storyboard


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
