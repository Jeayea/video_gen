"""原声旁白直通：视频模型已经在原声里念过旁白（`VIDEO_PROMPT_NARRATION`）的镜头，
配音阶段不再用 TTS 念第二遍，合成时按原音量用片段原声。"""

from __future__ import annotations

import pytest

from tests.conftest import wait_for_job
from tests.factories import small_project
from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider
from videogen.models.audio import AudioClip, AudioKind, AudioManifest
from videogen.models.project import Project
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import ClipStatus, MediaKind, VideoClip, VideoManifest
from videogen.orchestrator.pipeline import Pipeline
from videogen.providers.tts.mock import MockTTSProvider
from videogen.providers.video.base import VideoRequest
from videogen.providers.video.mock import MockVideoProvider
from videogen.services import audio_service, preflight_service, video_service
from videogen.utils.ffmpeg import ffmpeg_available, ffprobe_available, probe_streams, run_ffmpeg

NARRATION = "两手持帘，入水一荡。"


def _shot(shot_id: str, **kwargs) -> Shot:
    return Shot(
        id=shot_id, scene_id="scene_1", shot_no=1, duration_sec=3, video_prompt="a vat", **kwargs
    )


def _clip(shot_id: str, prompt: str, **kwargs) -> VideoClip:
    return VideoClip(
        shot_id=shot_id,
        scene_id="scene_1",
        status=ClipStatus.READY,
        media_kind=MediaKind.VIDEO,
        path=f"clips/{shot_id}.mp4",
        prompt=prompt,
        **kwargs,
    )


def _directive(settings, monkeypatch, shot: Shot) -> str:
    monkeypatch.setattr(settings, "video_prompt_narration", True)
    return video_service.build_prompt(shot, Project(title="t"))


def test_native_shots_are_read_from_the_prompt_that_was_sent(settings, monkeypatch):
    spoken = _shot("s1", narration=NARRATION)
    plain = _shot("s2", narration="这句没交给视频模型。")
    talk = _shot("s3", narration=NARRATION, dialogue_text="对呀！", dialogue_speaker="char_1")
    storyboard = Storyboard(shots=[spoken, plain, talk])
    sent = _directive(settings, monkeypatch, spoken)
    manifest = VideoManifest(
        clips=[_clip("s1", sent), _clip("s2", "a vat, no narration"), _clip("s3", sent)]
    )

    assert video_service.native_narration_shots(storyboard, manifest) == {"s1"}

    # 判据是「当时发出去的 prompt」，不是现在的开关：事后关掉开关，原声里那句旁白也还在
    monkeypatch.setattr(settings, "video_prompt_narration", False)
    assert video_service.native_narration_shots(storyboard, manifest) == {"s1"}
    # 旁白改了字 = 原声念的已经不是这一句，要重新配
    spoken.narration = "改过的旁白。"
    assert video_service.native_narration_shots(storyboard, manifest) == set()
    assert video_service.native_narration_shots(storyboard, None) == set()


def test_native_shots_need_a_ready_video_clip(settings, monkeypatch):
    spoken = _shot("s1", narration=NARRATION)
    sent = _directive(settings, monkeypatch, spoken)
    failed = _clip("s1", sent)
    failed.status = ClipStatus.FAILED
    assert video_service.native_narration_shots(
        Storyboard(shots=[spoken]), VideoManifest(clips=[failed])
    ) == set()


@pytest.mark.asyncio
async def test_audio_stage_skips_native_narration_and_drops_old_tts(store):
    project = small_project(store, shots=2)
    storyboard = store.load_storyboard(project.id)
    storyboard.shots[0].dialogue_text = None
    storyboard.shots[0].dialogue_speaker = None
    storyboard.shots[0].narration = NARRATION
    old = AudioClip(
        id=storyboard.shots[0].id,
        shot_id=storyboard.shots[0].id,
        kind=AudioKind.NARRATION,
        text=NARRATION,
    )
    manifest = await audio_service.synthesize_all(
        project,
        storyboard,
        store.load_bible(project.id),
        FakeTTSProvider(),
        store.audio_dir(project.id),
        store.ensure_project_dir(project.id),
        retry_backoff_sec=0,
        existing=AudioManifest(clips=[old]),
        native_narration={storyboard.shots[0].id},
    )
    # 旁白镜头不配、旧的 TTS 条目作废；台词镜头照常配
    assert [c.shot_id for c in manifest.clips] == [storyboard.shots[1].id]
    assert manifest.clips[0].status == ClipStatus.READY
    assert any(w.startswith("原声旁白：1 个镜头") for w in manifest.warnings)


def test_audio_preflight_does_not_charge_native_narration():
    storyboard = Storyboard(
        shots=[_shot("s1", narration=NARRATION), _shot("s2", narration="再一句。")]
    )
    full = preflight_service.audio_plan(storyboard, None)
    native = preflight_service.audio_plan(storyboard, None, native_narration={"s1"})
    assert native["tts_chars"] < full["tts_chars"]
    assert any("原声直出" in w for w in native["warnings"])


requires_ffmpeg = pytest.mark.skipif(
    not (ffmpeg_available() and ffprobe_available()), reason="需要 ffmpeg / ffprobe"
)


class _ClipWithVoice(MockVideoProvider):
    """mock 片段上加一条音轨，模拟视频模型念出来的原生旁白。"""

    async def generate(self, request: VideoRequest):  # type: ignore[override]
        clip = await super().generate(request)
        voiced = clip.path.with_name(clip.path.stem + "_voice.mp4")
        await run_ffmpeg(
            [
                "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(clip.path),
                "-f", "lavfi", "-i",
                f"sine=frequency=220:sample_rate=44100:duration={request.duration_sec}",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
                "-shortest", str(voiced),
            ],
            timeout_sec=60,
        )
        clip.path.unlink(missing_ok=True)
        return clip.model_copy(update={"path": voiced})


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_uses_native_narration_at_full_level(store, settings, monkeypatch):
    """全链路：视频原生念旁白 → 配音阶段一条都不配 → 合成用原声（不压到环境音的 -20dB）+ 字幕。"""
    monkeypatch.setattr(settings, "video_prompt_narration", True)
    project = small_project(store, shots=2, shot_duration=1)
    storyboard = store.load_storyboard(project.id)
    for shot in storyboard.shots:
        shot.dialogue_text = None
        shot.dialogue_speaker = None
        shot.narration = NARRATION
    store.save_storyboard(project.id, storyboard)
    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: _ClipWithVoice(),
        tts_factory=lambda: MockTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )

    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)
    job = await pipeline.start_audio_generation(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=120)
    assert finished.status.value == "succeeded", finished.error
    assert store.load_audio_manifest(project.id).clips == []

    # 环境音开关是关的（默认 off），原声旁白也必须进成片
    job = await pipeline.start_compose(
        project.id, overrides={"bgm_engine": "none", "compose_keep_segments": True}
    )
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert all(segment.native_voice for segment in report.segments)
    assert any(w.startswith("原声旁白：2 个镜头") for w in report.warnings)
    assert not any("纯画面" in w for w in report.warnings)
    project_dir = store.ensure_project_dir(project.id)
    assert "audio" in (await probe_streams(project_dir / report.output_path) or {})
    params = (store.segments_dir(project.id) / f"{storyboard.shots[0].id}.params").read_text()
    assert "native=on" in params
    srt = (project_dir / report.subtitles_path).read_text(encoding="utf-8")
    assert NARRATION.rstrip("。") in srt
