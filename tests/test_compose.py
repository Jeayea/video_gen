from __future__ import annotations

import asyncio
import json

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
from videogen.utils.ffmpeg import ffmpeg_available

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


def test_subtitle_style_scales_with_reference_side():
    # 竖屏/横屏都用 min(宽,高) 作参照，字号必须一致
    portrait = compose_service.subtitle_style((1080, 1920))
    landscape = compose_service.subtitle_style((1920, 1080))
    assert portrait == landscape
    assert portrait[0] == 54, "1080 * 5% = 54px"
    assert portrait[1] == 43
    # 极端分辨率也不能崩
    assert compose_service.subtitle_style((16, 16))[0] >= 10


def test_burn_filter_uses_ass_script_units():
    """force_style 的字号是 384x288 参考系单位，必须换算，否则竖屏字幕溢出。"""
    portrait = compose_service._burn_filter("subtitles.srt", resolution=(1080, 1920))
    # 54px @1920 高 -> 54*288/1920 = 8
    assert "FontSize=8" in portrait
    assert "MarginV=6" in portrait

    landscape = compose_service._burn_filter("subtitles.srt", resolution=(1920, 1080))
    # 54px @1080 高 -> 14
    assert "FontSize=14" in landscape


def test_script_units_conversion():
    assert compose_service.script_units(54, 1920) == 8
    assert compose_service.script_units(54, 1080) == 14
    assert compose_service.script_units(0, 1920) == 1  # 不能算出 0 让 libass 报错


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
    assert not any("BGM" in w for w in report.warnings)


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
    """请求烧字幕但 ffmpeg 没有 libass 时，必须降级成软字幕 + 明确告警。"""
    pipeline = real_pipeline(store)
    project = await prepare_media(store, pipeline, small_project(store, shots=1))

    async def fake_caps() -> set[str]:
        return {"mov_text", "libx264", "aac"}  # 故意没有 subtitles

    monkeypatch.setattr(pipeline_module.compose_service, "ffmpeg_capabilities", fake_caps)

    job = await pipeline.start_compose(project.id, overrides={"compose_subtitle_mode": "burn"})
    finished = await wait_for_job(pipeline, job.id, timeout=120)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert report.subtitle_mode == SubtitleMode.BURN
    assert report.subtitle_mode_effective == SubtitleMode.SOFT
    assert any("libass" in w for w in report.warnings)


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
