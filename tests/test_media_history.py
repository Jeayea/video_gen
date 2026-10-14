"""作废的片段 / 成片先留进 `history/`，不再直接删（重做失败时还找得回来）。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import wait_for_job
from videogen.models.video import ClipStatus, VideoClip, VideoManifest
from videogen.services import video_service
from videogen.utils import media_history


def _write(path, data: bytes = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_archive_moves_files_and_records_meta(tmp_path, settings):
    clip = _write(tmp_path / "clips" / "s1.mp4", b"clip")
    seg = _write(tmp_path / "clips" / "segments" / "seg_1.mp4", b"segment")
    outside = _write(tmp_path.parent / "elsewhere.mp4")

    entry = media_history.archive(
        tmp_path,
        ["clips/s1.mp4", seg, "clips/s1.mp4", outside, "missing.mp4"],
        reason="重做 seg_1",
        extra={"clips": [{"shot_id": "s1", "cost_usd": 1.5}]},
        settings=settings,
    )

    assert entry is not None and entry.parent.name == media_history.HISTORY_DIR
    assert not clip.exists() and not seg.exists()  # 挪走了，不是复制
    assert (entry / "clips/s1.mp4").read_bytes() == b"clip"
    assert (entry / "clips/segments/seg_1.mp4").read_bytes() == b"segment"
    assert outside.exists()  # 项目目录外的文件不碰
    meta = json.loads((entry / "history.json").read_text(encoding="utf-8"))
    assert meta["reason"] == "重做 seg_1"
    assert [f["path"] for f in meta["files"]] == ["clips/s1.mp4", "clips/segments/seg_1.mp4"]

    [listed] = media_history.list_entries(tmp_path)
    assert listed["id"] == entry.name and listed["bytes"] == len(b"clip") + len(b"segment")
    assert listed["clips"][0]["cost_usd"] == 1.5
    assert media_history.resolve_file(tmp_path, entry.name, "clips/s1.mp4")
    assert media_history.resolve_file(tmp_path, entry.name, "../../clips/s1.mp4") is None
    assert media_history.resolve_file(tmp_path, entry.name, "history.json") is None
    assert media_history.resolve_file(tmp_path, "../x", "clips/s1.mp4") is None

    # 没有文件可留：什么都不做
    assert media_history.archive(tmp_path, ["missing.mp4"], reason="x", settings=settings) is None


def test_prune_by_count_and_size_keeps_newest(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(settings, "media_history_keep", 2)
    monkeypatch.setattr(settings, "media_history_max_mb", 0)
    for i in range(4):
        _write(tmp_path / "clips" / f"s{i}.mp4")
        media_history.archive(tmp_path, [f"clips/s{i}.mp4"], reason=f"r{i}", settings=settings)
    assert [e["reason"] for e in media_history.list_entries(tmp_path)] == ["r3", "r2"]

    # 按大小：超了从最旧的删，但最新一批永远留着
    monkeypatch.setattr(settings, "media_history_keep", 10)
    monkeypatch.setattr(settings, "media_history_max_mb", 1)
    _write(tmp_path / "clips" / "big.mp4", b"0" * (2 * 1024 * 1024))
    media_history.archive(tmp_path, ["clips/big.mp4"], reason="big", settings=settings)
    assert [e["reason"] for e in media_history.list_entries(tmp_path)] == ["big"]


def test_keep_zero_means_plain_delete(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(settings, "media_history_keep", 0)
    clip = _write(tmp_path / "clips" / "s1.mp4")
    assert media_history.archive(tmp_path, [clip], reason="x", settings=settings) is None
    assert not clip.exists()
    assert not (tmp_path / media_history.HISTORY_DIR).exists()


def test_invalidate_clips_archives_segment_once(tmp_path):
    for name in ("clips/a.mp4", "clips/b.mp4", "clips/segments/seg.mp4", "frames/a.jpg"):
        _write(tmp_path / name)
    manifest = VideoManifest(
        clips=[
            VideoClip(shot_id=sid, status=ClipStatus.READY, path=f"clips/{sid}.mp4",
                      segment_id="seg", segment_path="clips/segments/seg.mp4",
                      cost_usd=cost, prompt=f"prompt {sid}")
            for sid, cost in (("a", 1.2), ("b", 0.0))
        ]
    )
    video_service.invalidate_clips(manifest, ["a", "b"], tmp_path, reason="重做 seg")

    [entry] = media_history.list_entries(tmp_path)
    assert entry["reason"] == "重做 seg"
    assert sorted(f["path"] for f in entry["files"]) == [
        "clips/a.mp4", "clips/b.mp4", "clips/segments/seg.mp4"
    ]
    assert [c["prompt"] for c in entry["clips"]] == ["prompt a", "prompt b"]
    assert not (tmp_path / "frames/a.jpg").exists()  # 抽帧是缓存，照删
    assert all(c.status == ClipStatus.PENDING and not c.path for c in manifest.clips)


def test_purge_media_archives_deliverables_but_not_caches(store):
    from videogen.models.project import Project

    project = Project(title="留存")
    store.save_project(project)
    root = store.ensure_project_dir(project.id)
    _write(root / "clips" / "s1.mp4")
    _write(root / "output" / "final.mp4", b"final")
    _write(root / "output" / "segments" / "s1.mp4")  # 合成缓存
    store.save_video_manifest(project.id, VideoManifest())

    store.purge_media(project.id, video=True, compose=True, reason="分镜修改")
    [entry] = media_history.list_entries(root)
    paths = sorted(f["path"] for f in entry["files"])
    assert paths == ["clips/s1.mp4", "output/final.mp4", "video_manifest.json"]
    assert not (root / "clips").exists() and not (root / "output").exists()

    # 坏产物（合成失败的半截成片）不留
    _write(root / "output" / "final.mp4", b"half")
    store.purge_media(project.id, compose=True, keep_history=False)
    assert len(media_history.list_entries(root)) == 1


@pytest.mark.asyncio
async def test_redo_segment_keeps_old_clip_in_history(store, video_pipeline, video_ready_project):
    pid = video_ready_project.id
    root = store.ensure_project_dir(pid)
    first = store.load_video_manifest(pid).clips[0]
    old_bytes = (root / first.path).read_bytes()

    result = await video_pipeline.invalidate_video(pid, shots=[first.shot_id])
    entries = media_history.list_entries(root)
    assert entries and entries[0]["reason"].startswith("重做 ")
    assert result["segments"][0]["segment_id"] in entries[0]["reason"]
    kept = media_history.resolve_file(root, entries[0]["id"], first.path)
    assert kept is not None and kept.read_bytes() == old_bytes

    # 重新生成后旧的仍在历史里
    job = await video_pipeline.start_video_generation(pid)
    await wait_for_job(video_pipeline, job.id)
    assert media_history.resolve_file(root, entries[0]["id"], first.path) is not None
