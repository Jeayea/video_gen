#!/usr/bin/env python
"""方案 B：真花钱的小口径端到端 —— 只重生成 N 个镜头，把整条媒体链路跑到底。

    # 1) 先看账（只播种 + 调 preflight，一分钱不花）
    .venv/bin/python tools/e2e/real_shot_e2e.py \\
        --shots scene_1_shot_1,scene_2_shot_2,scene_3_shot_2

    # 2) 确认过再真跑（花真钱，必须显式 --yes）
    .venv/bin/python tools/e2e/real_shot_e2e.py \\
        --shots scene_1_shot_1,scene_2_shot_2,scene_3_shot_2 --yes

为什么不是整片重跑：整片 15 镜 79 秒 ≈ $10.3，而这里要证明的只是
「真 provider 的片段 + 真 TTS + 真成片」这条链是通的。所以把源项目**整个复制**成
新项目（源项目一个字节都不动），只删掉指定的 N 个镜头的片段与配音，
让流水线只补这几条，其余镜头复用**已经付过钱**的片段。

跑完会顺手量三件事（都是本机 ffmpeg，$0）：
- 成片响度（对比 `COMPOSE_TARGET_LUFS`）
- 逐条分段响度极差（v0.20 的逐条归一有没有在新录的配音上生效）
- 人声闪避深度（复用 v0.19 的 `tools/music/*`，residual 口径逐样本相减）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
#: 复制过来的文件（文本链路 + 两份清单 + 账本）
COPY_FILES = (
    "story_bible.json",
    "world_state.json",
    "script.json",
    "storyboard.json",
    "outline.json",
    "consistency_report.json",
    "video_manifest.json",
    "audio_manifest.json",
    "meta.json",
    "cost_report.json",
)
#: 复制过来的目录（片段/配音/参考图 —— 这些已经付过钱了，复用）
COPY_DIRS = ("refs", "clips", "audio")


# ── HTTP ────────────────────────────────────────────────────


def call(base: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return response.status, {"raw_bytes": len(raw)}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return exc.code, {"raw_bytes": len(raw)}
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc)}


def wait_job(base: str, kind: str, project_id: str, *, timeout_sec: float) -> dict:
    deadline = time.monotonic() + timeout_sec
    last = ""
    while time.monotonic() < deadline:
        status, payload = call(base, "GET", f"/api/projects/{project_id}/{kind}/status")
        if status != 200:
            raise SystemExit(f"{kind}/status 读不到：HTTP {status} {payload}")
        job = (payload or {}).get("job") or {}
        note = (
            f"{job.get('status')} {job.get('progress')}/{job.get('total')} {job.get('message', '')}"
        )
        if note != last:
            print(f"    · {kind}: {note}", flush=True)
            last = note
        if job and not job.get("is_active"):
            if job.get("status") != "succeeded":
                raise SystemExit(
                    f"{kind} 任务失败：{job.get('status')} {job.get('error', '')[:300]}"
                )
            return payload or {}
        time.sleep(2.0)
    raise SystemExit(f"{kind} 超时（{timeout_sec:.0f}s，最后状态：{last}）")


# ── 播种（复制 + 剪掉 N 个镜头）──────────────────────────────


def seed(source: Path, target: Path, shots: list[str], *, force: bool) -> dict:
    if target.exists():
        if not force:
            raise SystemExit(f"{target} 已存在（加 --force 覆盖）")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in COPY_FILES:
        src = source / name
        if src.is_file():
            shutil.copy2(src, target / name)
    for name in COPY_DIRS:
        src = source / name
        if src.is_dir():
            shutil.copytree(src, target / name)

    dropped = {"video": [], "audio": [], "files": []}
    video_path = target / "video_manifest.json"
    audio_path = target / "audio_manifest.json"
    if video_path.is_file():
        video = json.loads(video_path.read_text(encoding="utf-8"))
        keep = []
        for clip in video.get("clips", []):
            if clip.get("shot_id") in shots:
                dropped["video"].append(clip["shot_id"])
            else:
                keep.append(clip)
        video["clips"] = keep
        video_path.write_text(json.dumps(video, ensure_ascii=False, indent=2), encoding="utf-8")
    if audio_path.is_file():
        audio = json.loads(audio_path.read_text(encoding="utf-8"))
        keep = []
        for clip in audio.get("clips", []):
            if clip.get("shot_id") in shots:
                dropped["audio"].append(clip["shot_id"])
            else:
                keep.append(clip)
        audio["clips"] = keep
        # 曲目路径是**绝对路径**且指回源项目 —— 清掉，让本次重新生成
        audio["bgm"] = None
        audio_path.write_text(json.dumps(audio, ensure_ascii=False, indent=2), encoding="utf-8")
    for shot in shots:
        for folder, suffix in (("clips", ".mp4"), ("audio", ".wav"), ("audio", ".mp3")):
            path = target / folder / f"{shot}{suffix}"
            if path.is_file():
                path.unlink()
                dropped["files"].append(str(path.relative_to(target)))

    meta_path = target / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["id"] = target.name
    meta["title"] = f"方案 B · 真跑 {len(shots)} 镜（mock 之外的第一次真链路）"
    meta["status"] = "video_ready"  # 12 镜已就绪 + N 镜待生成，与清单一致
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return dropped


# ── 测量（全是本机 ffmpeg）──────────────────────────────────


def probe(path: Path) -> dict:
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
    return json.loads(out.stdout) if out.returncode == 0 else {}


def summarize(info: dict) -> dict:
    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return {
        "duration_sec": round(float(fmt.get("duration") or 0), 2),
        "size_bytes": int(fmt.get("size") or 0),
        "video": f"{video.get('codec_name')} {video.get('width')}x{video.get('height')}"
        if video
        else "",
        "audio": f"{audio.get('codec_name')} {audio.get('sample_rate')}Hz {audio.get('channels')}ch"
        if audio
        else "",
    }


def segment_loudness(project_dir: Path, order: list[str]) -> dict:
    """逐条分段量集成响度（v0.20 的验收口径：极差要小）。"""
    sys.path.insert(0, str(REPO / "src"))
    import asyncio

    from videogen.utils.loudness import measure

    seg_dir = project_dir / "output" / "segments"
    values: dict[str, float | None] = {}
    for shot in order:
        path = seg_dir / f"{shot}.mp4"
        if not path.is_file():
            continue
        result = asyncio.run(measure(path))
        values[shot] = result.integrated
    numbers = [v for v in values.values() if v is not None]
    spread = round(max(numbers) - min(numbers), 2) if len(numbers) >= 2 else None
    return {"lufs": values, "spread_db": spread, "count": len(numbers)}


def measure_ducking(project_dir: Path, order: list[str], duration: float, out_dir: Path) -> dict:
    """复用 v0.19 的工具链：拼人声轨 → 真实滤镜链渲两版 → residual 逐样本相减。"""
    seg_dir = project_dir / "output" / "segments"
    bgm = project_dir / "output" / "music" / "bgm.wav"
    parts = [seg_dir / f"{shot}.mp4" for shot in order if (seg_dir / f"{shot}.mp4").is_file()]
    if not bgm.is_file() or not parts:
        return {"error": "缺分段或配乐，跳过闪避测量"}
    out_dir.mkdir(parents=True, exist_ok=True)
    listing = out_dir / "voice_parts.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    voice = out_dir / "voice.wav"
    ffmpeg = _ffmpeg_binary()
    concat = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-vn",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(voice),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if concat.returncode != 0 or not voice.is_file():
        return {"error": f"拼人声轨失败：{concat.stderr.strip()[:200]}"}

    harness = REPO / "tools/music/ducking_harness.py"
    step = subprocess.run(
        [
            str(REPO / ".venv/bin/python"),
            str(harness),
            "--voice",
            str(voice),
            "--music",
            str(bgm),
            "--out",
            str(out_dir),
            "--duration",
            f"{duration:.3f}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if step.returncode != 0:
        return {"error": f"滤镜链渲染失败：{step.stderr.strip()[-300:]}"}
    report = out_dir / "ducking_report.json"
    measure = subprocess.run(
        [
            str(REPO / ".venv/bin/python"),
            str(REPO / "tools/music/measure_ducking.py"),
            "--mode",
            "residual",
            "--voice",
            str(voice),
            "--variant",
            f"固定音量={out_dir / 'mix_off.wav'}",
            "--variant",
            f"人声闪避={out_dir / 'mix_on.wav'}",
            "--reference",
            f"固定音量={out_dir / 'mix_off.wav'}",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if measure.returncode != 0 or not report.is_file():
        return {"error": f"闪避测量失败：{measure.stderr.strip()[-300:]}"}
    payload = json.loads(report.read_text(encoding="utf-8"))
    return {
        "verdict": payload.get("verdict"),
        "deltas": payload.get("deltas_vs_reference", {}).get("人声闪避"),
        "report": str(report),
        "voice": str(voice),
    }


def _ffmpeg_binary() -> str:
    """拼人声轨只需要「能解码 mp4 + 编 wav」的 ffmpeg，用项目自己的解析顺序。"""
    sys.path.insert(0, str(REPO / "src"))
    from videogen.utils.ffmpeg import resolve_ffmpeg

    binary = resolve_ffmpeg()
    if not binary:
        raise SystemExit("找不到 ffmpeg，无法拼人声轨")
    return binary


# ── 主流程 ──────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="方案 B：真跑 N 个镜头的端到端")
    parser.add_argument("--source", default="20260911-一个合照被裁掉的女孩从默默无名")
    parser.add_argument(
        "--id",
        default="20260911-方案-b-真跑-3-镜-mock-之外的第一次真链路",
        help="新项目 id（必须是没用过的）",
    )
    parser.add_argument("--shots", default="", help="逗号分隔的 shot_id（要真花钱的）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--yes", action="store_true", help="确认花钱，缺省只播种 + 看账")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的新项目")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="跳过播种与花钱的步骤，只对已有项目重量一遍并重写报告（$0）",
    )
    parser.add_argument("--video-timeout", type=float, default=2400.0)
    parser.add_argument("--audio-timeout", type=float, default=900.0)
    parser.add_argument("--compose-timeout", type=float, default=2400.0)
    args = parser.parse_args()

    shots = [s.strip() for s in args.shots.split(",") if s.strip()]
    source = REPO / "projects" / args.source
    target = REPO / "projects" / args.id
    if not (source / "storyboard.json").is_file():
        raise SystemExit(f"源项目不完整：{source}")
    storyboard = json.loads((source / "storyboard.json").read_text(encoding="utf-8"))
    known = [s["id"] for s in storyboard.get("shots", [])]
    unknown = [s for s in shots if s not in known]
    if unknown:
        raise SystemExit(f"这些 shot_id 不在分镜里：{unknown}")
    order = [s for s in known]
    if not args.verify_only and not shots:
        raise SystemExit("--shots 不能为空（或改用 --verify-only）")

    out_dir = REPO / ".e2e" / "real"
    out_dir.mkdir(parents=True, exist_ok=True)
    previous_cost: dict = {}
    existing_report = out_dir / "report.json"
    if existing_report.is_file():
        previous_cost = (json.loads(existing_report.read_text(encoding="utf-8")) or {}).get(
            "cost"
        ) or {}
    dropped: dict = {"video": [], "audio": [], "files": []}
    spent_before: float | None = None
    before_updated = ""

    if args.verify_only:
        print(f"→ --verify-only：不播种、不调花钱接口，只对 {args.id} 重量一遍（$0）")
        if not target.is_dir():
            raise SystemExit(f"项目不存在：{target}")
        return verify_and_report(args, target, order, dropped, None, "", previous_cost)

    print(f"→ 播种：{source.name} → {target.name}（剪掉 {len(shots)} 镜：{', '.join(shots)}）")
    dropped = seed(source, target, shots, force=args.force)
    print(
        f"  剪掉视频清单 {len(dropped['video'])} 条、配音清单 {len(dropped['audio'])} 条、"
        f"文件 {len(dropped['files'])} 个"
    )

    status, _ = call(args.base_url, "GET", "/api/health")
    if status != 200:
        raise SystemExit(f"服务不可用（{args.base_url}/api/health → HTTP {status}）")
    status, pre = call(args.base_url, "GET", f"/api/projects/{args.id}/preflight?action=all")
    if status != 200:
        raise SystemExit(f"preflight 失败：HTTP {status} {pre}")
    video_part = (pre or {}).get("video") or {}
    audio_part = (pre or {}).get("audio") or {}
    cost_report_path = target / "cost_report.json"
    before = (
        json.loads(cost_report_path.read_text(encoding="utf-8"))
        if cost_report_path.is_file()
        else {"total_usd": 0.0}
    )
    spent_before = sum(e.get("usd", 0.0) for e in before.get("entries", []))
    before_updated = before.get("updated_at") or ""

    print()
    print("── preflight（这一跑大概要花多少）──")
    print(f"  待生成镜头：{video_part.get('pending_shots', video_part.get('shots', '?'))}")
    print(
        f"  视频计费秒数：{video_part.get('billed_seconds', '?')}"
        f"  单价：${video_part.get('price_per_second', '?')}/秒"
    )
    print(
        f"  视频预估：${video_part.get('estimated_usd')}"
        f"（如果全部重跑会是多少：${video_part.get('estimated_usd_if_all')}）"
    )
    print(f"  配音预估：${audio_part.get('estimated_usd')}")
    print(f"  该项目账本已花：${spent_before:.4f} / 预算 ${before.get('budget_usd', 0):.2f}")
    print()

    if not args.yes:
        print("（未加 --yes：只播种 + 看账，没有调用任何花钱的接口）")
        return 0

    print("→ 生成视频片段（真实 provider）")
    status, payload = call(args.base_url, "POST", f"/api/projects/{args.id}/video/generate")
    if status != 202:
        raise SystemExit(f"提交视频任务失败：HTTP {status} {payload}")
    wait_job(args.base_url, "video", args.id, timeout_sec=args.video_timeout)

    print("→ 生成配音（真实 TTS，script_mode=off 避免额外 LLM 调用）")
    status, payload = call(
        args.base_url,
        "POST",
        f"/api/projects/{args.id}/audio/generate",
        {"script_mode": "off"},
    )
    if status != 202:
        raise SystemExit(f"提交配音任务失败：HTTP {status} {payload}")
    wait_job(args.base_url, "audio", args.id, timeout_sec=args.audio_timeout)

    print("→ 生成配乐（本机作曲，$0）")
    status, music = call(
        args.base_url, "POST", f"/api/projects/{args.id}/music", {"engine": "generate"}
    )
    if status != 200:
        raise SystemExit(f"配乐失败：HTTP {status} {music}")

    print("→ 合成成片（真实片段 + 真配音 + 配乐 + 闪避 + burn 字幕 + 响度归一）")
    status, payload = call(
        args.base_url,
        "POST",
        f"/api/projects/{args.id}/compose",
        {
            "bgm_enabled": True,
            "bgm_engine": "generate",
            "bgm_duck": True,
            "compose_keep_segments": True,
        },
    )
    if status != 202:
        raise SystemExit(f"提交合成任务失败：HTTP {status} {payload}")
    wait_job(args.base_url, "compose", args.id, timeout_sec=args.compose_timeout)

    return verify_and_report(
        args, target, order, dropped, spent_before, before_updated, previous_cost
    )


def verify_and_report(
    args: argparse.Namespace,
    target: Path,
    order: list[str],
    dropped: dict,
    spent_before: float | None,
    before_updated: str,
    previous_cost: dict,
) -> int:
    """验收 + 测量（全是本机 ffmpeg，$0）。`--verify-only` 和真跑完都会走这里。"""
    final = target / "output" / "final.mp4"
    final_info = summarize(probe(final)) if final.is_file() else {}
    # 合成报告在项目根目录（`output/` 下只放产物）
    compose: dict = {}
    for candidate in (target / "compose_report.json", target / "output" / "compose_report.json"):
        if candidate.is_file():
            compose = json.loads(candidate.read_text(encoding="utf-8"))
            break
    cost_report_path = target / "cost_report.json"
    after = json.loads(cost_report_path.read_text(encoding="utf-8"))
    spent_after = sum(e.get("usd", 0.0) for e in after.get("entries", []))
    if spent_before is None:
        cost_block = dict(previous_cost) if previous_cost else {}
        cost_block.setdefault("spent_after_usd", round(spent_after, 6))
        cost_block["spent_total_usd"] = round(spent_after, 6)
    else:
        cost_block = {
            "spent_before_usd": round(spent_before, 6),
            "spent_after_usd": round(spent_after, 6),
            "delta_usd": round(spent_after - spent_before, 6),
            "new_entries": [
                {
                    "stage": e.get("stage"),
                    "provider": e.get("provider"),
                    "model": e.get("model"),
                    "calls": e.get("calls"),
                    "usd": e.get("usd"),
                }
                for e in after.get("entries", [])
                if e.get("usd", 0) > 0 and e.get("created_at", "") > (before_updated or "")
            ],
        }

    print()
    print("── 验收 ──")
    print(f"  成片：{final_info}")
    lufs = compose.get("final_lufs")
    peak = compose.get("final_true_peak_db")
    print(f"  成片响度：{lufs} LUFS / 真峰值 {peak} dBFS（目标 {compose.get('target_lufs')}）")
    print(f"  逐条增益：{compose.get('voice_gains')}")
    print(f"  告警：{compose.get('warnings')}")
    out_dir = REPO / ".e2e" / "real"
    out_dir.mkdir(parents=True, exist_ok=True)
    print("  量逐条分段响度…")
    loud = segment_loudness(target, order)
    print(f"    极差 {loud['spread_db']} dB（{loud['count']} 条）")
    print("  量人声闪避（residual）…")
    duck = measure_ducking(target, order, final_info.get("duration_sec", 0.0), out_dir / "ducking")
    print(f"    {duck.get('verdict') or duck.get('error')}")

    payload = {
        "project_id": args.id,
        "shots_regenerated": args.shots.split(",") if args.shots else [],
        "dropped": dropped,
        "final": final_info,
        "compose": {
            key: compose.get(key)
            for key in (
                "bgm_engine",
                "bgm_duck",
                "final_lufs",
                "final_true_peak_db",
                "target_lufs",
                "voice_gains",
                "warnings",
                "video_provider",
                "tts_provider",
                "subtitle_mode_effective",
            )
        },
        "segment_loudness": {"spread_db": loud["spread_db"], "lufs": loud["lufs"]},
        "ducking": duck,
        "cost": cost_block,
    }
    out = out_dir / "report.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    if "delta_usd" in cost_block:
        print(f"本次花费：${cost_block['delta_usd']:.4f}（累计 ${spent_after:.4f}）")
    else:
        print(f"本项目账本累计：${spent_after:.4f}（--verify-only 没花钱）")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
