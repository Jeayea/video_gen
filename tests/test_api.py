from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeLLM
from videogen import main
from videogen.orchestrator.pipeline import Pipeline
from videogen.utils.file_store import FileStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "projects")
    llm = FakeLLM()
    pipeline = Pipeline(store, llm_factory=lambda: llm)
    # 端点运行时从模块全局取 store/pipeline，这里替换成临时目录的实例
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "pipeline", pipeline)
    with TestClient(main.app) as test_client:
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


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["stages_implemented"] == ["bible", "script", "storyboard"]
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
