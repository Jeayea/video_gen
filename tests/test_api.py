from __future__ import annotations

import base64
import time

import pytest
from fastapi.testclient import TestClient

from tests.factories import small_project
from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen import main
from videogen.orchestrator.pipeline import Pipeline
from videogen.providers.tts.mock import MockTTSProvider
from videogen.providers.video.mock import MockVideoProvider
from videogen.utils.file_store import FileStore

#: 一个最小的假 PNG（base64），参考图上传用例用
PNG_BASE64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fake-image").decode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "projects")
    llm = FakeLLM()
    video = FakeVideoProvider()
    pipeline = Pipeline(
        store,
        llm_factory=lambda: llm,
        video_factory=lambda: video,
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    # 端点运行时从模块全局取 store/pipeline，这里替换成临时目录的实例
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "pipeline", pipeline)
    with TestClient(main.app) as test_client:
        # 测试里要检查产物/调用次数，挂到 client 上方便取用
        test_client.videogen_store = store
        test_client.videogen_pipeline = pipeline
        test_client.videogen_video = video
        yield test_client


@pytest.fixture
def media_client(tmp_path, monkeypatch):
    """产出**真实**媒体文件的客户端（mock 视频 + mock 配音），合成用例用它。"""
    store = FileStore(tmp_path / "projects")
    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: MockVideoProvider(),
        tts_factory=lambda: MockTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "pipeline", pipeline)
    with TestClient(main.app) as test_client:
        test_client.videogen_store = store
        test_client.videogen_pipeline = pipeline
        yield test_client


def create_project(client: TestClient, **overrides) -> dict:
    body = {
        "title": "测试短剧",
        "input_mode": "theme",
        "input_text": "一个被裁员的程序员靠 AI 逆袭创业",
        "aspect_ratio": "9:16",
        "target_duration_sec": 45,
    }
    body.update(overrides)
    resp = client.post("/api/projects", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def prepare_confirmed_storyboard(client: TestClient) -> str:
    """走完文本链路，返回已确认分镜的项目 id。"""
    pid = create_project(client)["id"]
    assert client.post(f"/api/projects/{pid}/bible/generate").status_code == 200
    assert client.post(f"/api/projects/{pid}/script/generate").status_code == 200
    assert client.post(f"/api/projects/{pid}/script/confirm").status_code == 200
    assert client.post(f"/api/projects/{pid}/storyboard/generate").status_code == 200
    assert client.post(f"/api/projects/{pid}/storyboard/confirm").status_code == 200
    return pid


def wait_video_job(client: TestClient, project_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/projects/{project_id}/video/status").json()
        job = status.get("job")
        if job and not job["is_active"]:
            return status
        time.sleep(0.02)
    raise AssertionError("视频任务未在超时内结束")


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["stages_implemented"] == [
        "bible",
        "script",
        "storyboard",
        "video",
        "audio",
        "compose",
    ]
    assert data["stages_pending"] == []
    assert data["tts_provider"] == "edge"
    assert data["subtitle_mode"] in {"soft", "burn", "none"}
    assert "llm_key_configured" in data
    # 绝不能把密钥暴露给前端
    assert "key" not in {k for k in data if "configured" not in k}


def test_health_reports_burn_and_image_capabilities(client):
    """字幕能不能烧、角色图用什么模型，都要能从 /health 看出来（都不含密钥）。"""
    data = client.get("/api/health").json()
    assert data["image_provider"] == "mock"  # 测试基线强制 mock
    assert data["image_model"]
    assert data["image_price_usd"] >= 0
    assert isinstance(data["ffmpeg_can_burn_subtitles"], bool)
    assert data["ffmpeg_auto_upgrade"] is True
    if data["ffmpeg_can_burn_subtitles"]:
        assert data["ffmpeg_burn_path"]  # 能烧就必须告诉前端用哪个二进制
    else:
        assert data["ffmpeg_burn_path"] == ""


def test_index_and_static_mounted(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/index.html").status_code == 200


def test_create_project_requires_input_text(client):
    resp = client.post("/api/projects", json={"input_text": ""})
    assert resp.status_code == 422


def test_duration_bounds(client):
    too_short = client.post("/api/projects", json={"input_text": "x", "target_duration_sec": 1})
    too_long = client.post("/api/projects", json={"input_text": "x", "target_duration_sec": 9999})
    assert too_short.status_code == 422
    assert too_long.status_code == 422


def test_full_flow_through_api(client):
    project = create_project(client)
    pid = project["id"]

    assert client.post(f"/api/projects/{pid}/bible/generate").status_code == 200
    assert client.get(f"/api/projects/{pid}/bible").status_code == 200
    assert client.get(f"/api/projects/{pid}/outline").status_code == 404  # 还没生成剧本

    resp = client.post(f"/api/projects/{pid}/script/generate")
    assert resp.status_code == 200, resp.text
    script = resp.json()
    assert sum(s["duration_sec"] for s in script["scenes"]) == 45
    assert client.get(f"/api/projects/{pid}/outline").status_code == 200

    # 分镜需要先确认剧本
    assert client.post(f"/api/projects/{pid}/storyboard/generate").status_code == 400

    confirmed = client.post(f"/api/projects/{pid}/script/confirm").json()
    assert confirmed["project_status"] == "script_confirmed"

    # 确认后不能再改剧本 / 再生成剧本
    assert client.put(f"/api/projects/{pid}/script", json=script).status_code == 400
    assert client.post(f"/api/projects/{pid}/script/generate").status_code == 400

    # 退回后可以
    assert client.post(f"/api/projects/{pid}/script/reopen").json()["project_status"] == "draft"
    assert client.post(f"/api/projects/{pid}/script/generate").status_code == 200
    client.post(f"/api/projects/{pid}/script/confirm")

    resp = client.post(f"/api/projects/{pid}/storyboard/generate")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["shots"]) == 10

    report = client.get(f"/api/projects/{pid}/consistency").json()
    assert report["stage"] == "script+storyboard"

    # P0-1 回归：确认分镜后是 storyboard_confirmed，而不是永远卡死的 video_ready
    done = client.post(f"/api/projects/{pid}/storyboard/confirm").json()
    assert done["project_status"] == "storyboard_confirmed"
    assert client.get(f"/api/projects/{pid}").json()["status"] == "storyboard_confirmed"
    # 仍然可以退回去改
    assert client.post(f"/api/projects/{pid}/storyboard/reopen").status_code == 200


def test_error_message_uses_status_value(client):
    project = create_project(client)
    pid = project["id"]
    resp = client.post(f"/api/projects/{pid}/script/confirm")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "draft" in detail  # 用 status.value，而不是 ProjectStatus.DRAFT


def test_unknown_project_returns_404(client):
    assert client.get("/api/projects/doesnotexist").status_code == 404
    assert client.post("/api/projects/doesnotexist/script/generate").status_code == 404
    assert client.delete("/api/projects/doesnotexist").status_code == 404


def test_delete_project(client):
    project = create_project(client)
    pid = project["id"]
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert client.get("/api/projects").json() == []


def test_update_project_metadata(client):
    project = create_project(client)
    pid = project["id"]
    resp = client.put(f"/api/projects/{pid}", json={"title": "改名了", "target_duration_sec": 60})
    assert resp.status_code == 200
    assert resp.json()["title"] == "改名了"
    assert resp.json()["target_duration_sec"] == 60


def test_consistency_revalidate(client):
    project = create_project(client)
    pid = project["id"]
    client.post(f"/api/projects/{pid}/bible/generate")
    client.post(f"/api/projects/{pid}/script/generate")
    resp = client.post(f"/api/projects/{pid}/consistency/revalidate")
    assert resp.status_code == 200
    assert "issues" in resp.json()


def test_api_token_enforced_when_configured(tmp_path, monkeypatch):
    from videogen.config import Settings

    store = FileStore(tmp_path / "projects")
    pipeline = Pipeline(store, llm_factory=FakeLLM)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "pipeline", pipeline)
    monkeypatch.setattr(main, "settings", Settings(api_token="s3cret", _env_file=None))

    with TestClient(main.app) as test_client:
        good = {"X-API-Token": "s3cret"}
        assert test_client.get("/api/health").status_code == 200  # health 免鉴权
        assert test_client.get("/api/projects").status_code == 401
        assert test_client.get("/api/projects", headers=good).status_code == 200
        assert test_client.get("/api/projects?token=s3cret").status_code == 200
        assert test_client.get("/api/projects", headers={"X-API-Token": "wrong"}).status_code == 401


# ── 视频阶段（后台任务）────────────────────────────────


def test_video_generate_requires_confirmed_storyboard(client):
    pid = create_project(client)["id"]
    client.post(f"/api/projects/{pid}/bible/generate")
    client.post(f"/api/projects/{pid}/script/generate")
    client.post(f"/api/projects/{pid}/script/confirm")
    client.post(f"/api/projects/{pid}/storyboard/generate")  # 还没确认

    resp = client.post(f"/api/projects/{pid}/video/generate")
    assert resp.status_code == 400
    assert "storyboard_review" in resp.json()["detail"]


def test_video_flow_through_api(client):
    pid = prepare_confirmed_storyboard(client)
    expected = len(client.get(f"/api/projects/{pid}/storyboard").json()["shots"])

    # 生成前状态
    before = client.get(f"/api/projects/{pid}/video/status").json()
    assert before["project_status"] == "storyboard_confirmed"
    assert before["manifest"] is None

    resp = client.post(f"/api/projects/{pid}/video/generate")
    assert resp.status_code == 202
    job_id = resp.json()["job"]["id"]
    assert resp.json()["job"]["kind"] == "video"

    status = wait_video_job(client, pid)
    assert status["job"]["status"] == "succeeded"
    assert status["job"]["percent"] == 100
    assert status["project_status"] == "video_ready"
    assert status["summary"]["ready"] == expected
    assert status["summary"]["usable"] is True

    # Job 查询接口
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "succeeded"
    assert client.get(f"/api/projects/{pid}/jobs").json()[0]["id"] == job_id

    # 片段可下载
    manifest = client.get(f"/api/projects/{pid}/video/manifest").json()
    first = manifest["clips"][0]
    clip_resp = client.get(f"/api/projects/{pid}/clips/{first['shot_id']}")
    assert clip_resp.status_code == 200
    assert clip_resp.headers["content-type"].startswith("video/mp4")
    assert clip_resp.content

    # 再点一次：已就绪的片段不重复生成（续跑）
    calls_before = len(client.videogen_video.calls)
    assert client.post(f"/api/projects/{pid}/video/generate").status_code == 202
    wait_video_job(client, pid)
    assert len(client.videogen_video.calls) == calls_before


def test_video_regenerate_only_failed_clips(client):
    pid = prepare_confirmed_storyboard(client)
    storyboard = client.get(f"/api/projects/{pid}/storyboard").json()
    failed_shot = storyboard["shots"][0]["id"]
    client.videogen_video.fail_shots = {failed_shot}
    client.videogen_video.fail_times = 99

    client.post(f"/api/projects/{pid}/video/generate")
    status = wait_video_job(client, pid)
    assert status["summary"]["failed"] == 1
    assert status["summary"]["ready"] == len(storyboard["shots"]) - 1
    assert status["project_status"] == "video_ready"  # 部分失败也能进下一步

    # 修好 provider 后重试：只有失败的那个镜头被重新生成
    client.videogen_video.fail_shots = set()
    calls_before = len(client.videogen_video.calls)
    client.post(f"/api/projects/{pid}/video/generate")
    status = wait_video_job(client, pid)
    assert len(client.videogen_video.calls) == calls_before + 1
    assert status["summary"]["failed"] == 0


def test_video_cancel_through_api(client):
    pid = prepare_confirmed_storyboard(client)
    client.videogen_video.delay = 0.3

    job_id = client.post(f"/api/projects/{pid}/video/generate").json()["job"]["id"]
    time.sleep(0.05)
    assert client.post(f"/api/projects/{pid}/video/cancel").json()["cancelled"] == 1
    time.sleep(0.2)

    status = client.get(f"/api/projects/{pid}/video/status").json()
    assert status["job"]["status"] == "cancelled"
    # 没有可用片段 → 退回可重试状态
    assert status["project_status"] == "storyboard_confirmed"
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"


def test_video_duplicate_job_rejected_through_api(client):
    pid = prepare_confirmed_storyboard(client)
    client.videogen_video.delay = 0.3

    assert client.post(f"/api/projects/{pid}/video/generate").status_code == 202
    resp = client.post(f"/api/projects/{pid}/video/generate")
    assert resp.status_code == 400
    assert "已有视频生成任务" in resp.json()["detail"]
    client.post(f"/api/projects/{pid}/video/cancel")
    time.sleep(0.25)


def test_video_endpoints_unknown_project(client):
    assert client.post("/api/projects/nope/video/generate").status_code == 404
    assert client.get("/api/projects/nope/video/status").status_code == 404
    assert client.get("/api/projects/nope/jobs").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_clip_endpoint_404_when_no_manifest(client):
    pid = prepare_confirmed_storyboard(client)
    assert client.get(f"/api/projects/{pid}/clips/shot_1").status_code == 404
    assert client.get(f"/api/projects/{pid}/video/manifest").status_code == 404


def test_health_reports_video_stage(client):
    data = client.get("/api/health").json()
    assert "video" in data["stages_implemented"]
    assert "audio" in data["stages_implemented"]
    assert data["stages_pending"] == []
    assert data["video_provider"] == "mock"
    assert "ffmpeg_available" in data
    assert "ffprobe_available" in data
    assert "bgm_enabled" in data


# ── 配音 / 合成 API ───────────────────────────────────


def wait_audio_job(client: TestClient, project_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/projects/{project_id}/audio/status").json()
        job = status.get("job")
        if job and not job["is_active"]:
            return status
        time.sleep(0.02)
    raise AssertionError("配音任务没有在超时前结束")


def wait_compose_job(client: TestClient, project_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/projects/{project_id}/compose/status").json()
        job = status.get("job")
        if job and not job["is_active"]:
            return status
        time.sleep(0.05)
    raise AssertionError("合成任务没有在超时前结束")


def test_audio_endpoints_full_flow(client):
    pid = prepare_confirmed_storyboard(client)
    assert client.post(f"/api/projects/{pid}/video/generate").status_code == 202
    wait_video_job(client, pid)

    assert client.get(f"/api/projects/{pid}/audio/manifest").status_code == 404
    resp = client.post(f"/api/projects/{pid}/audio/generate", json={"voices": {}})
    assert resp.status_code == 202
    assert resp.json()["job"]["kind"] == "audio"

    status = wait_audio_job(client, pid)
    assert status["job"]["status"] == "succeeded"
    assert status["summary"]["usable"] is True
    assert status["manifest"]["clips"]
    assert status["voice_map"]

    manifest = client.get(f"/api/projects/{pid}/audio/manifest").json()
    clip_id = manifest["clips"][0]["id"]
    audio_resp = client.get(f"/api/projects/{pid}/audio/{clip_id}")
    assert audio_resp.status_code == 200
    assert len(audio_resp.content) > 0

    assert client.get(f"/api/projects/{pid}/audio/不存在的条目").status_code == 404


def test_audio_script_mode_request(client, monkeypatch):
    """配音文案模式能通过 API 传下去，非法值被挡在门口，默认不调 LLM。"""
    from videogen.config import get_settings

    monkeypatch.setattr(get_settings(), "audio_llm_vision", False)
    pid = prepare_confirmed_storyboard(client)
    assert client.post(f"/api/projects/{pid}/video/generate").status_code == 202
    wait_video_job(client, pid)

    # 非法模式直接 422（配置笔误不该悄悄花钱）
    bad = client.post(f"/api/projects/{pid}/audio/generate", json={"script_mode": "fll"})
    assert bad.status_code == 422

    resp = client.post(
        f"/api/projects/{pid}/audio/generate",
        json={"script_mode": "narration", "use_vision": False},
    )
    assert resp.status_code == 202, resp.text
    status = wait_audio_job(client, pid)
    assert status["job"]["status"] == "succeeded"
    assert status["manifest"]["script_mode"] == "narration"
    assert status["manifest"]["script_lines"] > 0
    assert status["summary"]["script_lines"] > 0
    # 旁白文案确实被写进了分镜
    storyboard = client.get(f"/api/projects/{pid}/storyboard").json()
    assert any(shot.get("voice_script_source") == "llm:narration" for shot in storyboard["shots"])

    health = client.get("/api/health").json()
    assert health["audio_script_mode"] == "off"
    assert "audio_script_model" in health


def test_audio_default_does_not_call_llm(client):
    pid = prepare_confirmed_storyboard(client)
    assert client.post(f"/api/projects/{pid}/video/generate").status_code == 202
    wait_video_job(client, pid)
    assert client.post(f"/api/projects/{pid}/audio/generate", json={}).status_code == 202
    status = wait_audio_job(client, pid)
    assert status["manifest"]["script_mode"] == "off"
    assert status["manifest"]["script_lines"] == 0


def test_audio_generate_requires_video(client):
    pid = prepare_confirmed_storyboard(client)
    resp = client.post(f"/api/projects/{pid}/audio/generate")
    assert resp.status_code == 400
    assert "视频" in resp.json()["detail"]


def test_audio_unknown_project(client):
    assert client.get("/api/projects/nope/audio/status").status_code == 404
    assert client.post("/api/projects/nope/audio/generate").status_code == 404
    assert client.get("/api/projects/nope/audio/manifest").status_code == 404


def test_compose_endpoints_full_flow(media_client):
    client = media_client
    if not client.get("/api/health").json()["ffmpeg_available"]:
        pytest.skip("环境里没有 ffmpeg，跳过真实合成 API 用例")

    # 用手工小项目，避免走 LLM 生成 10 个镜头（合成会很慢）
    pid = small_project(client.videogen_store, shots=2).id
    assert client.post(f"/api/projects/{pid}/video/generate").status_code == 202
    wait_video_job(client, pid)
    assert client.post(f"/api/projects/{pid}/audio/generate").status_code == 202
    wait_audio_job(client, pid)

    resp = client.post(f"/api/projects/{pid}/compose", json={"subtitle_mode": "soft"})
    assert resp.status_code == 202
    assert resp.json()["job"]["kind"] == "compose"

    status = wait_compose_job(client, pid)
    assert status["job"]["status"] == "succeeded", status["job"]["error"]
    assert status["summary"]["segments"] > 0
    assert client.get(f"/api/projects/{pid}").json()["status"] == "done"

    final = client.get(f"/api/projects/{pid}/final")
    assert final.status_code == 200
    assert final.headers["content-type"] == "video/mp4"
    assert len(final.content) > 1000

    subs = client.get(f"/api/projects/{pid}/subtitles")
    assert subs.status_code == 200
    assert "林晚" in subs.text or "-->" in subs.text


def test_compose_requires_audio(client):
    pid = prepare_confirmed_storyboard(client)
    assert client.post(f"/api/projects/{pid}/video/generate").status_code == 202
    wait_video_job(client, pid)

    resp = client.post(f"/api/projects/{pid}/compose")
    assert resp.status_code == 400
    assert "配音" in resp.json()["detail"]


def test_compose_endpoints_404_without_artifacts(client):
    pid = prepare_confirmed_storyboard(client)
    assert client.get(f"/api/projects/{pid}/final").status_code == 404
    assert client.get(f"/api/projects/{pid}/subtitles").status_code == 404
    assert client.get("/api/projects/nope/compose/status").status_code == 404
    assert client.post("/api/projects/nope/compose").status_code == 404


def test_compose_rejects_invalid_subtitle_mode(client):
    pid = prepare_confirmed_storyboard(client)
    resp = client.post(f"/api/projects/{pid}/compose", json={"subtitle_mode": "乱填"})
    assert resp.status_code == 422


# ── 模型 / 成本 / 参考图 / 批量（Phase B）────────────────


def test_models_endpoint(client):
    data = client.get("/api/models").json()
    assert data["default"]
    assert data["default"] in data["candidates"]
    assert "llm_input_per_mtok" in data["prices"]
    assert "budget_usd" in data


def test_cost_endpoint(client):
    pid = create_project(client)["id"]
    assert client.post(f"/api/projects/{pid}/bible/generate").status_code == 200

    data = client.get(f"/api/projects/{pid}/cost").json()
    assert data["summary"]["total_usd"] >= 0
    assert data["summary"]["total_tokens"] > 0
    assert "bible" in data["stages"]
    assert "video_usd" in data["estimate"]

    assert client.get("/api/projects/nope/cost").status_code == 404


def test_generate_accepts_model_override(client):
    pid = create_project(client)["id"]
    resp = client.post(
        f"/api/projects/{pid}/bible/generate", json={"model": "anthropic/claude-3.5-haiku"}
    )
    assert resp.status_code == 200

    report = client.get(f"/api/projects/{pid}/cost").json()
    assert report["summary"]["entries"] == 1
    assert report["stages"]["bible"]["tokens"] > 0
    assert report["budget_usd"] == main.settings.budget_usd


def test_reference_image_flow(client):
    pid = prepare_confirmed_storyboard(client)

    payload = {"filename": "lin.png", "content_base64": PNG_BASE64}
    resp = client.post(f"/api/projects/{pid}/characters/char_1/reference", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "refs/char_1.png"

    bible = client.get(f"/api/projects/{pid}/bible").json()
    assert bible["characters"][0]["reference_image"] == "refs/char_1.png"

    image = client.get(f"/api/projects/{pid}/characters/char_1/reference")
    assert image.status_code == 200
    assert len(image.content) > 0

    assert client.delete(f"/api/projects/{pid}/characters/char_1/reference").status_code == 200
    assert client.get(f"/api/projects/{pid}/characters/char_1/reference").status_code == 404


def test_reference_image_validation(client, monkeypatch):
    pid = prepare_confirmed_storyboard(client)
    url = f"/api/projects/{pid}/characters/char_1/reference"

    bad_ext = client.post(url, json={"filename": "a.gif", "content_base64": PNG_BASE64})
    assert bad_ext.status_code == 400
    assert client.post(url, json={"filename": "a.png", "content_base64": "!!!"}).status_code == 400
    assert client.post(
        f"/api/projects/{pid}/characters/char_nope/reference",
        json={"filename": "a.png", "content_base64": PNG_BASE64},
    ).status_code == 404

    from videogen import main

    monkeypatch.setattr(main.settings, "reference_image_max_mb", 0.000001)
    resp = client.post(url, json={"filename": "a.png", "content_base64": PNG_BASE64})
    assert resp.status_code == 400
    assert "太大" in resp.json()["detail"]


def test_ai_reference_plan_and_generation_flow(client):
    """AI 生成角色图：plan（不花钱）→ 单个生成 → 批量补齐 → 预览 + 记账。"""
    pid = prepare_confirmed_storyboard(client)

    plan = client.get(f"/api/projects/{pid}/characters/references/plan")
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert body["missing"] == body["total"] > 0
    assert body["estimated_usd"] == pytest.approx(body["missing"] * body["price_per_image"])
    assert body["provider"] == "mock"  # 测试基线强制 mock，绝不打真实图片接口

    one = client.post(
        f"/api/projects/{pid}/characters/char_1/reference/generate", json={"force": False}
    )
    assert one.status_code == 200, one.text
    assert one.json()["reference_image"] == "refs/char_1.png"
    assert one.json()["skipped"] is False

    # 再点一次：已有图，默认跳过、不花钱
    again = client.post(
        f"/api/projects/{pid}/characters/char_1/reference/generate", json={}
    )
    assert again.json()["skipped"] is True
    assert again.json()["cost_usd"] == 0.0

    preview = client.get(f"/api/projects/{pid}/characters/char_1/reference")
    assert preview.status_code == 200
    assert preview.content.startswith(b"\x89PNG")

    batch = client.post(
        f"/api/projects/{pid}/characters/references/generate", json={"concurrency": 2}
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["skipped"] == 1  # char_1 已经有了
    assert batch.json()["failed"] == 0
    assert client.get(f"/api/projects/{pid}/characters/references/plan").json()["missing"] == 0

    cost = client.get(f"/api/projects/{pid}/cost").json()
    assert cost["stages"]["image"]["calls"] == cost["stages"]["image"]["units"]


def test_ai_reference_generation_unknown_character_and_project(client):
    pid = prepare_confirmed_storyboard(client)
    assert client.post(
        f"/api/projects/{pid}/characters/char_nope/reference/generate", json={}
    ).status_code == 404
    assert client.post(
        "/api/projects/nope/characters/references/generate", json={}
    ).status_code == 404
    assert client.get("/api/projects/nope/characters/references/plan").status_code == 404


def test_subtitles_vtt_is_served_for_old_projects(client):
    """老项目只有 srt 时，`subtitles.vtt` 要能就地转出来（不用重新合成）。"""
    pid = prepare_confirmed_storyboard(client)
    store = client.videogen_store
    output = store.output_dir(pid)
    srt = (
        "1\n00:00:00,000 --> 00:00:02,500\n林悦：你好\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\n李薇儿：再见\n"
    )
    (output / "subtitles.srt").write_text(srt, encoding="utf-8")
    from videogen.models.compose import ComposeReport

    store.save_compose_report(
        pid,
        ComposeReport(
            output_path="output/final.mp4",
            subtitles_path="output/subtitles.srt",
            duration_sec=5.0,
        ),
    )

    resp = client.get(f"/api/projects/{pid}/subtitles.vtt")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/vtt")
    assert resp.text.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.500" in resp.text  # 逗号已经换成点号
    assert "林悦：你好" in resp.text

    # 没有合成报告时是 404，而不是空字幕
    other = prepare_confirmed_storyboard(client)
    assert client.get(f"/api/projects/{other}/subtitles.vtt").status_code == 404


def wait_batch(client: TestClient, batch_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/batches/{batch_id}").json()
        if not data["is_active"]:
            return data
        time.sleep(0.02)
    raise AssertionError("批量任务没有在超时前结束")


def test_batch_endpoints(client):
    assert client.get("/api/batches/stages").json()["stop_after"][0] == "storyboard"

    resp = client.post(
        "/api/batches",
        json={
            "items": [{"input_text": "逆袭故事", "title": "批量A"}, {"input_text": "复仇故事"}],
            "target_duration_sec": 30,
            "stop_after": "storyboard",
        },
    )
    assert resp.status_code == 202, resp.text
    batch = resp.json()
    assert batch["summary"]["total"] == 2

    done = wait_batch(client, batch["id"])
    assert done["status"] == "succeeded"
    assert done["percent"] == 100
    assert done["summary"]["succeeded"] == 2
    for item in done["items"]:
        assert item["project_id"]
        assert client.get(f"/api/projects/{item['project_id']}").json()["status"] == (
            "storyboard_confirmed"
        )

    assert len(client.get("/api/batches").json()) >= 1
    assert client.get("/api/batches/nope").status_code == 404
    assert client.post("/api/batches/nope/cancel").json()["cancelled"] is False


def test_batch_endpoint_validation(client):
    assert client.post("/api/batches", json={"items": []}).status_code == 422
    resp = client.post(
        "/api/batches",
        json={"items": [{"input_text": "x"}], "stop_after": "乱填"},
    )
    assert resp.status_code == 400
    assert "stop_after" in resp.json()["detail"]


def test_health_reports_budget_and_batch(client):
    data = client.get("/api/health").json()
    assert "budget_usd" in data
    assert "active_batches" in data
    assert data["llm_model_candidates"]


def test_budget_exceeded_returns_400(client, monkeypatch):
    """超预算要被拒绝且返回可读原因（不是 500）。"""
    from videogen import main

    pid = prepare_confirmed_storyboard(client)
    monkeypatch.setattr(main.settings, "budget_usd", 0.0000001)

    resp = client.post(f"/api/projects/{pid}/video/generate")
    assert resp.status_code == 400
    assert "预算不足" in resp.json()["detail"]
    assert client.get(f"/api/projects/{pid}").json()["status"] == "storyboard_confirmed"


# ── 成本前置（preflight）/ 合并短镜头 ─────────────────────


def test_preflight_endpoint(client):
    pid = prepare_confirmed_storyboard(client)

    data = client.get(f"/api/projects/{pid}/preflight").json()
    assert data["action"] == "all"
    assert data["video"]["to_generate"] > 0
    assert data["audio"]["action"] == "audio"
    assert data["upcoming_usd"] >= 0
    assert "budget" in data

    only_video = client.get(f"/api/projects/{pid}/preflight?action=video").json()
    assert only_video["audio"] is None and only_video["compose"] is None


def test_merge_shots_endpoint_preview_then_apply(client):
    pid = prepare_confirmed_storyboard(client)

    before = client.get(f"/api/projects/{pid}/storyboard").json()
    preview = client.post(f"/api/projects/{pid}/storyboard/merge-shots", json={}).json()
    assert preview["dry_run"] is True
    unchanged = client.get(f"/api/projects/{pid}/storyboard").json()
    assert len(unchanged["shots"]) == len(before["shots"])

    if preview["to_shots"] < preview["from_shots"]:
        applied = client.post(
            f"/api/projects/{pid}/storyboard/merge-shots",
            json={"dry_run": False, "min_sec": 5},
        ).json()
        assert applied["dry_run"] is False
        after = client.get(f"/api/projects/{pid}/storyboard").json()
        assert len(after["shots"]) == applied["to_shots"]

    bad = client.post(
        f"/api/projects/{pid}/storyboard/merge-shots", json={"min_sec": 0}
    )
    assert bad.status_code == 422  # 0 秒没有意义，别让它悄悄什么都不做
