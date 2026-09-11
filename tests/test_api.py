from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeLLM, FakeVideoProvider
from videogen import main
from videogen.orchestrator.pipeline import Pipeline
from videogen.utils.file_store import FileStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "projects")
    llm = FakeLLM()
    video = FakeVideoProvider()
    pipeline = Pipeline(store, llm_factory=lambda: llm, video_factory=lambda: video)
    # 端点运行时从模块全局取 store/pipeline，这里替换成临时目录的实例
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "pipeline", pipeline)
    with TestClient(main.app) as test_client:
        # 测试里要检查产物/调用次数，挂到 client 上方便取用
        test_client.videogen_store = store
        test_client.videogen_pipeline = pipeline
        test_client.videogen_video = video
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
    assert data["stages_implemented"] == ["bible", "script", "storyboard", "video"]
    assert "llm_key_configured" in data
    # 绝不能把密钥暴露给前端
    assert "key" not in {k for k in data if "configured" not in k}


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
    assert data["stages_pending"] == ["audio", "compose"]
    assert data["video_provider"] == "mock"
    assert "ffmpeg_available" in data
