from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.conftest import wait_for_job
from tests.factories import small_project
from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.audio import AudioClip, AudioManifest
from videogen.models.compose import ComposeReport, ComposeSegment, SubtitleMode
from videogen.models.project import Project, ProjectStatus
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import ClipStatus, MediaKind, VideoClip, VideoManifest
from videogen.orchestrator import pipeline as pipeline_module
from videogen.orchestrator.pipeline import Pipeline, PipelineError
from videogen.providers.tts.mock import MockTTSProvider
from videogen.providers.video.mock import MockVideoProvider
from videogen.services import compose_service, subtitle_service
from videogen.utils.av_sync import check_av_sync
from videogen.utils.ffmpeg import (
    ffmpeg_available,
    ffmpeg_capabilities_for,
    ffprobe_available,
    probe_duration,
    probe_streams,
    resolve_ffmpeg,
    run_ffmpeg,
)

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="环境里没有 ffmpeg，跳过真实合成用例"
)


# ── 造一个小项目（比整条 LLM 链路快得多）────────────────


def real_pipeline(store, bgm=None, tts=None) -> Pipeline:
    """真实 ffmpeg 的媒体流水线：mock 视频 + mock 配音（都产出真文件）。"""
    return Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: MockVideoProvider(),
        tts_factory=lambda: tts or MockTTSProvider(),
        bgm_factory=lambda: bgm or FakeBgmLibrary(),
    )


async def prepare_media(store, pipeline: Pipeline, project: Project, *, audio: bool = True):
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)
    if audio:
        job = await pipeline.start_audio_generation(project.id)
        await wait_for_job(pipeline, job.id)
    return store.load_project(project.id)


# ── 字幕（纯函数，不需要 ffmpeg）──────────────────────


def fake_segments(durations: list[float], texts: list[str]) -> list[ComposeSegment]:
    return [
        ComposeSegment(
            shot_id=f"s{index}",
            index=index,
            duration_sec=duration,
            text=text,
            video_path=f"clips/s{index}.mp4",
        )
        for index, (duration, text) in enumerate(zip(durations, texts, strict=True))
    ]


def test_srt_timeline_is_cumulative():
    srt = subtitle_service.build_srt(fake_segments([2.0, 3.0], ["第一句", "第二句"]))
    assert "1\n00:00:00,000 --> 00:00:02,000\n第一句" in srt
    assert "2\n00:00:02,000 --> 00:00:05,000\n第二句" in srt


def test_srt_skips_segments_without_text():
    srt = subtitle_service.build_srt(fake_segments([1.0, 1.0], ["有台词", ""]))
    assert srt.count("-->") == 1
    assert "有台词" in srt


def test_srt_clamps_cue_to_speech_duration():
    manifest = AudioManifest(provider="fake")
    manifest.upsert(
        AudioClip(
            id="s0",
            shot_id="s0",
            text="短",
            voice="v",
            status=ClipStatus.READY,
            duration_sec=0.8,
        )
    )
    srt = subtitle_service.build_srt(fake_segments([10.0], ["短"]), manifest)
    # 字幕只覆盖说话的那几秒，不要糊满整个镜头
    assert "00:00:00,000 --> 00:00:01,150" in srt


def test_srt_prefixes_speaker_name():
    storyboard = Storyboard(
        shots=[
            Shot(
                id="s0",
                scene_id="scene_1",
                shot_no=1,
                duration_sec=3,
                dialogue_text="这个项目是我做的。",
                dialogue_speaker="char_1",
            )
        ]
    )
    manifest = AudioManifest(provider="fake")
    manifest.upsert(
        AudioClip(
            id="s0",
            shot_id="s0",
            speaker="char_1",
            speaker_name="林晚",
            text="这个项目是我做的。",
            voice="v",
            status=ClipStatus.READY,
            duration_sec=2.0,
        )
    )
    segments = subtitle_service.build_segments(storyboard, manifest)
    assert "林晚：这个项目是我做的。" in subtitle_service.build_srt(segments, manifest)


def test_build_segments_binds_audio_by_shot():
    storyboard = Storyboard(
        shots=[Shot(id="s1", scene_id="scene_1", shot_no=1, duration_sec=3, narration="旁白")]
    )
    manifest = AudioManifest(provider="fake")
    manifest.upsert(
        AudioClip(
            id="s1",
            shot_id="s1",
            text="旁白",
            voice="v",
            status=ClipStatus.READY,
            path="audio/s1.mp3",
            duration_sec=1.5,
        )
    )
    segments = subtitle_service.build_segments(storyboard, manifest)
    assert segments[0].audio_path == "audio/s1.mp3"
    assert segments[0].duration_sec == 3
    assert segments[0].text == "旁白"


# ── 前置校验（不需要 ffmpeg）──────────────────────────


def make_manifests(store, project, *, video_ok=True, audio_ok=True):
    storyboard = store.load_storyboard(project.id)
    from videogen.utils.file_store import FileStore

    store_obj: FileStore = store
    clips_dir = store_obj.clips_dir(project.id)
    video = VideoManifest(provider="fake")
    for shot in storyboard.shots:
        path = clips_dir / f"{shot.id}.mp4"
        path.write_bytes(b"fake")
        video.upsert(
            VideoClip(
                shot_id=shot.id,
                scene_id=shot.scene_id,
                status=ClipStatus.READY if video_ok else ClipStatus.FAILED,
                media_kind=MediaKind.VIDEO,
                path=f"clips/{shot.id}.mp4",
                duration_sec=shot.duration_sec,
            )
        )
    audio = AudioManifest(provider="fake")
    for shot in storyboard.shots:
        path = store_obj.audio_dir(project.id) / f"{shot.id}.mp3"
        path.write_bytes(b"fake")
        audio.upsert(
            AudioClip(
                id=shot.id,
                shot_id=shot.id,
                text=shot.dialogue_text or "",
                voice="v",
                status=ClipStatus.READY if audio_ok else ClipStatus.FAILED,
                path=f"audio/{shot.id}.mp3",
                duration_sec=1.0,
            )
        )
    return storyboard, video, audio


@pytest.mark.asyncio
async def test_compose_rejects_incomplete_video(store):
    project = small_project(store)
    storyboard, video, audio = make_manifests(store, project, video_ok=False)

    with pytest.raises(compose_service.ComposeError) as exc:
        await compose_service.compose(
            project,
            storyboard,
            video,
            audio,
            segments_dir=store.segments_dir(project.id),
            output_dir=store.output_dir(project.id),
            project_dir=store.ensure_project_dir(project.id),
        )
    assert "视频片段不完整" in str(exc.value)


@pytest.mark.asyncio
async def test_compose_rejects_failed_audio(store):
    project = small_project(store)
    storyboard, video, audio = make_manifests(store, project, audio_ok=False)

    with pytest.raises(compose_service.ComposeError) as exc:
        await compose_service.compose(
            project,
            storyboard,
            video,
            audio,
            segments_dir=store.segments_dir(project.id),
            output_dir=store.output_dir(project.id),
            project_dir=store.ensure_project_dir(project.id),
        )
    assert "配音不完整" in str(exc.value)


@pytest.mark.asyncio
async def test_compose_requires_missing_video_manifest(store):
    project = small_project(store)
    with pytest.raises(compose_service.ComposeError):
        await compose_service.compose(
            project,
            store.load_storyboard(project.id),
            None,
            None,
            segments_dir=store.segments_dir(project.id),
            output_dir=store.output_dir(project.id),
            project_dir=store.ensure_project_dir(project.id),
        )


def test_ass_uses_output_pixels_and_soft_look_by_default():
    ass = subtitle_service.build_ass(
        fake_segments([2.0, 3.0], ["第一句", "第二句"]),
        resolution=(1080, 1920),
        look=subtitle_service.subtitle_look("")[1],
    )
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    style = next(line for line in ass.splitlines() if line.startswith("Style: "))
    fields = style[len("Style: ") :].split(",")
    assert fields[1] == "Hiragino Sans GB"
    assert fields[2] == "45", "soft 字号 = 1080 * 60/1440"
    assert fields[7] == "0", "soft 不加粗"
    assert fields[21] == "230", "竖屏离底边 12%：1920 * 0.12"
    assert "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\\blur3}第一句" in ass
    assert "0:00:02.00,0:00:05.00" in ass


def test_ass_doc_look_and_overrides():
    segments = fake_segments([1.0], ["{危险}标签"])
    doc = subtitle_service.build_ass(
        segments, resolution=(1440, 2560), look=subtitle_service.SUBTITLE_LOOKS["doc"]
    )
    fields = next(x for x in doc.splitlines() if x.startswith("Style: "))[7:].split(",")
    assert fields[1] == "Songti SC" and fields[7] == "-1" and fields[2] == "64"
    # 台词里的花括号不能被当成 ASS 样式标签吃掉
    assert "｛危险｝标签" in doc

    custom = subtitle_service.build_ass(
        segments,
        resolution=(1920, 1080),
        look=subtitle_service.SUBTITLE_LOOKS["box"],
        font="PingFang SC",
        font_scale=0.06,
        margin_scale=0.2,
    )
    fields = next(x for x in custom.splitlines() if x.startswith("Style: "))[7:].split(",")
    assert fields[1] == "PingFang SC"
    assert fields[2] == "65", "1080 * 0.06"
    assert fields[15] == "3", "box = 半透明底条"
    assert fields[21] == "216", "显式 margin_scale 仍按 min(宽,高) 算"
    assert "\\blur" not in custom


def test_unknown_subtitle_look_falls_back_to_soft():
    assert subtitle_service.subtitle_look("fancy")[0] == "soft"
    assert subtitle_service.subtitle_look(" DOC ")[0] == "doc"


def test_burn_filter_reads_styled_ass():
    assert compose_service._burn_filter("subtitles.ass") == "subtitles=subtitles.ass"


def test_finish_burns_the_ass_file(tmp_path):
    seen: dict = {}

    async def fake_run(args, *, timeout_sec=0.0, cwd=None, exe=None):
        seen["args"] = args

    import videogen.services.compose_service as module

    original = module.run_ffmpeg
    module.run_ffmpeg = fake_run
    try:
        effective = asyncio.run(
            compose_service._finish(
                Path("base.mp4"),
                Path("final.mp4"),
                cwd=tmp_path,
                duration=10.0,
                bgm_path=None,
                bgm_gain_db=-20.0,
                fade_sec=1.5,
                subtitle_file=Path("subtitles.srt"),
                burn_file=Path("subtitles.ass"),
                mode=SubtitleMode.BURN,
                caps={"subtitles", "mov_text"},
                crf=23,
                preset="veryfast",
            )
        )
    finally:
        module.run_ffmpeg = original
    assert effective == SubtitleMode.BURN
    assert "subtitles=subtitles.ass" in " ".join(seen["args"])


def test_follow_clip_lengths_uses_actual_length_but_keeps_room_for_voice(tmp_path, monkeypatch):
    lengths = {"a.mp4": 3.21, "b.mp4": 2.88, "c.mp4": 2.5, "d.mp4": 4.4}

    async def fake_probe(path, **_):
        return lengths[Path(path).name]

    monkeypatch.setattr(compose_service, "probe_duration", fake_probe)
    segs = [
        ComposeSegment(shot_id=sid, index=i, duration_sec=dur, audio_path=audio)
        for i, (sid, dur, audio) in enumerate(
            [("a", 3.0, None), ("b", 4.0, "audio/b.wav"), ("c", 3.0, None), ("d", 4.0, None)]
        )
    ]
    manifest = VideoManifest(
        clips=[
            VideoClip(
                shot_id=sid,
                status=ClipStatus.READY,
                path=f"{sid}.mp4",
                segment_id="seg" if sid != "d" else "",
            )
            for sid in "abcd"
        ]
    )
    audio = AudioManifest(
        clips=[
            AudioClip(
                id="b",
                shot_id="b",
                text="台词",
                voice="v",
                status=ClipStatus.READY,
                path="audio/b.wav",
                duration_sec=3.4,
                offset_sec=0.1,
            )
        ]
    )
    asyncio.run(compose_service._follow_clip_lengths(segs, manifest, audio, tmp_path))
    by_id = {s.shot_id: s.duration_sec for s in segs}
    assert by_id["a"] == 3.21, "比分镜长：不再砍尾巴"
    assert by_id["b"] == 3.5, "片段 2.88 秒但配音要 3.5 秒：按配音"
    assert by_id["c"] == 2.5, "比分镜短：不再冻结补帧"
    assert by_id["d"] == 4.0, "单镜生成的片段不动（多出来的是档位尾巴）"


def test_segment_freshness_uses_mtime(tmp_path):
    clip = tmp_path / "clip.mp4"
    segment = tmp_path / "seg.mp4"
    clip.write_bytes(b"a")
    assert not compose_service._segment_is_fresh(segment, [clip])

    segment.write_bytes(b"b")
    assert compose_service._segment_is_fresh(segment, [clip])

    # 源文件更新 -> 旧分段作废
    import os
    import time

    time.sleep(0.01)
    os.utime(clip, None)
    assert not compose_service._segment_is_fresh(segment, [clip])


# ── pipeline / job 层（用 monkeypatch 避开真实 ffmpeg）──


class SlowCompose:
    """替代 compose_service.compose 的慢实现，用来测任务编排。"""

    def __init__(self, *, delay: float = 0.3, fail: bool = False, segments: int = 2):
        self.delay = delay
        self.fail = fail
        self.segments = segments
        self.calls = 0

    async def __call__(self, project, storyboard, video, audio, **kwargs) -> ComposeReport:
        self.calls += 1
        progress = kwargs.get("on_progress")
        if progress:
            progress(0, self.segments + 2, "开始渲染分段")
        await asyncio.sleep(self.delay)
        if self.fail:
            raise compose_service.ComposeError("假的合成失败")
        if progress:
            progress(self.segments + 2, self.segments + 2, "合成完成")
        segments = [
            ComposeSegment(
                shot_id=shot.id,
                scene_id=shot.scene_id,
                index=index,
                duration_sec=float(shot.duration_sec),
                video_path=f"clips/{shot.id}.mp4",
            )
            for index, shot in enumerate(storyboard.shots[: self.segments])
        ]
        return ComposeReport(
            output_path="output/final.mp4",
            subtitles_path="output/subtitles.srt",
            duration_sec=sum(s.duration_sec for s in segments),
            segments=segments,
        )


async def fake_media_project(store, *, shots: int = 2) -> Project:
    """用假 provider 把项目推到 AUDIO_READY（不需要 ffmpeg）。"""
    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: FakeVideoProvider(),
        tts_factory=lambda: FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    project = small_project(store, shots=shots)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)
    job = await pipeline.start_audio_generation(project.id)
    await wait_for_job(pipeline, job.id)
    return store.load_project(project.id)


@pytest.mark.asyncio
async def test_compose_requires_audio_ready(store, media_pipeline, video_ready_project):
    with pytest.raises(PipelineError) as exc:
        await media_pipeline.start_compose(video_ready_project.id)
    assert "配音" in str(exc.value)


@pytest.mark.asyncio
async def test_duplicate_compose_job_rejected(store, monkeypatch):
    pipeline = real_pipeline(store)
    project = await fake_media_project(store)
    monkeypatch.setattr(pipeline_module.compose_service, "compose", SlowCompose())

    job = await pipeline.start_compose(project.id)
    with pytest.raises(PipelineError) as exc:
        await pipeline.start_compose(project.id)
    assert "已有合成任务" in str(exc.value)

    await pipeline.cancel_compose(project.id)
    await wait_for_job(pipeline, job.id)


@pytest.mark.asyncio
async def test_cancel_compose_job_settles_status(store, monkeypatch):
    pipeline = real_pipeline(store)
    project = await fake_media_project(store)
    monkeypatch.setattr(pipeline_module.compose_service, "compose", SlowCompose(delay=0.4))

    job = await pipeline.start_compose(project.id)
    assert store.load_project(project.id).status == ProjectStatus.COMPOSING
    await asyncio.sleep(0.05)
    assert await pipeline.cancel_compose(project.id) == 1

    finished = await wait_for_job(pipeline, job.id)
    assert finished.status.value == "cancelled"
    assert store.load_project(project.id).status == ProjectStatus.AUDIO_READY
    assert store.load_compose_report(project.id) is None


@pytest.mark.asyncio
async def test_compose_failure_settles_back_to_audio_ready(store, monkeypatch):
    pipeline = real_pipeline(store)
    project = await fake_media_project(store)
    monkeypatch.setattr(pipeline_module.compose_service, "compose", SlowCompose(delay=0, fail=True))

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id)

    assert finished.status.value == "failed"
    assert "假的合成失败" in finished.error
    assert store.load_project(project.id).status == ProjectStatus.AUDIO_READY
    assert store.load_compose_report(project.id) is None


@pytest.mark.asyncio
async def test_compose_status_shape(store, monkeypatch):
    pipeline = real_pipeline(store)
    project = await fake_media_project(store)

    status = pipeline.compose_status(project.id)
    assert status["report"] is None and status["job"] is None
    assert status["ffmpeg_available"] is True

    monkeypatch.setattr(pipeline_module.compose_service, "compose", SlowCompose(delay=0))
    job = await pipeline.start_compose(project.id)
    await wait_for_job(pipeline, job.id)

    status = pipeline.compose_status(project.id)
    assert status["job"]["status"] == "succeeded"
    assert status["summary"]["segments"] == 2
    assert store.load_project(project.id).status == ProjectStatus.DONE


@pytest.mark.asyncio
async def test_recover_interrupted_composing(store, monkeypatch):
    pipeline = real_pipeline(store)
    project = await fake_media_project(store)

    def force_status(value: str) -> None:
        path = store.project_dir(project.id) / "meta.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["status"] = value
        path.write_text(json.dumps(raw), encoding="utf-8")

    force_status("composing")
    recovered = await pipeline.recover_interrupted_jobs()
    assert project.id in recovered
    assert store.load_project(project.id).status == ProjectStatus.AUDIO_READY


# ── 真实 ffmpeg 合成 ──────────────────────────────────


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_end_to_end(store):
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store))

    assert store.load_project(project.id).status == ProjectStatus.AUDIO_READY
    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=120)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    project_dir = store.ensure_project_dir(project.id)
    final = project_dir / report.output_path

    assert report.output_path == "output/final.mp4"
    assert final.is_file() and final.stat().st_size > 1000
    assert report.duration_sec == store.load_storyboard(project.id).total_duration_sec
    assert len(report.segments) == len(store.load_storyboard(project.id).shots)
    assert report.video_provider == "mock"
    assert report.tts_provider == "mock"
    assert report.subtitle_mode_effective == SubtitleMode.SOFT
    assert store.load_project(project.id).status == ProjectStatus.DONE
    # 中间产物不留在项目里
    assert not (project_dir / "output" / "segments").exists()
    assert not (project_dir / "output" / "final_base.mp4").exists()


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_writes_srt_sidecar(store):
    pipeline = real_pipeline(store)
    project = await prepare_media(
        store, pipeline, small_project(store, dialogue="这个项目是我做的。")
    )

    job = await pipeline.start_compose(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)

    report = store.load_compose_report(project.id)
    srt = (store.ensure_project_dir(project.id) / report.subtitles_path).read_text(encoding="utf-8")
    assert "林晚：这个项目是我做的。" in srt
    assert "-->" in srt


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_extends_short_clip_to_fit_audio(store):
    """配音比视频长时用冻结帧补齐，不重新烧视频额度。"""
    pipeline = real_pipeline(store)
    project = small_project(
        store, shots=1, shot_duration=1, dialogue="这是二十个字左右的配音台词内容。"
    )
    project = await prepare_media(store, pipeline, project)

    storyboard = store.load_storyboard(project.id)
    assert storyboard.shots[0].duration_sec > 1, "音画对齐应该把镜头拉长"
    assert storyboard.audio_aligned is True

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=120)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert report.duration_sec == storyboard.shots[0].duration_sec


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_subtitle_modes(store):
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2))

    # none：只导出 srt
    job = await pipeline.start_compose(project.id, overrides={"compose_subtitle_mode": "none"})
    await wait_for_job(pipeline, job.id, timeout=120)
    report = store.load_compose_report(project.id)
    assert report.subtitle_mode == SubtitleMode.NONE
    assert report.subtitle_mode_effective == SubtitleMode.NONE
    assert (store.ensure_project_dir(project.id) / report.subtitles_path).is_file()

    # burn：烧进画面（imageio-ffmpeg 自带 libass）
    job = await pipeline.start_compose(project.id, overrides={"compose_subtitle_mode": "burn"})
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    assert report.subtitle_mode == SubtitleMode.BURN
    assert (store.ensure_project_dir(project.id) / report.output_path).stat().st_size > 1000

    # soft：回到默认，内嵌 mov_text
    job = await pipeline.start_compose(project.id, overrides={"compose_subtitle_mode": "soft"})
    await wait_for_job(pipeline, job.id, timeout=120)
    assert store.load_compose_report(project.id).subtitle_mode_effective == SubtitleMode.SOFT


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_burn_uses_project_subtitle_style(store):
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2))
    await pipeline.update_project(project.id, {"subtitle_style": "doc"})

    job = await pipeline.start_compose(project.id, overrides={"compose_subtitle_mode": "burn"})
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    if report.subtitle_mode_effective != SubtitleMode.BURN:
        pytest.skip("本机没有带 libass 的 ffmpeg")
    assert report.subtitle_style == "doc"
    ass = (store.output_dir(project.id) / "subtitles.ass").read_text(encoding="utf-8")
    assert "Songti SC" in ass

    # 单次请求压过项目设置
    job = await pipeline.start_compose(
        project.id,
        overrides={"compose_subtitle_mode": "burn", "compose_subtitle_style": "box"},
    )
    await wait_for_job(pipeline, job.id, timeout=180)
    assert store.load_compose_report(project.id).subtitle_style == "box"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_speed_change_actually_burns_subtitles(store):
    """变速时 soft 必须真的改成烧字幕（以前只写了告警，_finish 拿到的仍是 soft）。"""
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2))
    job = await pipeline.start_compose(
        project.id, overrides={"compose_subtitle_mode": "soft", "compose_speed": 2.0}
    )
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    if not any("自动改为烧字幕" in w for w in report.warnings):
        pytest.skip("本机没有带 libass 的 ffmpeg")
    assert report.subtitle_mode_effective == SubtitleMode.BURN
    assert report.subtitle_style == "soft"


# ── 响度（EBU R128）────────────────────────────────────


def test_params_match_compares_content(tmp_path):
    """复用判据要比**内容**：只有文件名相同、内容不同，必须判为不匹配。"""
    marker = tmp_path / "scene_1_shot_1.params"
    assert not compose_service._params_match(marker, "gain=+0.0")
    marker.write_text("ambient=off mouth=off lipsync=on gain=+6.0", encoding="utf-8")
    assert compose_service._params_match(marker, "ambient=off mouth=off lipsync=on gain=+6.0")
    assert not compose_service._params_match(marker, "ambient=off mouth=off lipsync=on gain=+0.0")
    # 行尾空白不算差异
    marker.write_text("  ambient=off  \n", encoding="utf-8")
    assert compose_service._params_match(marker, "ambient=off")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_changing_loudness_settings_rerenders_segments(store, settings):
    """回归：关掉逐条归一之后必须重渲分段，不能复用上一轮归一后的分段。

    真实事故：标记原来只是文件名，旧标记还在、分段却已被另一套参数重渲过，
    于是「关闭归一」的对比跑复用到了归一后的分段（12/15 条量出来都是 -20.1）。
    """
    settings.audio_normalize = True
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2))

    job = await pipeline.start_compose(project.id, overrides={"compose_keep_segments": True})
    await wait_for_job(pipeline, job.id, timeout=180)
    segments = sorted(store.segments_dir(project.id).glob("*.mp4"))
    assert segments
    normalized = {path.name: path.stat().st_mtime_ns for path in segments}
    marker = store.segments_dir(project.id) / f"{segments[0].stem}.params"
    assert marker.is_file()
    assert "gain=" in marker.read_text(encoding="utf-8")

    job = await pipeline.start_compose(
        project.id, overrides={"audio_normalize": False, "compose_keep_segments": True}
    )
    await wait_for_job(pipeline, job.id, timeout=180)
    for path in sorted(store.segments_dir(project.id).glob("*.mp4")):
        assert path.stat().st_mtime_ns != normalized[path.name], f"{path.name} 被错误复用"


def test_final_loudness_filter_targets_and_limits():
    chain = compose_service._final_loudness_filter(-16.0, -1.5)
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in chain
    assert "alimiter=limit=0.8414" in chain  # -1.5dBFS → 线性


def test_loudness_filter_is_appended_to_the_mix():
    """成片响度归一是**最后一层**：混完 BGM 之后再整体归一。"""
    seen: dict = {}

    async def fake_run(args, *, timeout_sec=0.0, cwd=None, exe=None):
        seen["args"] = args

    import videogen.services.compose_service as module

    original = module.run_ffmpeg
    module.run_ffmpeg = fake_run
    try:
        asyncio.run(
            compose_service._finish(
                Path("base.mp4"),
                Path("final.mp4"),
                cwd=Path("."),
                duration=10.0,
                bgm_path=None,
                bgm_gain_db=-20.0,
                fade_sec=1.5,
                subtitle_file=None,
                mode=SubtitleMode.NONE,
                caps=set(),
                crf=23,
                preset="veryfast",
                loudness_normalize=True,
                target_lufs=-16.0,
                true_peak_db=-1.5,
            )
        )
    finally:
        module.run_ffmpeg = original
    args = seen["args"]
    chain = args[args.index("-filter_complex") + 1]
    assert chain.startswith("[0:a]loudnorm=I=-16")
    assert args[args.index("-map") + 1] == "0:v"
    assert "[aout]" in args
    # 归一必须重编码音频：`-c:v copy` 可以有（视频流不动），但音频不能 copy
    assert "-c:a" in args and args[args.index("-c:a") + 1] == "aac"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_normalizes_voice_and_final_loudness(store, settings):
    """端到端：逐条配音被拉到同一响度，成片响度落在目标附近。"""
    settings.audio_normalize = True
    settings.audio_target_lufs = -20.0
    settings.compose_loudness_normalize = True
    settings.compose_target_lufs = -16.0
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=3))

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)

    assert report.audio_normalize is True
    assert report.loudness_normalize is True
    assert report.voice_gains, "应该记下每条配音补了多少 dB"
    assert any("逐条配音响度归一" in w for w in report.warnings)
    assert report.final_lufs is not None
    # 成片响度应该被拉到目标附近（loudnorm 单遍允许有偏差）
    assert abs(report.final_lufs - settings.compose_target_lufs) < 2.0
    assert report.final_true_peak_db is None or report.final_true_peak_db <= 0.0


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_can_turn_loudness_normalization_off(store, settings):
    settings.audio_normalize = False
    settings.compose_loudness_normalize = False
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2))

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    assert report.audio_normalize is False
    assert report.loudness_normalize is False
    assert report.voice_gains == {}
    assert not any("响度" in w for w in report.warnings)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_voice_gain_is_applied_and_recorded(store, settings, tmp_path):
    """故意做一条小声 6dB 的配音：归一应该给它 +6dB 左右，并写进报告。"""
    settings.audio_normalize = True
    settings.audio_target_lufs = -20.0
    # 假配音本身很轻（-30 LUFS 量级），上限给小了会两条都顶到天花板，看不出差异
    settings.audio_normalize_max_db = 20.0
    pipeline = real_pipeline(store)
    project = small_project(store, shots=2)
    await prepare_media(store, pipeline, project)

    manifest = store.load_audio_manifest(project.id)
    target_clip = manifest.clips[0]
    quiet = tmp_path / "quiet.mp3"
    source = store.ensure_project_dir(project.id) / target_clip.path
    await run_ffmpeg(
        ["-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-af", "volume=-6dB", str(quiet)],
        timeout_sec=60,
    )
    target_clip.path = str(quiet)
    target_clip.duration_sec = target_clip.duration_sec
    store.save_audio_manifest(project.id, manifest)

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    gains = report.voice_gains
    assert target_clip.shot_id in gains
    # 这条比其他条小声 6dB，增益就应该明显更大
    others = [value for key, value in gains.items() if key != target_clip.shot_id]
    assert others
    assert gains[target_clip.shot_id] - max(others) > 3.0


# ── 配乐混音滤镜（纯函数）──────────────────────────────


def test_bgm_mix_filter_without_duck_uses_fixed_gain():
    chain = compose_service.bgm_mix_filter(
        gain_db=-20.0, fade_sec=1.5, duration=10.0, duck=False
    )
    assert "volume=-20.0dB" in chain
    assert "afade=t=in:st=0:d=1.50" in chain
    assert "afade=t=out:st=8.50:d=1.50" in chain
    assert "atrim=duration=10.000" in chain
    assert chain.endswith(
        "[0:a][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )
    assert "sidechaincompress" not in chain


def test_bgm_mix_filter_with_duck_wires_sidechain():
    chain = compose_service.bgm_mix_filter(
        gain_db=-18.0,
        fade_sec=2.0,
        duration=20.0,
        duck=True,
        duck_ratio=6.0,
        duck_threshold_db=-30.0,
        duck_attack_ms=25.0,
        duck_release_ms=420.0,
    )
    # 人声要分成两路：一路进最终混音，一路当 sidechain
    assert "[0:a]asplit=2[voice][side]" in chain
    assert "[music][side]sidechaincompress=" in chain
    assert "ratio=6" in chain
    assert "attack=25" in chain
    assert "release=420" in chain
    assert "makeup=1[ducked]" in chain
    # -30dBFS → 线性 0.0316
    assert "threshold=0.03162" in chain
    assert chain.endswith(
        "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )


def test_bgm_mix_filter_fade_never_exceeds_a_third():
    """很短的片子别把淡入淡出算成整段。"""
    chain = compose_service.bgm_mix_filter(gain_db=-20.0, fade_sec=1.5, duration=1.2)
    assert "afade=t=in:st=0:d=0.40" in chain
    assert "afade=t=out:st=0.80:d=0.40" in chain


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_with_real_bgm(store, tmp_path):
    provider = MockTTSProvider()
    from videogen.providers.tts.base import TTSRequest

    bgm_file = tmp_path / "tense_01.mp3"
    await provider.synthesize(
        TTSRequest(clip_id="bgm", text="背景音乐占位音轨内容。", output_path=bgm_file)
    )
    pipeline = real_pipeline(store, bgm=FakeBgmLibrary(path=str(bgm_file)))
    project = await prepare_media(store, pipeline, small_project(store, shots=2))

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=120)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert report.bgm_id == "fake_bgm"
    assert report.bgm_gain_db == get_settings().bgm_gain_db
    assert report.bgm_engine == "library"
    assert report.bgm_duck is True
    assert report.warnings  # 至少有一条说明用了什么音乐
    # 配乐成功时会记一条「用了什么音乐」的说明（不是问题），但绝不能有失败告警
    assert any("配乐（本地曲库）" in w for w in report.warnings)
    assert not any("配乐跳过" in w or "BGM 文件不存在" in w for w in report.warnings)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_warns_when_bgm_file_missing(store):
    pipeline = real_pipeline(store)  # 假曲库指向不存在的文件
    project = await prepare_media(store, pipeline, small_project(store, shots=2))

    job = await pipeline.start_compose(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)

    report = store.load_compose_report(project.id)
    assert any("BGM 文件不存在" in w for w in report.warnings)
    assert (store.ensure_project_dir(project.id) / report.output_path).is_file()


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_skips_missing_clip_file(store):
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2))

    # 手动删掉一个片段文件：合成应该跳过它并告警，而不是整条失败
    storyboard = store.load_storyboard(project.id)
    victim = storyboard.shots[0].id
    (store.clips_dir(project.id) / f"{victim}.mp4").unlink()

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=120)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert len(report.segments) == len(storyboard.shots) - 1
    assert any(victim in w for w in report.warnings)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_is_idempotent_on_rerun(store):
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2))

    job = await pipeline.start_compose(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)
    first = (store.ensure_project_dir(project.id) / "output" / "final.mp4").stat().st_size

    job2 = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job2.id, timeout=120)
    assert finished.status.value == "succeeded", finished.error

    second = (store.ensure_project_dir(project.id) / "output" / "final.mp4").stat().st_size
    assert second > 0
    assert abs(second - first) < first * 0.5  # 同样的输入 -> 同样的成片
    assert store.load_project(project.id).status == ProjectStatus.DONE


@requires_ffmpeg
@pytest.mark.asyncio
async def test_burn_degrades_when_libass_is_missing(store, monkeypatch):
    """请求烧字幕但**任何** ffmpeg 都没有 libass 时，必须降级成软字幕 + 明确告警。"""
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=1))

    async def fake_caps() -> set[str]:
        return {"mov_text", "libx264", "aac"}  # 故意没有 subtitles

    async def no_libass_candidate(required):
        # 候选里也找不到带 libass 的（否则会「升级」而不是降级）
        return resolve_ffmpeg(), {"mov_text", "libx264", "aac"}

    monkeypatch.setattr(pipeline_module.compose_service, "ffmpeg_capabilities", fake_caps)
    monkeypatch.setattr(
        pipeline_module.compose_service, "resolve_ffmpeg_for", no_libass_candidate
    )

    job = await pipeline.start_compose(project.id, overrides={"compose_subtitle_mode": "burn"})
    finished = await wait_for_job(pipeline, job.id, timeout=120)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert report.subtitle_mode == SubtitleMode.BURN
    assert report.subtitle_mode_effective == SubtitleMode.SOFT
    assert report.subtitle_ffmpeg == ""
    assert any("libass" in w for w in report.warnings)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_burn_upgrades_to_a_libass_capable_ffmpeg(store, monkeypatch):
    """主 ffmpeg 没有 libass 时，自动换用候选里带 libass 的那个，真的把字幕烧进画面。"""
    portable = pytest.importorskip("imageio_ffmpeg").get_ffmpeg_exe()
    portable_caps = await ffmpeg_capabilities_for(portable)
    if "subtitles" not in portable_caps or "libx264" not in portable_caps:
        pytest.skip("imageio-ffmpeg 便携版不带 libass，无法验证升级路径")

    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=1))

    async def fake_caps() -> set[str]:
        return {"mov_text", "libx264", "aac"}  # 主 ffmpeg 假装没有 libass

    monkeypatch.setattr(pipeline_module.compose_service, "ffmpeg_capabilities", fake_caps)

    job = await pipeline.start_compose(project.id, overrides={"compose_subtitle_mode": "burn"})
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert report.subtitle_mode_effective == SubtitleMode.BURN
    assert report.subtitle_ffmpeg == str(Path(portable).resolve())
    assert any("libass" in w for w in report.warnings)

    # 烧进去的字幕不再是独立流：成片里只剩 video + audio
    final = store.ensure_project_dir(project.id) / report.output_path
    streams = await probe_streams(final)
    assert streams is not None and "video" in streams
    assert "subtitle" not in streams


@requires_ffmpeg
@pytest.mark.asyncio
async def test_subtitles_fall_back_to_srt_only_when_mov_text_missing(store, monkeypatch):
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=1))

    async def fake_caps() -> set[str]:
        return {"libx264", "aac"}  # 既不能烧也不能内嵌

    monkeypatch.setattr(pipeline_module.compose_service, "ffmpeg_capabilities", fake_caps)

    job = await pipeline.start_compose(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)

    report = store.load_compose_report(project.id)
    assert report.subtitle_mode_effective == SubtitleMode.NONE
    assert (store.ensure_project_dir(project.id) / report.subtitles_path).is_file()
    assert any("mov_text" in w for w in report.warnings)
    # 即使降级到「只有侧挂文件」，也要有浏览器能用的 vtt
    assert report.subtitles_vtt_path
    vtt = (store.ensure_project_dir(project.id) / report.subtitles_vtt_path).read_text("utf-8")
    assert vtt.startswith("WEBVTT")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_mock_audio_and_video_have_expected_kinds(store):
    """占位产物绝不能进合成：这里顺带确认 mock provider 在真 ffmpeg 下产出真媒体。"""
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=1))

    video = store.load_video_manifest(project.id)
    audio = store.load_audio_manifest(project.id)
    assert video.is_usable and audio.is_usable
    assert all(c.media_kind == MediaKind.VIDEO for c in video.ready_clips)
    assert all(c.media_kind == MediaKind.AUDIO for c in audio.ready_clips)


# ── 字幕文本格式（srt / vtt）─────────────────────────────
# Web 预览必须靠 vtt：浏览器既不认 srt，也不渲染 mp4 里的 mov_text 软字幕轨。


def _cues() -> list[ComposeSegment]:
    return [
        ComposeSegment(shot_id="s1", index=0, duration_sec=2.5, text="林悦：你好"),
        ComposeSegment(shot_id="s2", index=1, duration_sec=2.5, text="李薇儿：再见"),
        ComposeSegment(shot_id="s3", index=2, duration_sec=2.0, text=""),  # 静音镜头不出字幕
    ]


def test_build_vtt_uses_dot_milliseconds_and_skips_empty_text():
    vtt = subtitle_service.build_vtt(_cues())
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:02.500" in vtt
    assert "00:00:02.500 --> 00:00:05.000" in vtt
    assert vtt.count("-->") == 2  # 空文本不产生 cue
    assert "林悦：你好" in vtt
    assert "," not in vtt.split("-->")[0][-30:]  # 时间戳里没有逗号


def test_build_srt_and_vtt_have_the_same_cues():
    segments = _cues()
    srt = subtitle_service.build_srt(segments)
    vtt = subtitle_service.build_vtt(segments)
    assert srt.count("-->") == vtt.count("-->")
    for cue in ("林悦：你好", "李薇儿：再见"):
        assert cue in srt and cue in vtt


def test_srt_to_vtt_converts_existing_files():
    srt = "1\n00:00:01,000 --> 00:00:03,250\n对白\n"
    vtt = subtitle_service.srt_to_vtt(srt)
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:03.250" in vtt
    assert "对白" in vtt
    # 已经是 vtt 就原样返回（幂等）
    assert subtitle_service.srt_to_vtt(vtt).strip() == vtt.strip()
    assert subtitle_service.srt_to_vtt("").strip() == "WEBVTT"


def test_build_vtt_without_cues_is_still_valid():
    assert subtitle_service.build_vtt([]) == "WEBVTT\n"


# ── 口型对齐（把配音延后到画面开口时刻）────────────────────────


def test_segment_filter_delays_voice_when_asked():
    """口型对齐 = 给配音加 adelay；不加就保持原样（否则嘴还没动声音先到）。"""
    from videogen.services.compose_service import _segment_filter

    base = _segment_filter(1080, 1920, 5.0, 24, voice=True, ambient=False)
    assert "adelay" not in base

    delayed = _segment_filter(
        1080, 1920, 5.0, 24, voice=True, ambient=False, voice_delay_ms=1220
    )
    assert "adelay=1220|1220" in delayed
    # 延迟必须在 apad/atrim 之前：先延后，再用静音补满、裁到片段长度
    assert delayed.index("adelay") < delayed.index("apad")


def test_segment_filter_delay_also_applies_when_mixing_ambient():
    from videogen.services.compose_service import _segment_filter

    chain = _segment_filter(
        1080, 1920, 5.0, 24, voice=True, ambient=True, voice_delay_ms=800
    )
    assert "adelay=800|800" in chain
    assert "amix" in chain  # 环境音仍然混，只是人声被延后


async def _make_voice_sample(path: Path, *, silence_sec: float, speech_sec: float) -> bool:
    """造一段「先静音 X 秒、再出声 Y 秒」的音频（模拟画面里的人晚开口）。"""
    from videogen.utils.ffmpeg import FFmpegError, ffmpeg_available, run_ffmpeg

    if not ffmpeg_available():
        return False
    # 注意：`atrim=duration=0` 不等于「裁成 0 秒」，它会读成不裁剪；
    # 而 anullsrc/sine 都是无限长的源 —— 静音为 0 时必须只用一路输入，否则 ffmpeg 永远不结束
    if silence_sec > 0.01:
        args = [
            "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-f", "lavfi", "-i", "sine=frequency=440:r=24000",
            "-filter_complex",
            f"[0:a]atrim=duration={silence_sec}[a0];"
            f"[1:a]atrim=duration={speech_sec}[a1];"
            f"[a0][a1]concat=n=2:v=0:a=1[a]",
            "-map", "[a]",
        ]
    else:
        args = [
            "-f", "lavfi", "-i", "sine=frequency=440:r=24000",
            "-filter_complex",
            f"[0:a]atrim=duration={speech_sec}[a]",
            "-map", "[a]",
        ]
    try:
        await run_ffmpeg(
            ["-y", "-hide_banner", "-loglevel", "error", *args, "-c:a", "pcm_s16le", str(path)],
            timeout_sec=60,
        )
    except FFmpegError:  # pragma: no cover - 环境问题
        return False
    return path.is_file()


@pytest.mark.asyncio
async def test_speech_span_finds_when_the_mouth_starts_moving(tmp_path):
    """真实样本的形状：开头 1.22 秒没声音（嘴还没动），之后才开始说话。"""
    from videogen.utils.audio_align import mouth_align_delay, speech_span

    sample = tmp_path / "late.wav"
    if not await _make_voice_sample(sample, silence_sec=1.22, speech_sec=1.0):
        pytest.skip("需要 ffmpeg 才能分析音轨")

    span = await speech_span(sample, tmp_path)
    assert span is not None
    assert abs(span[0] - 1.22) < 0.1
    delay = await mouth_align_delay(sample, tmp_path, voice_sec=1.0, segment_sec=5.0)
    assert abs(delay - 1.22) < 0.1


@pytest.mark.asyncio
async def test_speech_span_is_zero_when_speech_starts_immediately(tmp_path):
    from videogen.utils.audio_align import mouth_align_delay, speech_span

    sample = tmp_path / "now.wav"
    if not await _make_voice_sample(sample, silence_sec=0.0, speech_sec=1.0):
        pytest.skip("需要 ffmpeg 才能分析音轨")
    span = await speech_span(sample, tmp_path)
    assert span is not None and span[0] <= 0.05
    assert await mouth_align_delay(sample, tmp_path, voice_sec=1.0, segment_sec=5.0) == 0.0


@pytest.mark.asyncio
async def test_mouth_align_delay_never_pushes_voice_past_the_segment(tmp_path):
    """尾音会被切掉时宁可不延后（对齐不能以丢字为代价）。"""
    from videogen.utils.audio_align import mouth_align_delay

    sample = tmp_path / "late2.wav"
    if not await _make_voice_sample(sample, silence_sec=1.22, speech_sec=1.0):
        pytest.skip("需要 ffmpeg 才能分析音轨")
    # 配音 4.4s + 延后 1.22s = 5.62s > 5s 片段 → 只能延后到刚好塞得下（0.55s）
    delay = await mouth_align_delay(sample, tmp_path, voice_sec=4.4, segment_sec=5.0)
    assert abs(delay - 0.55) < 0.05


@pytest.mark.asyncio
async def test_mouth_align_delay_respects_the_cap(tmp_path):
    from videogen.utils.audio_align import mouth_align_delay

    sample = tmp_path / "later.wav"
    if not await _make_voice_sample(sample, silence_sec=2.5, speech_sec=1.0):
        pytest.skip("需要 ffmpeg 才能分析音轨")
    delay = await mouth_align_delay(
        sample, tmp_path, voice_sec=1.0, segment_sec=8.0, max_delay_sec=1.0
    )
    assert delay == 1.0


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_mouth_alignment_is_wired_and_reported(store, settings):
    """打开开关后：配音被延后（adelay 进滤镜），报告里写明有几个镜头被延后。"""
    from videogen.utils.ffmpeg import FFmpegError, run_ffmpeg

    settings.compose_align_dub_to_mouth = True
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store))

    # 给第一个镜头的片段塞一段「1.2 秒后才开口」的音轨，模拟视频模型的原生语音
    manifest = store.load_video_manifest(project.id)
    project_dir = store.ensure_project_dir(project.id)
    first = manifest.ready_clips[0]
    clip_path = project_dir / first.path
    delayed = project_dir / "clips" / "delayed.mp4"
    try:
        await run_ffmpeg(
            [
                "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(clip_path),
                "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                "-f", "lavfi", "-i", "sine=frequency=440:r=24000",
                "-filter_complex",
                # 视频要够长，否则音频会被 -shortest 截掉（那样就测不出「晚开口」）
                "[0:v]tpad=stop_mode=clone:stop_duration=3[v];"
                "[1:a]atrim=duration=1.2[a0];[2:a]atrim=duration=1.0[a1];"
                "[a0][a1]concat=n=2:v=0:a=1[a]",
                "-map", "[v]", "-map", "[a]", "-t", "3",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                "-c:a", "aac", str(delayed),
            ],
            timeout_sec=120,
        )
    except FFmpegError as exc:  # pragma: no cover
        pytest.skip(f"ffmpeg 无法构造测试片段：{str(exc)[:120]}")
    first.path = "clips/delayed.mp4"
    store.save_video_manifest(project.id, manifest)

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    # 第一镜被延后（其余镜头的 mock 音轨是满的/没有音轨，不触发）
    assert report.warnings, report.model_dump()
    assert any("口型对齐" in w for w in report.warnings), report.warnings
    # 字幕跟着延后的配音走：第一条字幕不能在开口之前就出现
    srt = (store.output_dir(project.id) / "subtitles.srt").read_text(encoding="utf-8")
    first_start = srt.split("\n")[1].split(" --> ")[0]
    assert first_start > "00:00:00,000", srt[:200]


# ── 逐帧口型（用 ffmpeg 当假引擎，验证接线而不是模型质量）──────────


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_uses_lipsynced_video_when_engine_set(store, monkeypatch):
    """开了逐帧口型：分段渲染必须用重画后的画面，并落一份口型报告。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "lipsync_engine", "cli")
    monkeypatch.setattr(
        settings,
        "lipsync_cmd",
        "ffmpeg -y -hide_banner -loglevel error -i {video} -i {audio} "
        "-map 0:v:0 -an -c:v libx264 -preset ultrafast -pix_fmt yuv420p {out}",
    )
    monkeypatch.setattr(settings, "lipsync_measure", False)
    monkeypatch.setattr(settings, "compose_align_dub_to_mouth", True)

    pipeline = real_pipeline(store)
    project = await prepare_media(
        store, pipeline, small_project(store, dialogue="这个项目是我做的。")
    )
    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    project_dir = store.ensure_project_dir(project.id)
    assert any("逐帧口型" in item for item in report.warnings), report.warnings
    # 做过口型的镜头不再叠「把配音整体延后」（否则嘴和声音会错开）
    assert not any("口型对齐：" in item for item in report.warnings), report.warnings
    assert (project_dir / "output" / "lipsync_report.json").is_file()


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_warns_and_continues_when_lipsync_engine_fails(store, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "lipsync_engine", "cli")
    monkeypatch.setattr(settings, "lipsync_cmd", "/bin/sh -c 'exit 9' {video} {audio} {out}")

    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store))
    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    # 可选后处理失败绝不能把合成打死
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    assert any("逐帧口型跳过" in item for item in report.warnings), report.warnings
    assert (store.ensure_project_dir(project.id) / report.output_path).is_file()


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_skips_lipsync_for_narration_only_shots(store, monkeypatch):
    """旁白镜头不该让画面里的人跟着旁白动嘴。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "lipsync_engine", "cli")
    monkeypatch.setattr(
        settings,
        "lipsync_cmd",
        "ffmpeg -y -hide_banner -loglevel error -i {video} -i {audio} "
        "-map 0:v:0 -an -c:v libx264 -preset ultrafast -pix_fmt yuv420p {out}",
    )
    monkeypatch.setattr(settings, "lipsync_measure", False)

    pipeline = real_pipeline(store)
    # 分镜里没有台词（只有画面）→ 这类镜头不该做口型
    project = await prepare_media(store, pipeline, small_project(store, dialogue=""))
    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    assert not any("逐帧口型" in item for item in report.warnings), report.warnings


# ── 成片变速（COMPOSE_SPEED）────────────────────────────
# 动机：视频模型按**最短档位**计费（H3 = 5 秒），所以 5 秒镜头最划算 ——
# 排 5 秒镜头再整体 2 倍速，每镜观感 2.5 秒，档位一分不浪费。


def test_atempo_chain_keeps_every_step_legal():
    """任意倍率都拆成 0.5~2.0 的合法级联：老版 ffmpeg 不认 atempo=4。"""
    assert compose_service.atempo_chain(1.0) == "atempo=1"
    assert compose_service.atempo_chain(2.0) == "atempo=2"
    assert compose_service.atempo_chain(1.5) == "atempo=1.5"
    assert compose_service.atempo_chain(4.0) == "atempo=2,atempo=2"
    assert compose_service.atempo_chain(3.0) == "atempo=2,atempo=1.5"
    for speed in (0.5, 0.8, 1.0, 1.7, 2.0, 3.3, 4.0):
        product = 1.0
        for step in compose_service.atempo_chain(speed).split(","):
            factor = float(step.split("=")[1])
            assert 0.5 <= factor <= 2.0, step
            product *= factor
        assert product == pytest.approx(speed, rel=1e-9)


def test_atempo_chain_rejects_non_positive_speed():
    with pytest.raises(compose_service.ComposeError):
        compose_service.atempo_chain(0)


def test_speed_filter_scales_pts_and_keeps_pitch():
    """视频缩放 PTS，音频走 atempo —— 故意不用 asetrate（那会把配音变花栗鼠）。"""
    chain = compose_service.speed_filter(2.0)
    assert chain == "[0:v]setpts=PTS/2[v];[0:a]atempo=2[a]"
    assert "asetrate" not in chain


def test_srt_timeline_scales_with_speed():
    """变速后侧挂字幕必须跟着缩短，否则会比画面晚一倍。"""
    segments = fake_segments([2.0, 2.0], ["第一句", "第二句"])

    normal = subtitle_service.build_srt(segments)
    assert "00:00:00,000 --> 00:00:02,000" in normal
    assert "00:00:02,000 --> 00:00:04,000" in normal

    fast = subtitle_service.build_srt(segments, speed=2.0)
    assert "00:00:00,000 --> 00:00:01,000" in fast
    assert "00:00:01,000 --> 00:00:02,000" in fast

    vtt = subtitle_service.build_vtt(segments, speed=2.0)
    assert "00:00:00.000 --> 00:00:01.000" in vtt


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_speed_halves_the_delivered_film(store):
    """2 倍速：交付时长减半，且音画仍同步（配音/口型/字幕一起被缩放）。"""
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2, shot_duration=2))

    job = await pipeline.start_compose(project.id, overrides={"compose_speed": 2.0})
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    project_dir = store.ensure_project_dir(project.id)
    final = project_dir / report.output_path

    assert report.speed == 2.0
    assert report.source_duration_sec == store.load_storyboard(project.id).total_duration_sec
    assert report.duration_sec == pytest.approx(report.source_duration_sec / 2, abs=0.01)
    assert any("2 倍速" in item for item in report.warnings), report.warnings

    if ffprobe_available():
        measured = await probe_duration(final)
        assert measured is not None
        assert measured == pytest.approx(report.duration_sec, abs=0.35)
        # 音画同步自检在变速**之后**跑，必须仍然是通的
        sync = await check_av_sync(final)
        assert sync.ok, sync

    # 侧挂字幕的时间轴必须一起缩短
    srt = (project_dir / report.subtitles_path).read_text(encoding="utf-8")
    ends = [
        line.partition("-->")[2].strip()
        for line in srt.splitlines()
        if "-->" in line
    ]
    assert ends
    last_end = _stamp_to_seconds(ends[-1])
    assert last_end <= report.duration_sec + 0.05, (last_end, report.duration_sec)


def _stamp_to_seconds(stamp: str) -> float:
    hours, minutes, rest = stamp.split(":")
    seconds, _, millis = rest.partition(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_speed_one_is_a_no_op(store):
    """默认 1.0 倍速不能多跑一趟 ffmpeg、也不能改时长口径。"""
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=2, shot_duration=2))

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert report.speed == 1.0
    assert report.duration_sec == report.source_duration_sec
    assert not any("倍速" in item for item in report.warnings), report.warnings


def test_apply_speed_pins_exact_duration():
    """回归：变速必须带 `-t` 掐到精确时长。

    真实事故：`atempo` 输出不精确等于「源时长 / speed」，AAC 还会再补 padding，
    67s 成片 2 倍速实测视频 33.583s / 音频 33.753s（差 0.17s），
    刚好越过 0.20s 的音画同步阈值被报成「成片音画不同步」。
    """
    seen: dict = {}

    async def fake_run(args, *, timeout_sec=0.0, cwd=None, exe=None):
        seen["args"] = list(args)

    import videogen.services.compose_service as module

    original = module.run_ffmpeg
    module.run_ffmpeg = fake_run
    try:
        asyncio.run(
            compose_service.apply_speed(
                Path("final.mp4"),
                Path("final_speed.mp4"),
                speed=2.0,
                duration=33.5,
                fps=24,
                crf=23,
                preset="veryfast",
                cwd=Path("."),
            )
        )
    finally:
        module.run_ffmpeg = original

    args = seen["args"]
    assert "-t" in args
    assert args[args.index("-t") + 1] == "33.500"
    # 帧率必须显式给：否则 setpts 改完时间戳会被 CFR 转换吃掉
    assert args[args.index("-r") + 1] == "24"
    assert "setpts=PTS/2" in " ".join(args)
