#!/usr/bin/env python
"""端到端验证（$0）：从「已有分镜」一路跑到 final.mp4，全程走真实 HTTP 接口。

    .venv/bin/python tools/e2e/mock_pipeline_e2e.py --source <已有项目>         # 全自动，跑完关服务
    .venv/bin/python tools/e2e/mock_pipeline_e2e.py --source <已有项目> --keep  # 保留服务

为什么是 $0：
- `VIDEO_PROVIDER=mock`（ffmpeg 真出 540x960 H.264，纯色背景 + 扫过的白条，肉眼能看出是视频）
- `TTS_PROVIDER=mock`（按字数估时长，真出一段可播放的 mp3）
- 配音文案 LLM / 视觉 / 运动全关；配乐用本机作曲；合成/响度全是本机 ffmpeg
- 服务起在 8099 + 独立的 PROJECTS_DIR，**绝不碰 projects/ 里的真实项目**

为什么从「分镜已确认」开始：文本链路（圣经/剧本/分镜）要调真 LLM，是花钱的；
这里把 `--source` 项目的分镜产物复制过来，专注跑媒体链路（视频 → 配音 → 配乐 → 成片），
也就是 v0.20/v0.21 改动最集中的那一段。

顺带验证 v0.21 的两处修复（真实场景：一个坏文件不该让整个项目列表 500）：
- 写接口只认 `X-API-Token` 头；不带 token 的写请求必须被拒
- 坏掉的 meta.json 必须被隔离成 `.bad` 且不影响 `/api/projects`
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
#: 复制过来的文本链路产物（分镜是媒体链路的入口）
SEED_FILES = (
    "story_bible.json",
    "world_state.json",
    "script.json",
    "storyboard.json",
    "outline.json",
    "script_outline.json",
)
#: 服务端口（和 .env 里真实运行时用的 8001 分开，避免误伤）
DEFAULT_PORT = 8099
DEFAULT_TOKEN = "e2e-token-2f9c"


# ── 基础设施 ────────────────────────────────────────────────


class Http:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def __call__(
        self, method: str, path: str, body: Any = None, *, token: str | None = None
    ) -> tuple[int, Any]:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        supplied = self.token if token is None else token
        if supplied:
            headers["X-API-Token"] = supplied
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
                return response.status, _maybe_json(raw)
        except urllib.error.HTTPError as exc:
            return exc.code, _maybe_json(exc.read())
        except urllib.error.URLError as exc:
            return 0, {"error": str(exc)}


def _maybe_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw_bytes": len(raw)}


class Report:
    """收集每一步的结论，最后一次性打印/落盘。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.data: dict = {}

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(step["ok"] for step in self.steps) and bool(self.steps)


def probe(path: Path) -> dict:
    """ffprobe：时长/分辨率/音视频流（没装 ffprobe 就返回空）。"""
    binary = shutil.which("ffprobe")
    if not binary or not path.is_file():
        return {}
    out = subprocess.run(
        [
            binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return {"error": out.stderr.strip()[:200]}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}


def summarize_probe(info: dict) -> dict:
    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return {
        "duration_sec": round(float(fmt.get("duration") or 0), 2),
        "size_bytes": int(fmt.get("size") or 0),
        "video": f"{video.get('codec_name', '?')} {video.get('width')}x{video.get('height')}"
        if video
        else "",
        "audio": f"{audio.get('codec_name', '?')} {audio.get('sample_rate')}Hz "
        f"{audio.get('channels')}ch"
        if audio
        else "",
    }


# ── 播种：把文本链路产物复制成一个「分镜已确认」的新项目 ──────


def seed(source: Path, projects_dir: Path, project_id: str, *, force: bool = False) -> Path:
    target = projects_dir / project_id
    if target.exists():
        if not force:
            raise SystemExit(f"{target} 已存在（加 --force 覆盖）")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    meta = json.loads((source / "meta.json").read_text(encoding="utf-8"))
    for name in SEED_FILES:
        src = source / name
        if src.is_file():
            shutil.copy2(src, target / name)
    refs = source / "refs"
    if refs.is_dir():
        shutil.copytree(refs, target / "refs")
    meta.update(
        {
            "id": project_id,
            "title": "E2E 冒烟（mock 视频 + mock 配音）",
            "status": "storyboard_confirmed",  # 媒体链路的入口状态
            "music_engine": "generate",  # 本机作曲，$0
            "music_note": "端到端冒烟：压抑一点，别太吵",
            "music_duck": True,
        }
    )
    (target / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def server_env(projects_dir: Path, token: str, extra: dict[str, str]) -> dict[str, str]:
    """独立的 mock 环境：任何一项漏掉都可能变成真花钱的调用。"""
    env = dict(os.environ)
    env.update(
        {
            "PROJECTS_DIR": str(projects_dir),
            "API_TOKEN": token,
            "VIDEO_PROVIDER": "mock",
            "TTS_PROVIDER": "mock",
            # 人声来源默认已改成 native（视频原声、不调 TTS）：冒烟要测的是 TTS 配音链路
            "AUDIO_DUB_MODE": "tts",
            "LIPSYNC_ENGINE": "none",
            "AUDIO_LLM_SCRIPT": "off",
            "AUDIO_LLM_VISION": "false",
            "AUDIO_LLM_MOTION": "false",
            "BGM_ENGINE": "generate",
            "COMPOSE_SUBTITLE_MODE": "burn",
            "BUDGET_USD": "1",
        }
    )
    env.update(extra)
    return env


def start_server(
    projects_dir: Path, token: str, port: int, log_path: Path, extra: dict[str, str]
) -> subprocess.Popen:
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            str(REPO / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "videogen.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(REPO),
        env=server_env(projects_dir, token, extra),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc


def wait_health(http: Http, proc: subprocess.Popen, attempts: int = 120) -> bool:
    for _ in range(attempts):
        if proc.poll() is not None:
            return False
        status, _ = http("GET", "/api/health", token="")
        if status == 200:
            return True
        time.sleep(0.5)
    return False


def wait_job(http: Http, kind: str, project_id: str, report: Report, *, timeout_sec: float) -> dict:
    """轮询任务到终态；顺带把进度打到 stdout，长任务不至于「看起来卡死」。"""
    deadline = time.monotonic() + timeout_sec
    last = ""
    while time.monotonic() < deadline:
        status, payload = http("GET", f"/api/projects/{project_id}/{kind}/status")
        if status != 200:
            report.step(f"{kind} status 可读", False, f"HTTP {status}")
            return {}
        job = (payload or {}).get("job") or {}
        note = (
            f"{job.get('status')} {job.get('progress')}/{job.get('total')} {job.get('message', '')}"
        )
        if note != last:
            print(f"    · {kind}: {note}")
            last = note
        if job and not job.get("is_active"):
            if job.get("status") != "succeeded":
                report.step(
                    f"{kind} 任务成功",
                    False,
                    f"status={job.get('status')} {job.get('error', '')[:200]}",
                )
            return payload or {}
        time.sleep(1.5)
    report.step(f"{kind} 任务成功", False, f"超时 {timeout_sec:.0f}s（最后：{last}）")
    return {}


# ── 主流程 ──────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端冒烟（$0，mock 视频/配音）")
    parser.add_argument(
        "--source", required=True, help="复制分镜产物的已有项目 id（projects/ 下的目录名）"
    )
    parser.add_argument("--projects-dir", default="", help="默认 <repo>/.e2e/projects")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--keep", action="store_true", help="跑完不关服务")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的冒烟项目")
    parser.add_argument("--video-timeout", type=float, default=900.0)
    parser.add_argument("--audio-timeout", type=float, default=600.0)
    parser.add_argument("--compose-timeout", type=float, default=900.0)
    args = parser.parse_args()

    source = REPO / "projects" / args.source
    if not (source / "storyboard.json").is_file():
        raise SystemExit(f"源项目没有 storyboard.json：{source}")

    workdir = REPO / ".e2e"
    projects_dir = Path(args.projects_dir) if args.projects_dir else workdir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    project_id = "e2e000000001"
    log_path = workdir / "server.log"
    report = Report()

    print(f"→ 播种：{source.name} → {projects_dir / project_id}")
    seed(source, projects_dir, project_id, force=args.force)

    # 坏 meta.json 的项目：验证 v0.21 的隔离修复（真实现场：一个坏文件让 /api/projects 500）
    broken_id = "e2e0000000bad"
    broken = projects_dir / broken_id
    broken.mkdir(exist_ok=True)
    (broken / "meta.json").write_text('{"status": "NOT_A_STATUS"}', encoding="utf-8")

    print(f"→ 起服务：127.0.0.1:{args.port}（mock 视频/配音，独立 PROJECTS_DIR，日志 {log_path}）")
    proc = start_server(projects_dir, args.token, args.port, log_path, {})
    base = f"http://127.0.0.1:{args.port}"
    http = Http(base, args.token)
    try:
        if not wait_health(http, proc):
            report.step("服务启动", False, f"健康检查失败，看 {log_path}")
            return finish(report, workdir, project_id, base, proc, args.keep)
        report.step("服务启动（/api/health）", True, base)

        # 1) token 策略：写接口不带 token 必须被拒
        status, _ = http("POST", f"/api/projects/{project_id}/video/generate", token="")
        report.step(
            "写接口不带 X-API-Token 被拒",
            401 <= status < 500,
            f"HTTP {status}",
        )

        # 2) 坏 meta.json 不影响项目列表
        status, payload = http("GET", "/api/projects")
        ids = [p.get("id") for p in (payload or [])] if isinstance(payload, list) else []
        quarantined = (broken / "meta.json.bad").is_file()
        report.step(
            "/api/projects 不被坏 meta.json 拖垮",
            status == 200 and broken_id not in ids and quarantined,
            f"HTTP {status}，项目数={len(ids)}，已隔离={'是' if quarantined else '否'}",
        )
        report.data["quarantine"] = {
            "status": status,
            "projects": len(ids),
            "quarantined": quarantined,
        }

        # 3) 视频片段
        print("→ 生成视频片段（mock provider，真出 H.264）")
        status, payload = http("POST", f"/api/projects/{project_id}/video/generate")
        if not report.step("提交视频任务", status == 202, f"HTTP {status}"):
            return finish(report, workdir, project_id, base, proc, args.keep)
        video_status = wait_job(http, "video", project_id, report, timeout_sec=args.video_timeout)
        manifest = (video_status or {}).get("manifest") or {}
        shots = manifest.get("shots") or manifest.get("clips") or {}
        entries = list(shots.values()) if isinstance(shots, dict) else list(shots)
        clip_dir = projects_dir / project_id / "clips"
        files = sorted(clip_dir.glob("*.mp4")) if clip_dir.is_dir() else []
        total_bytes = sum(f.stat().st_size for f in files)
        report.step(
            f"片段落盘（{len(files)} 个 mp4，{total_bytes / 1e6:.1f} MB）",
            len(files) >= 1 and total_bytes > 0,
        )
        sample = summarize_probe(probe(files[0])) if files else {}
        report.data["clips"] = {
            "count": len(files),
            "manifest_entries": len(entries),
            "bytes": total_bytes,
            "sample": sample,
            "files": [f.name for f in files],
        }
        print(f"    · 抽样 {files[0].name if files else '-'}：{sample}")

        # 4) 配音
        print("→ 生成配音（mock TTS，真出可播放音频）")
        status, payload = http("POST", f"/api/projects/{project_id}/audio/generate", {})
        if not report.step("提交配音任务", status == 202, f"HTTP {status}"):
            return finish(report, workdir, project_id, base, proc, args.keep)
        audio_status = wait_job(http, "audio", project_id, report, timeout_sec=args.audio_timeout)
        audio_dir = projects_dir / project_id / "audio"
        audio_files = sorted(
            p for p in audio_dir.glob("*") if p.suffix in {".mp3", ".wav"} and p.is_file()
        )
        report.step(
            f"配音落盘（{len(audio_files)} 条）",
            len(audio_files) >= 1,
            f"{sum(p.stat().st_size for p in audio_files) / 1e6:.2f} MB",
        )
        report.data["audio"] = {
            "count": len(audio_files),
            "summary": (audio_status or {}).get("summary"),
            "sample": summarize_probe(probe(audio_files[0])) if audio_files else {},
        }

        # 5) 配乐（本机作曲，$0）
        print("→ 生成配乐（本机作曲，$0）")
        status, payload = http("POST", f"/api/projects/{project_id}/music", {"engine": "generate"})
        music_ok = status == 200 and bool((payload or {}).get("audio_url"))
        report.step(
            "配乐生成",
            music_ok,
            f"HTTP {status} engine={(payload or {}).get('engine', '-')} "
            f"mood={(payload or {}).get('mood', '-')}",
        )
        report.data["music"] = {
            key: (payload or {}).get(key)
            for key in ("engine", "mood", "key", "bpm", "duration_sec", "sections", "audio_url")
        }
        # 只读产物允许 ?token=；这里顺便验证这条路径
        status, _ = http(
            "GET", f"/api/projects/{project_id}/music/audio?token={args.token}", token=""
        )
        report.step("配乐试听（?token= 只读放行）", status == 200, f"HTTP {status}")

        # 6) 成片
        print("→ 合成成片（ffmpeg：分段 → 混音/闪避 → 字幕 → 响度归一）")
        status, payload = http(
            "POST",
            f"/api/projects/{project_id}/compose",
            {
                "bgm_enabled": True,
                "bgm_engine": "generate",
                "bgm_duck": True,
                "music_note": "端到端冒烟：压抑，别太吵",
                "compose_keep_segments": True,
            },
        )
        if not report.step("提交合成任务", status == 202, f"HTTP {status}"):
            return finish(report, workdir, project_id, base, proc, args.keep)
        wait_job(http, "compose", project_id, report, timeout_sec=args.compose_timeout)

        final_path = projects_dir / project_id / "output" / "final.mp4"
        final_info = summarize_probe(probe(final_path)) if final_path.is_file() else {}
        ok = (
            final_path.is_file()
            and final_info.get("video", "") != ""
            and final_info.get("audio", "") != ""
        )
        report.step(
            "成片 final.mp4（含视频流 + 音频流）",
            ok,
            f"{final_info.get('duration_sec', 0)}s {final_info.get('size_bytes', 0) / 1e6:.1f} MB "
            f"{final_info.get('video')} + {final_info.get('audio')}",
        )
        # 下载路径（?token= 只读放行）
        status, _ = http("GET", f"/api/projects/{project_id}/final?token={args.token}", token="")
        report.step("成片下载（?token= 只读放行）", status == 200, f"HTTP {status}")

        loudness = measure_loudness(final_path) if final_path.is_file() else {}
        report.data["final"] = {**final_info, "loudness": loudness}

        seg_dir = projects_dir / project_id / "output" / "segments"
        report.data["segments_kept"] = len(list(seg_dir.glob("*.mp4"))) if seg_dir.is_dir() else 0
        return finish(report, workdir, project_id, base, proc, args.keep)
    finally:
        if proc.poll() is None and not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if args.keep and proc.poll() is None:
            print(f"→ 服务保留在 {base}（token={args.token}）")


def measure_loudness(path: Path) -> dict:
    """用项目自己的响度工具量成片（本机 ffmpeg，不联网）。"""
    sys.path.insert(0, str(REPO / "src"))
    try:
        import asyncio

        from videogen.utils.loudness import measure

        result = asyncio.run(measure(path))
        return {
            "integrated_lufs": result.integrated,
            "true_peak_db": result.true_peak,
            "lra": result.lra,
            "error": result.error,
        }
    except Exception as exc:  # noqa: BLE001 - 量不出来不影响冒烟结论
        return {"error": f"{type(exc).__name__}: {exc}"}


def finish(
    report: Report,
    workdir: Path,
    project_id: str,
    base: str,
    proc: subprocess.Popen,
    keep: bool,
) -> int:
    payload = {
        "ok": report.ok,
        "project_id": project_id,
        "server": base,
        "server_alive": proc.poll() is None,
        "steps": report.steps,
        **report.data,
    }
    out = workdir / "report.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [s for s in report.steps if not s["ok"]]
    print()
    print(
        f"{'✅ 全部通过' if report.ok else '❌ 有失败项'}：{len(report.steps)} 项检查，"
        f"{len(failed)} 项失败"
    )
    for step in failed:
        print(f"   - {step['name']}: {step['detail']}")
    print(f"报告：{out}")
    if not keep and proc.poll() is None:
        proc.terminate()
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
