"""OpenRouter 真实视频 provider（默认 minimax/hailuo-3 = MiniMax H3）。

全部用 `httpx.MockTransport` 打桩：**不发真实请求、不花一分钱**，
但请求体、轮询、下载、错误映射、参考图、计费口径都按真实 API 的形状验证。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.conftest import wait_for_job
from tests.fakes import FakeLLM
from videogen.config import get_settings
from videogen.models.project import Project
from videogen.models.storyboard import Shot, Storyboard
from videogen.orchestrator.pipeline import Pipeline
from videogen.providers.video import clear_video_capability_cache, get_video_provider
from videogen.providers.video.base import (
    VideoArtifactError,
    VideoProviderError,
    VideoReferenceRejected,
    VideoRequest,
)
from videogen.providers.video.mock import MockVideoProvider
from videogen.providers.video.openrouter import OpenRouterVideoProvider
from videogen.services import cost_service
from videogen.services.cost_service import BudgetExceeded
from videogen.utils.file_store import FileStore

#: 真实 `/videos/models` 里 minimax/hailuo-3 的条目（2026-09 抓的）
HAILUO3 = {
    "id": "minimax/hailuo-3",
    "name": "MiniMax: H3",
    "supported_resolutions": ["2K"],
    "supported_aspect_ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "supported_durations": [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    "supported_frame_images": ["first_frame", "last_frame"],
    "supported_sizes": None,
    "generate_audio": True,
    "seed": False,
    "pricing_skus": {"duration_seconds": "0.13", "reference_images": "0.04"},
    "allowed_passthrough_parameters": ["aigc_watermark"],
}

HAILUO3_MAX = {
    **HAILUO3,
    "id": "minimax/hailuo-3-max",
    "name": "MiniMax: H3 Max",
    "supported_resolutions": ["768p", "480p"],
    "seed": False,
    "pricing_skus": {
        "duration_seconds": "0.08",
        "duration_seconds_480p": "0.05",
        "duration_seconds_768p": "0.08",
    },
}

VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"fake-video-payload" * 10


@pytest.fixture(autouse=True)
def _clear_caps():
    clear_video_capability_cache()
    yield
    clear_video_capability_cache()


class Recorder:
    """记录请求，并按脚本返回响应。

    - `content_ok=False`：官方 content 端点 404（用来验证回退到 unsigned_urls）
    - `unsigned_requires_auth=True`：unsigned_urls 也要求 Authorization（真实行为，
      裸下载会 401）
    - `cost=0.83`：轮询响应里带上 `usage.cost`，验证账本以实际扣费为准
    """

    def __init__(
        self,
        *,
        models=None,
        submit_status=202,
        polls=None,
        download_bytes=VIDEO_BYTES,
        content_ok: bool = True,
        unsigned_requires_auth: bool = True,
        cost: float | None = None,
    ):
        self.models = models if models is not None else [HAILUO3]
        self.submit_status = submit_status
        self.polls = list(
            polls or [{"status": "completed", "unsigned_urls": ["https://cdn/v.mp4"]}]
        )
        self.download_bytes = download_bytes
        self.content_ok = content_ok
        self.unsigned_requires_auth = unsigned_requires_auth
        self.cost = cost
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.models_calls = 0
        self.submits = 0

    def _poll_payload(self, poll: dict) -> dict:
        payload = {"id": "job-1", **poll}
        if self.cost is not None:
            payload["usage"] = {"cost": self.cost, "is_byok": False}
        return payload

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/videos/models"):
            self.models_calls += 1
            return httpx.Response(200, json={"data": self.models})
        if path.endswith("/videos") and request.method == "POST":
            self.submits += 1
            self.bodies.append(json.loads(request.content))
            if self.submit_status >= 400:
                return httpx.Response(
                    self.submit_status,
                    json={
                        "error": {
                            "message": f"假的 {self.submit_status} 错误",
                            "code": self.submit_status,
                        }
                    },
                )
            return httpx.Response(
                202,
                json={
                    "id": "job-1",
                    "polling_url": "https://openrouter.ai/api/v1/videos/job-1",
                    "status": "pending",
                },
            )
        if path.endswith("/videos/job-1/content"):
            if not self.content_ok:
                return httpx.Response(404, json={"error": {"message": "content not ready"}})
            return httpx.Response(200, content=self.download_bytes)
        if path.endswith("/videos/job-1") and request.method == "GET":
            poll = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return httpx.Response(200, json=self._poll_payload(poll))
        if request.url.host == "cdn":
            # 真实环境里 unsigned_urls 也需要 Authorization，裸下载会 401
            if self.unsigned_requires_auth and "authorization" not in request.headers:
                return httpx.Response(401, json={"error": {"message": "unauthorized"}})
            return httpx.Response(200, content=self.download_bytes)
        return httpx.Response(404, json={"error": {"message": "not found"}})


def make_provider(recorder: Recorder, **kwargs) -> OpenRouterVideoProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    kwargs.setdefault("api_key", "test-key-not-a-real-secret")
    kwargs.setdefault("model", "minimax/hailuo-3")
    return OpenRouterVideoProvider(client=client, **kwargs)


def request_for(tmp_path: Path, **overrides) -> VideoRequest:
    # 老写法 reference_image=... 兼容成列表（多角色镜头用 reference_images=[a, b]）
    if "reference_image" in overrides:
        overrides.setdefault("reference_images", [overrides.pop("reference_image")])
    data = {
        "shot_id": "scene_1_shot_1",
        "scene_id": "scene_1",
        "prompt": "medium shot of char_1 in loc_1",
        "duration_sec": 3,
        "aspect_ratio": "9:16",
        "output_path": tmp_path / "clip.mp4",
    }
    data.update(overrides)
    return VideoRequest(**data)


# ── 能力 / 单价 / 计费档位 ────────────────────────────


@pytest.mark.asyncio
async def test_capabilities_are_parsed_and_cached():
    recorder = Recorder()
    provider = make_provider(recorder)

    caps = await provider.capabilities()
    again = await provider.capabilities()

    assert caps["id"] == "minimax/hailuo-3"
    assert again is caps
    assert recorder.models_calls == 1, "能力列表应该被缓存，不要每次都拉"


@pytest.mark.asyncio
async def test_duration_options_expose_billing_tiers():
    """成本前置靠它算「3 秒的镜头也要买 5 秒」——档位必须升序且去重。"""
    recorder = Recorder()
    provider = make_provider(recorder)

    options = await provider.duration_options()

    assert options[0] == 5 and options[-1] == 15
    assert options == sorted(set(options))
    # 档位与计费秒数是同一份数据
    assert await provider.planned_seconds(3) == 5.0
    assert recorder.models_calls == 1  # 档位与计费共用同一份缓存的能力表


@pytest.mark.asyncio
async def test_duration_options_empty_when_model_unknown():
    recorder = Recorder(models=[])
    provider = make_provider(recorder, model="someone/brand-new-model")
    assert await provider.duration_options() == []


@pytest.mark.asyncio
async def test_unknown_model_does_not_break(monkeypatch):
    recorder = Recorder(models=[])
    provider = make_provider(recorder, model="someone/brand-new-model")
    assert await provider.capabilities() == {}
    # 不校验也不该让上游崩：时长原样返回
    assert await provider.planned_seconds(4) == 4.0


@pytest.mark.asyncio
async def test_price_reads_resolution_specific_sku():
    recorder = Recorder(models=[HAILUO3, HAILUO3_MAX])
    provider = make_provider(recorder, model="minimax/hailuo-3", resolution="")
    assert await provider.price_per_second() == pytest.approx(0.13)

    provider480 = make_provider(recorder, model="minimax/hailuo-3-max", resolution="480p")
    assert await provider480.price_per_second() == pytest.approx(0.05)


KLING_STD = {
    "id": "kwaivgi/kling-v3.0-std",
    "supported_resolutions": ["720p"],
    "supported_durations": list(range(3, 16)),
    "generate_audio": True,
    "seed": False,
    "pricing_skus": {
        "duration_seconds": "0.084",
        "duration_seconds_with_audio": "0.126",
        "text_to_video_duration_seconds_720p": "0.084",
    },
}
VEO_FAST = {
    "id": "google/veo-3.1-fast",
    "supported_resolutions": ["720p", "1080p", "4K"],
    "supported_durations": [4, 6, 8],
    "generate_audio": True,
    "pricing_skus": {
        "duration_seconds_with_audio": "0.12",
        "duration_seconds_without_audio": "0.10",
        "duration_seconds_with_audio_720p": "0.10",
        "duration_seconds_without_audio_720p": "0.08",
    },
}


@pytest.mark.asyncio
async def test_price_follows_audio_tier():
    recorder = Recorder(models=[KLING_STD, VEO_FAST])
    kling = "kwaivgi/kling-v3.0-std"
    silent = make_provider(recorder, model=kling, generate_audio=False)
    assert await silent.price_per_second() == pytest.approx(0.084)
    voiced = make_provider(recorder, model=kling, generate_audio=True)
    assert await voiced.price_per_second() == pytest.approx(0.126)

    veo = "google/veo-3.1-fast"
    veo_silent = make_provider(recorder, model=veo, resolution="720p")
    assert await veo_silent.price_per_second() == pytest.approx(0.08)
    veo_voiced = make_provider(recorder, model=veo, resolution="720p", generate_audio=True)
    assert await veo_voiced.price_per_second() == pytest.approx(0.10)
    veo_1080 = make_provider(recorder, model=veo, resolution="1080p", generate_audio=True)
    assert await veo_1080.price_per_second() == pytest.approx(0.12)


@pytest.mark.asyncio
async def test_planned_seconds_clamps_to_model_durations():
    provider = make_provider(Recorder())
    assert await provider.planned_seconds(3) == 5.0  # H3 最短 5 秒
    assert await provider.planned_seconds(5) == 5.0
    assert await provider.planned_seconds(7.2) == 8.0
    assert await provider.planned_seconds(99) == 15.0  # 最长 15 秒


# ── 生成流程 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_success_flow(tmp_path):
    recorder = Recorder(
        polls=[
            {"status": "pending"},
            {"status": "processing"},
            {"status": "completed", "unsigned_urls": ["https://cdn/v.mp4"]},
        ]
    )
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01  # 测试里不等 10 秒

    clip = await provider.generate(request_for(tmp_path))

    assert clip.path == tmp_path / "clip.mp4"
    assert clip.path.read_bytes() == VIDEO_BYTES
    assert clip.task_id == "job-1"
    assert clip.media_kind.value == "video"
    # 拿不到真实时长（假视频）时按计费档位记 5s
    assert clip.duration_sec == 5
    assert recorder.submits == 1


@pytest.mark.asyncio
async def test_submitted_payload_respects_model_capabilities(tmp_path):
    recorder = Recorder()
    provider = make_provider(recorder, resolution="1080p")  # H3 只支持 2K
    provider.poll_interval_sec = 0.01

    await provider.generate(request_for(tmp_path, seed=1234))

    payload = recorder.bodies[0]
    assert payload["model"] == "minimax/hailuo-3"
    assert payload["duration"] == 5, "3 秒镜头要抬到模型最短的 5 秒"
    assert payload["resolution"] == "2K", "不支持的分辨率要换成模型支持的"
    assert payload["aspect_ratio"] == "9:16"
    assert payload["generate_audio"] is False, "我们用 TTS 配音，不让模型生成音轨"
    assert "seed" not in payload, "H3 不支持 seed，带上会被 400"


@pytest.mark.asyncio
async def test_seed_sent_when_model_supports_it(tmp_path):
    recorder = Recorder(models=[{**HAILUO3, "seed": True}])
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    await provider.generate(request_for(tmp_path, seed=777))
    assert recorder.bodies[0]["seed"] == 777


@pytest.mark.asyncio
async def test_failed_job_raises_with_reason(tmp_path):
    recorder = Recorder(polls=[{"status": "failed", "error": "content policy violation"}])
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    with pytest.raises(VideoProviderError) as exc:
        await provider.generate(request_for(tmp_path))
    assert "content policy violation" in str(exc.value)
    assert "job-1" in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_raises_and_mentions_job(tmp_path):
    recorder = Recorder(polls=[{"status": "processing"}])
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01
    provider.timeout_sec = 0.05

    with pytest.raises(VideoProviderError) as exc:
        await provider.generate(request_for(tmp_path))
    assert "超时" in str(exc.value) and "job-1" in str(exc.value)


@pytest.mark.asyncio
async def test_empty_download_is_rejected(tmp_path):
    recorder = Recorder(
        polls=[{"status": "completed", "unsigned_urls": ["https://cdn/empty.mp4"]}],
        download_bytes=b"",
    )
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    with pytest.raises(VideoProviderError):
        await provider.generate(request_for(tmp_path))
    assert not (tmp_path / "clip.mp4").exists(), "空文件不能留下来"


@pytest.mark.asyncio
async def test_no_unsigned_urls_still_tries_content_endpoint(tmp_path):
    """即使响应里没有 unsigned_urls，也先试官方的 content 端点。"""
    recorder = Recorder(polls=[{"status": "completed"}])
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    clip = await provider.generate(request_for(tmp_path))
    assert clip.path.is_file()


@pytest.mark.asyncio
async def test_no_urls_and_content_unavailable_raises_artifact_error(tmp_path):
    recorder = Recorder(polls=[{"status": "completed"}], content_ok=False)
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    with pytest.raises(VideoArtifactError) as exc:
        await provider.generate(request_for(tmp_path))
    message = str(exc.value)
    assert "下载失败" in message and "job-1" in message
    assert exc.value.retryable is False


# ── 错误映射（消息要能直接给用户看）────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "keyword"),
    [(400, "参数不被支持"), (401, "API Key 无效"), (402, "余额不足"), (403, "权限"), (429, "限流")],
)
async def test_http_errors_are_mapped_to_actionable_messages(tmp_path, status, keyword):
    recorder = Recorder(submit_status=status)
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01
    provider.max_http_retries = 1  # 不要为了测错误映射而退避重试

    with pytest.raises(VideoProviderError) as exc:
        await provider.generate(request_for(tmp_path))
    message = str(exc.value)
    assert keyword in message
    assert "test-key-not-a-real-secret" not in message, "错误信息里绝不能出现密钥"


def test_missing_api_key_is_rejected(monkeypatch):
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")
    with pytest.raises(VideoProviderError) as exc:
        OpenRouterVideoProvider(model="minimax/hailuo-3")
    assert "OPENROUTER_API_KEY" in str(exc.value)


# ── 角色一致性（参考图）───────────────────────────────


@pytest.mark.asyncio
async def test_reference_image_goes_to_input_references(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    recorder = Recorder()
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    await provider.generate(request_for(tmp_path, reference_image=ref))

    references = recorder.bodies[0]["input_references"]
    assert len(references) == 1
    assert references[0]["type"] == "image_url"
    assert references[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_reference_falls_back_to_first_frame(tmp_path):
    caps = {
        **HAILUO3,
        "pricing_skus": {"duration_seconds": "0.13"},  # 没有 reference 计价项
    }
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"fake")
    recorder = Recorder(models=[caps])
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    assert await provider.frame_mode() == "first_frame"
    # 只有定妆照、没有关键帧：定妆照绝不当首帧（否则每段都从一张证件照开场）
    await provider.generate(request_for(tmp_path, reference_image=ref))
    assert "frame_images" not in recorder.bodies[0]
    assert "input_references" not in recorder.bodies[0]

    # 有关键帧：关键帧当首帧
    keyframe = tmp_path / "keyframe.png"
    keyframe.write_bytes(b"fake-keyframe")
    await provider.generate(request_for(tmp_path, reference_image=ref, first_frame=keyframe))
    frames = recorder.bodies[1]["frame_images"]
    assert frames[0]["frame_type"] == "first_frame"
    assert "input_references" not in recorder.bodies[1]


@pytest.mark.asyncio
async def test_frame_mode_follows_capabilities():
    recorder = Recorder(models=[HAILUO3, KLING_STD, {"id": "x/no-images"}])
    assert await make_provider(recorder).frame_mode() == "references"
    assert await make_provider(recorder, model="kwaivgi/kling-v3.0-std").frame_mode() == "none"
    kling = {**KLING_STD, "id": "k/frames", "supported_frame_images": ["first_frame"]}
    recorder = Recorder(models=[kling])
    assert await make_provider(recorder, model="k/frames").frame_mode() == "first_frame"
    off = make_provider(recorder, model="k/frames", use_reference_images=False)
    assert await off.frame_mode() == "none"


@pytest.mark.asyncio
async def test_multiple_references_are_all_sent(tmp_path):
    """双人镜头：两个角色的参考图都要进 input_references（只锁一个等于没锁另一个）。"""
    ref_a = tmp_path / "char_1.png"
    ref_a.write_bytes(b"\x89PNG\r\n\x1a\nfake-a")
    ref_b = tmp_path / "char_2.png"
    ref_b.write_bytes(b"\x89PNG\r\n\x1a\nfake-b")
    recorder = Recorder()
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    await provider.generate(request_for(tmp_path, reference_images=[ref_a, ref_b]))

    refs = recorder.bodies[0]["input_references"]
    assert len(refs) == 2
    assert all(item["type"] == "image_url" for item in refs)
    assert refs[0]["image_url"]["url"] != refs[1]["image_url"]["url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_first_frame", [False, True])
async def test_rejected_images_raise_instead_of_silently_dropping(tmp_path, use_first_frame):
    """图被拒（真实事故：Seedance 把写实人脸首帧判成隐私信息）：抛 `VideoReferenceRejected`，
    不能悄悄去掉图再交一次 —— 那样片子照出、钱照花，一致性却没了。"""
    image = tmp_path / "img.png"
    image.write_bytes(b"fake")
    caps = {**HAILUO3, "pricing_skus": {"duration_seconds": "0.13"}} if use_first_frame else HAILUO3
    bodies: list[dict] = []
    # OpenRouter 把上游错误转义后包在 message 里
    upstream = (
        'HTTP 400: {"error":{"code":"InputImageSensitiveContentDetected.PrivacyInformation",'
        '"message":"The request failed because the input image may contain real person."}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/videos/models"):
            return httpx.Response(200, json={"data": [caps]})
        if path.endswith("/videos") and request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {"message": upstream}})
        raise AssertionError(f"不该走到这里：{path}")

    provider = OpenRouterVideoProvider(
        api_key="test-key",
        model="minimax/hailuo-3",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    kwargs = {"first_frame": image} if use_first_frame else {"reference_images": [image, image]}
    with pytest.raises(VideoReferenceRejected) as exc:
        await provider.generate(request_for(tmp_path, **kwargs))

    assert exc.value.retryable is False
    assert exc.value.kind == ("first_frame" if use_first_frame else "references")
    assert exc.value.code == "InputImageSensitiveContentDetected.PrivacyInformation"
    assert len(bodies) == 1, "被拒后不能再提交（不降级、不去图）"


# ── 工厂 / 与流水线、成本账本的联动 ────────────────────


def test_factory_returns_openrouter_provider(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "video_provider", "openrouter")
    provider = get_video_provider()
    assert isinstance(provider, OpenRouterVideoProvider)
    assert provider.model == settings.openrouter_video_model
    assert provider.supports_reference_image is True

    monkeypatch.setattr(settings, "video_provider", "mock")
    assert isinstance(get_video_provider(), MockVideoProvider)

    monkeypatch.setattr(settings, "video_provider", "nope")
    with pytest.raises(VideoProviderError):
        get_video_provider()


def _pipeline_with_openrouter(store: FileStore, recorder: Recorder, **kwargs) -> Pipeline:
    def factory() -> OpenRouterVideoProvider:
        provider = make_provider(recorder, **kwargs)
        provider.poll_interval_sec = 0.01
        return provider

    return Pipeline(
        store,
        llm_factory=lambda: FakeLLM(scenes=1),
        video_factory=factory,
    )


def _project(store: FileStore, *, shots: int = 2) -> Project:
    from videogen.models.project import ProjectStatus

    project = Project(title="真实 provider 测试", input_text="x", target_duration_sec=shots * 3)
    project.status = ProjectStatus.STORYBOARD_CONFIRMED
    store.save_project(project)
    store.save_storyboard(
        project.id,
        Storyboard(
            shots=[
                Shot(
                    id=f"scene_1_shot_{i}",
                    scene_id="scene_1",
                    shot_no=i,
                    duration_sec=3,
                    video_prompt="medium shot",
                )
                for i in range(1, shots + 1)
            ]
        ),
    )
    return project


@pytest.mark.asyncio
async def test_pipeline_generates_with_real_provider_shape(store):
    recorder = Recorder()
    pipeline = _pipeline_with_openrouter(store, recorder)
    project = _project(store, shots=2)

    job = await pipeline.start_video_generation(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=30)
    assert finished.status.value == "succeeded", finished.error

    manifest = store.load_video_manifest(project.id)
    assert manifest.provider == "openrouter"
    assert manifest.model == "minimax/hailuo-3"
    assert len(manifest.ready_clips) == 2
    assert all(c.task_id == "job-1" for c in manifest.ready_clips)
    assert recorder.submits == 2  # 两个镜头各提交一次


@pytest.mark.asyncio
async def test_cost_uses_provider_price_and_billed_seconds(store):
    """真实 provider 的单价与计费档位要进账本：3 秒镜头按 5 秒 × $0.13 记。"""
    recorder = Recorder()
    pipeline = _pipeline_with_openrouter(store, recorder)
    project = _project(store, shots=2)

    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=30)

    report = store.load_cost_report(project.id)
    entry = next(e for e in report.entries if e.stage.value == "video")
    assert entry.units == 10.0, "两个 3 秒镜头按模型最短 5 秒计费"
    assert entry.usd == pytest.approx(1.30), "10 秒 × $0.13"
    assert entry.model == "minimax/hailuo-3"


@pytest.mark.asyncio
async def test_budget_gate_uses_provider_price(store, monkeypatch):
    recorder = Recorder()
    pipeline = _pipeline_with_openrouter(store, recorder)
    project = _project(store, shots=2)
    monkeypatch.setattr(get_settings(), "budget_usd", 1.0)  # 只够 7.7 秒

    with pytest.raises(BudgetExceeded) as exc:
        await pipeline.start_video_generation(project.id)
    assert "$1.30" in str(exc.value) or "1.3" in str(exc.value)
    assert recorder.submits == 0, "预算不足时不能真的提交任务"


@pytest.mark.asyncio
async def test_reference_image_reaches_provider_through_pipeline(store):
    """角色参考图要从 bible 一路传到视频请求里（reference-to-video）。"""
    from videogen.models.bible import Character, StoryBible

    recorder = Recorder()
    pipeline = _pipeline_with_openrouter(store, recorder)
    project = _project(store, shots=1)
    (store.refs_dir(project.id) / "char_1.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    store.save_bible(
        project.id,
        StoryBible(
            characters=[
                Character(id="char_1", name="林晚", role="主角", reference_image="refs/char_1.png")
            ]
        ),
    )
    storyboard = store.load_storyboard(project.id)
    storyboard.shots[0].character_in_frame = ["char_1"]
    store.save_storyboard(project.id, storyboard)

    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=30)

    assert "input_references" in recorder.bodies[0]
    data_url = recorder.bodies[0]["input_references"][0]["image_url"]["url"]
    assert data_url.startswith("data:image/png")


def test_cost_service_price_override():
    entry = cost_service.video_entry("openrouter", "minimax/hailuo-3", 10, 2, price_per_second=0.13)
    assert entry.usd == pytest.approx(1.3)
    entry_default = cost_service.video_entry("mock", "mock", 10, 2, price_per_second=None)
    assert entry_default.usd == 0.0  # 默认单价 0


# ── 下载鉴权 / 实际扣费 / 不重复计费（真实调用踩出来的坑）──────


@pytest.mark.asyncio
async def test_download_prefers_authenticated_content_endpoint(tmp_path):
    """unsigned_urls 裸下载会 401，必须优先走带鉴权的 content 端点。"""
    recorder = Recorder()
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    await provider.generate(request_for(tmp_path))

    downloads = [r for r in recorder.requests if "/content" in r.url.path]
    assert downloads, "应该请求过 content 端点"
    assert downloads[0].headers.get("authorization") == "Bearer test-key-not-a-real-secret"
    assert downloads[0].url.params.get("index") == "0"


@pytest.mark.asyncio
async def test_download_falls_back_to_unsigned_url_with_auth(tmp_path):
    recorder = Recorder(content_ok=False)
    provider = make_provider(recorder)
    provider.poll_interval_sec = 0.01

    clip = await provider.generate(request_for(tmp_path))

    assert clip.path.is_file()
    cdn_requests = [r for r in recorder.requests if r.url.host == "cdn"]
    assert cdn_requests, "content 端点失败后应该回退到 unsigned_urls"
    assert cdn_requests[0].headers.get("authorization", "").startswith("Bearer ")


@pytest.mark.asyncio
async def test_download_failure_is_not_retryable_and_carries_job_id(tmp_path):
    """生成成功后的下载失败必须标记为不可重试（重试=再买一次）。"""
    recorder = Recorder(content_ok=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn":
            return httpx.Response(403, json={"error": {"message": "forbidden"}})
        return recorder.handler(request)

    provider = OpenRouterVideoProvider(
        api_key="test-key",
        model="minimax/hailuo-3",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider.poll_interval_sec = 0.01

    with pytest.raises(VideoArtifactError) as exc:
        await provider.generate(request_for(tmp_path))

    assert exc.value.retryable is False
    assert exc.value.job_id == "job-1"
    assert exc.value.urls, "要留下候选地址方便人工捞回"
    assert not (tmp_path / "clip.mp4").exists()


@pytest.mark.asyncio
async def test_video_service_does_not_regenerate_after_artifact_failure(store):
    """回归：下载失败时只能提交一次 —— 之前重试 3 次白烧了 3 份钱。"""
    recorder = Recorder(content_ok=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn":
            return httpx.Response(401, json={"error": {"message": "unauthorized"}})
        return recorder.handler(request)

    def factory() -> OpenRouterVideoProvider:
        provider = OpenRouterVideoProvider(
            api_key="test-key",
            model="minimax/hailuo-3",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        provider.poll_interval_sec = 0.01
        return provider

    pipeline = Pipeline(store, llm_factory=lambda: FakeLLM(scenes=1), video_factory=factory)
    project = _project(store, shots=1)

    job = await pipeline.start_video_generation(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=30)

    assert finished.status.value == "failed"
    assert recorder.submits == 1, "已经生成成功过，绝不能再提交一次"
    clip = store.load_video_manifest(project.id).clips[0]
    assert clip.status.value == "failed"
    assert clip.task_id == "job-1", "失败也要留下 job id 方便人工捞回"
    assert "job-1" in clip.error


@pytest.mark.asyncio
async def test_generation_failure_is_still_retried(store):
    """真正的生成失败（还没出片、没扣费）仍然要重试。"""
    recorder = Recorder(submit_status=500)

    def factory() -> OpenRouterVideoProvider:
        provider = make_provider(recorder)
        provider.poll_interval_sec = 0.01
        provider.max_http_retries = 1
        return provider

    pipeline = Pipeline(store, llm_factory=lambda: FakeLLM(scenes=1), video_factory=factory)
    project = _project(store, shots=1)

    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=30)

    assert recorder.submits >= 1
    clip = store.load_video_manifest(project.id).clips[0]
    assert clip.status.value == "failed"


@pytest.mark.asyncio
async def test_actual_cost_from_usage_is_recorded(store):
    """provider 报的实际扣费要进账本（比价目表推算更准）。"""
    recorder = Recorder(cost=0.83)

    def factory() -> OpenRouterVideoProvider:
        provider = make_provider(recorder)
        provider.poll_interval_sec = 0.01
        return provider

    pipeline = Pipeline(store, llm_factory=lambda: FakeLLM(scenes=1), video_factory=factory)
    project = _project(store, shots=1)

    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=30)

    clip = store.load_video_manifest(project.id).clips[0]
    assert clip.cost_usd == pytest.approx(0.83)

    entry = next(e for e in store.load_cost_report(project.id).entries if e.stage.value == "video")
    assert entry.usd == pytest.approx(0.83), "有实际扣费时不要用价目表推算"
    assert entry.units == 5.0


def test_video_entry_prefers_actual_cost():
    entry = cost_service.video_entry(
        "openrouter", "minimax/hailuo-3", 5, 1, price_per_second=0.13, actual_usd=0.83
    )
    assert entry.usd == pytest.approx(0.83)
    fallback = cost_service.video_entry(
        "openrouter", "minimax/hailuo-3", 5, 1, price_per_second=0.13
    )
    assert fallback.usd == pytest.approx(0.65)


def test_fit_prompt_drops_generic_directives_before_truncating():
    from videogen.config import get_settings
    from videogen.providers.video.openrouter import fit_prompt, prompt_limit

    settings = get_settings()
    shots = 'Shot 1 (0-2s): a man lies on a bed, says: "我起来了。" ' * 30
    prompt = f"{shots}{settings.video_background_directive} {settings.video_performance_directive}"
    assert prompt_limit("kwaivgi/kling-v3.0-std") == 2500
    assert prompt_limit("minimax/hailuo-3") == 0
    assert fit_prompt(prompt, 0) == prompt

    limit = len(shots) + 20
    fitted = fit_prompt(prompt, limit)
    assert len(fitted) <= limit
    assert settings.video_background_directive not in fitted
    assert fitted.startswith(shots.strip())  # 逐镜头内容原样保留

    tight = fit_prompt(prompt, 300)
    assert len(tight) <= 300
    assert tight.endswith(".") or tight.endswith('"')  # 按句截断，不截半个词
