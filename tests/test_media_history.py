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


# ── 恢复历史片段 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_brings_old_clip_back_without_billing(
    store, video_pipeline, video_ready_project
):
    from videogen.models.project import ProjectStatus
    from videogen.services import cost_service

    pid = video_ready_project.id
    root = store.ensure_project_dir(pid)
    manifest = store.load_video_manifest(pid)
    first = manifest.clips[0]
    (root / first.path).write_bytes(b"old take")
    ledger_before = cost_service.load_report(store, pid).total_usd

    await video_pipeline.invalidate_video(pid, shots=[first.shot_id])
    [redo] = media_history.list_entries(root)
    job = await video_pipeline.start_video_generation(pid)
    await wait_for_job(video_pipeline, job.id)
    assert (root / first.path).read_bytes() != b"old take"
    ledger_after_redo = cost_service.load_report(store, pid).total_usd

    result = await video_pipeline.restore_video_history(pid, redo["id"])
    assert result["restored_shots"] == [first.shot_id]
    assert (root / first.path).read_bytes() == b"old take"
    clip = store.load_video_manifest(pid).get(first.shot_id)
    assert clip.status == ClipStatus.READY and clip.cost_recorded
    assert clip.prompt == first.prompt
    # 不调视频模型、不记账；历史里那份还在（可以再恢复），被换下的新片段进了新的一批
    assert cost_service.load_report(store, pid).total_usd == ledger_after_redo >= ledger_before
    assert media_history.resolve_file(root, redo["id"], first.path) is not None
    newest = media_history.list_entries(root)[0]
    assert newest["reason"].startswith("恢复 ") and first.path in [
        f["path"] for f in newest["files"]
    ]
    assert store.load_project(pid).status == ProjectStatus.VIDEO_READY


def _seg_manifest(shot_ids, seg="seg_1"):
    return VideoManifest(
        clips=[
            VideoClip(shot_id=sid, status=ClipStatus.READY, path=f"clips/{sid}.mp4",
                      segment_id=seg, segment_path=f"clips/segments/{seg}.mp4",
                      prompt=f"prompt {sid}")
            for sid in shot_ids
        ]
    )


def _seg_storyboard(shot_ids, seg="seg_1"):
    from videogen.models.storyboard import Shot, Storyboard

    return Storyboard(shots=[
        Shot(id=sid, scene_id="scene_1", shot_no=i + 1, duration_sec=3, segment_id=seg)
        for i, sid in enumerate(shot_ids)
    ])


def test_restore_is_per_segment_and_skips_regrouped_segments(tmp_path):
    for name in ("clips/a.mp4", "clips/b.mp4", "clips/segments/seg_1.mp4"):
        _write(tmp_path / name, b"old")
    old = _seg_manifest(["a", "b"])
    video_service.invalidate_clips(old, ["a", "b"], tmp_path, reason="重做 seg_1")
    [entry] = media_history.list_entries(tmp_path)
    folder, meta = media_history.load_entry(tmp_path, entry["id"])

    # 现在这一段多了一个镜头：整段文件对不上，不恢复
    manifest = VideoManifest()
    result = video_service.restore_clips(
        manifest, _seg_storyboard(["a", "b", "c"]), folder, meta, tmp_path
    )
    assert result["restored_shots"] == []
    assert result["skipped"][0]["segment_id"] == "seg_1"
    assert not (tmp_path / "clips/a.mp4").exists()

    # 分段没变：整段（含整段文件）恢复
    result = video_service.restore_clips(
        manifest, _seg_storyboard(["a", "b"]), folder, meta, tmp_path, shot_ids=["b"]
    )
    assert result["restored_shots"] == ["a", "b"]
    assert (tmp_path / "clips/segments/seg_1.mp4").read_bytes() == b"old"
    assert manifest.get("a").segment_path == "clips/segments/seg_1.mp4"
    assert not list(tmp_path.rglob("*.restoring"))


def test_restore_reads_legacy_history_without_paths(tmp_path):
    """老版本的 history.json 只记了 shot_id / prompt 等，没记路径：按命名规则找文件。"""
    entry = tmp_path / media_history.HISTORY_DIR / "20261014-120000-000-重做-a"
    _write(entry / "clips/a.mp4", b"legacy")
    (entry / "history.json").write_text(json.dumps({
        "reason": "重做 a",
        "clips": [{"shot_id": "a", "prompt": "p", "cost_usd": 0.65, "duration_sec": 5}],
    }), encoding="utf-8")
    folder, meta = media_history.load_entry(tmp_path, entry.name)

    from videogen.models.storyboard import Shot, Storyboard

    manifest = VideoManifest()
    storyboard = Storyboard(shots=[Shot(id="a", scene_id="scene_1", shot_no=1, duration_sec=5)])
    result = video_service.restore_clips(manifest, storyboard, folder, meta, tmp_path)
    assert result["restored_shots"] == ["a"]
    assert (tmp_path / "clips/a.mp4").read_bytes() == b"legacy"
    assert manifest.get("a").cost_usd == 0.65 and manifest.get("a").cost_recorded


@pytest.mark.asyncio
async def test_restore_rejects_bad_entry_and_nothing_to_restore(
    store, video_pipeline, video_ready_project
):
    from videogen.orchestrator.pipeline import PipelineError, PipelineNotFound

    pid = video_ready_project.id
    with pytest.raises(PipelineNotFound):
        await video_pipeline.restore_video_history(pid, "../etc")
    root = store.ensure_project_dir(pid)
    entry = root / media_history.HISTORY_DIR / "20261014-120000-000-空"
    _write(entry / "output" / "final.mp4")
    with pytest.raises(PipelineError, match="没有能恢复的段"):
        await video_pipeline.restore_video_history(pid, entry.name)
