"""环境音混音（把视频模型自带的音轨压低垫在 TTS 人声下面）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import wait_for_job
from tests.factories import small_project
from tests.fakes import FakeBgmLibrary, FakeLLM
from videogen.config import get_settings
from videogen.models.compose import ComposeSegment
from videogen.orchestrator.pipeline import Pipeline
from videogen.providers.tts.base import TTSRequest
from videogen.providers.tts.mock import MockTTSProvider
from videogen.providers.video.base import VideoRequest
from videogen.providers.video.mock import MockVideoProvider
from videogen.services import compose_service
from videogen.utils.ffmpeg import (
    ffmpeg_available,
    ffprobe_available,
    probe_streams,
    run_ffmpeg,
)

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="环境里没有 ffmpeg，跳过真实混音用例"
)


# ── 滤镜拼接（纯函数）────────────────────────────────


def test_filter_voice_only_has_no_mix():
    expr = compose_service._segment_filter(1080, 1920, 4.0, 24, voice=True, ambient=False)
    assert "[voice]" not in expr and "amix" not in expr
    assert "[1:a]" in expr and "[0:a]" not in expr


def test_filter_ambient_only_uses_clip_audio():
    expr = compose_service._segment_filter(
        1080, 1920, 4.0, 24, voice=False, ambient=True, ambient_gain_db=-18.0
    )
    assert "[0:a]" in expr and "[1:a]" not in expr
    assert "volume=-18.0dB" in expr
    assert "amix" not in expr


def test_filter_mixes_voice_and_ambient_without_normalize():
    expr = compose_service._segment_filter(
        1080, 1920, 4.0, 24, voice=True, ambient=True, ambient_gain_db=-20.0
    )
    assert "[voice][ambient]amix=inputs=2" in expr
    # normalize=0 很关键：否则 amix 会把人声音量也压低
    assert "normalize=0" in expr
    assert "volume=-20.0dB" in expr
    assert "atrim=duration=4.000" in expr


def test_filter_silence_when_no_voice_and_no_ambient():
    expr = compose_service._segment_filter(1080, 1920, 2.0, 24, voice=False, ambient=False)
    assert "[1:a]atrim=duration=2.000" in expr
    assert "[0:a]" not in expr


# ── 环境音可用性判定 ─────────────────────────────────


@pytest.mark.asyncio
async def test_ambient_requested_but_no_ffprobe_degrades(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("videogen.services.compose_service.probe_streams", _no_probe)
    warnings: list[str] = []
    mode = await compose_service._resolve_ambient_mode(
        [_segment(tmp_path, "clips/a.mp4")], tmp_path, "under", warnings
    )
    assert mode == "off"
    assert any("ffprobe" in w for w in warnings)


@pytest.mark.asyncio
async def test_ambient_requested_but_clips_have_no_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "videogen.services.compose_service.probe_streams", _streams({"video"})
    )
    warnings: list[str] = []
    mode = await compose_service._resolve_ambient_mode(
        [_segment(tmp_path, "clips/a.mp4")], tmp_path, "under", warnings
    )
    assert mode == "off"
    assert any("都没有自带音轨" in w for w in warnings)


@pytest.mark.asyncio
async def test_ambient_off_never_probes(tmp_path, monkeypatch):
    async def boom(path, **kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("off 模式不该去探测音轨")

    monkeypatch.setattr("videogen.services.compose_service.probe_streams", boom)
    warnings: list[str] = []
    assert (
        await compose_service._resolve_ambient_mode(
            [_segment(tmp_path, "clips/a.mp4")], tmp_path, "off", warnings
        )
        == "off"
    )
    assert warnings == []


@pytest.mark.asyncio
async def test_ambient_enabled_when_clips_have_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "videogen.services.compose_service.probe_streams", _streams({"video", "audio"})
    )
    warnings: list[str] = []
    mode = await compose_service._resolve_ambient_mode(
        [_segment(tmp_path, "clips/a.mp4")], tmp_path, "under", warnings
    )
    assert mode == "under"
    assert warnings == []


def _segment(tmp_path, video_path: str) -> ComposeSegment:
    return ComposeSegment(shot_id="s1", index=0, duration_sec=2.0, video_path=video_path)


async def _no_probe(path, **kwargs):
    return None


def _streams(kinds: set[str]):
    async def probe(path, **kwargs):
        return set(kinds)

    return probe


# ── 真实混音（需要 ffmpeg + ffprobe）──────────────────


async def make_clip_with_audio(path, *, seconds: int = 2, frequency: int = 220) -> None:
    """造一个带真实音轨的片段（模拟视频模型自带的音频）。"""
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=0x334455:s=540x960:d={seconds}:r=24",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-t", str(seconds), str(path),
        ],
        timeout_sec=60,
    )


@requires_ffmpeg
@pytest.mark.asyncio
async def test_render_segment_mixes_ambient_and_voice(tmp_path):
    if not ffprobe_available():
        pytest.skip("需要 ffprobe 才能验证混音结果")

    clip = tmp_path / "clip.mp4"
    await make_clip_with_audio(clip, seconds=2)
    voice = tmp_path / "voice.mp3"
    await MockTTSProvider().synthesize(
        TTSRequest(clip_id="c", text="这是配音", output_path=voice)
    )

    out = tmp_path / "seg.mp4"
    await compose_service.render_segment(
        ComposeSegment(shot_id="s1", index=0, duration_sec=3.0, video_path="clip.mp4"),
        video_path=clip,
        audio_path=voice,
        output=out,
        width=540,
        height=960,
        fps=24,
        crf=28,
        preset="ultrafast",
        ambient=True,
        ambient_gain_db=-20.0,
    )

    assert out.is_file() and out.stat().st_size > 0
    streams = await probe_streams(out)
    assert streams is not None and "video" in streams and "audio" in streams
    # 混音后仍然只有一个音频流（环境音不是外挂轨道）
    info = await _stream_count(out)
    assert info == (1, 1), "应该正好 1 路视频 + 1 路音频"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_render_segment_ambient_only_without_voice(tmp_path):
    clip = tmp_path / "clip.mp4"
    await make_clip_with_audio(clip, seconds=2)

    out = tmp_path / "seg.mp4"
    await compose_service.render_segment(
        ComposeSegment(shot_id="s1", index=0, duration_sec=2.0, video_path="clip.mp4"),
        video_path=clip,
        audio_path=None,  # 这个镜头没有台词：环境音就是唯一音轨
        output=out,
        width=540,
        height=960,
        fps=24,
        crf=28,
        preset="ultrafast",
        ambient=True,
        ambient_gain_db=-20.0,
    )
    assert out.is_file()
    assert await _stream_count(out) == (1, 1)


async def _stream_count(path) -> tuple[int, int]:
    from videogen.utils.ffmpeg import describe_media

    info = await describe_media(path)
    video = sum(1 for line in info.splitlines() if "Stream #" in line and "Video:" in line)
    audio = sum(1 for line in info.splitlines() if "Stream #" in line and "Audio:" in line)
    return video, audio


# ── 端到端：真实 provider 产物 + 环境音 ────────────────


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_end_to_end_with_ambient(store, monkeypatch):
    """真实 ffmpeg 全链路：mock 视频（带音轨）→ 配音 → 环境音混音 → 成片。"""
    if not ffprobe_available():
        pytest.skip("需要 ffprobe")

    class ClipWithAudio(MockVideoProvider):
        """在 mock 片段上加一条正弦音轨，模拟真实模型自带的环境音。"""

        async def generate(self, request: VideoRequest):  # type: ignore[override]
            clip = await super().generate(request)
            mixed = clip.path.with_name(clip.path.stem + "_amb.mp4")
            await run_ffmpeg(
                [
                    "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(clip.path),
                    "-f", "lavfi", "-i",
                    f"sine=frequency=180:sample_rate=44100:duration={request.duration_sec}",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
                    "-shortest", str(mixed),
                ],
                timeout_sec=60,
            )
            clip.path.unlink(missing_ok=True)
            return clip.model_copy(update={"path": mixed})

    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: ClipWithAudio(),
        tts_factory=lambda: MockTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    project = small_project(store, shots=2, shot_duration=1, dialogue="好。")
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)
    # 片段文件名要和 manifest 里记录的一致
    storyboard = store.load_storyboard(project.id)
    for shot in storyboard.shots:
        manifest_clip = store.load_video_manifest(project.id).get(shot.id)
        assert (store.ensure_project_dir(project.id) / manifest_clip.path).is_file()

    job = await pipeline.start_audio_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)

    job = await pipeline.start_compose(project.id, overrides={"compose_ambient_mode": "under"})
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert report.ambient_mode == "under"
    assert report.ambient_mode_effective == "under"
    assert report.ambient_gain_db == get_settings().compose_ambient_gain_db
    final = store.ensure_project_dir(project.id) / report.output_path
    assert final.is_file()
    assert await _stream_count(final) == (1, 1)
    assert store.load_project(project.id).status.value == "done"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_compose_ambient_degrades_when_clips_silent(store):
    """片段没有音轨时（mock 默认），请求 under 也要能正常出片并告警。"""
    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: MockVideoProvider(),
        tts_factory=lambda: MockTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    project = small_project(store, shots=1, shot_duration=1, dialogue="好。")
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)
    job = await pipeline.start_audio_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)

    job = await pipeline.start_compose(project.id, overrides={"compose_ambient_mode": "under"})
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error

    report = store.load_compose_report(project.id)
    assert report.ambient_mode == "under"
    assert report.ambient_mode_effective == "off", "探测不到音轨就该降级"
    assert any("自带音轨" in w for w in report.warnings)


def test_ambient_mode_setting_is_validated(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "compose_ambient_mode", "UNDER")
    assert settings.ambient_mode == "under"
    monkeypatch.setattr(settings, "compose_ambient_mode", "乱填")
    assert settings.ambient_mode == "off"


def test_compose_request_accepts_ambient():
    from videogen.main import ComposeRequest

    assert ComposeRequest(ambient_mode="under", ambient_gain_db=-18).ambient_mode == "under"
    with pytest.raises(ValidationError):
        ComposeRequest(ambient_mode="乱填")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_switching_ambient_mode_changes_the_output(store):
    """off → under 必须重渲染分段并真的改变成片（不能复用纯人声的旧分段）。"""
    if not ffprobe_available():
        pytest.skip("需要 ffprobe")

    class ClipWithAudio(MockVideoProvider):
        async def generate(self, request: VideoRequest):  # type: ignore[override]
            clip = await super().generate(request)
            mixed = clip.path.with_name(clip.path.stem + "_amb.mp4")
            await run_ffmpeg(
                [
                    "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(clip.path),
                    "-f", "lavfi", "-i",
                    f"sine=frequency=180:sample_rate=44100:duration={request.duration_sec}",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
                    "-shortest", str(mixed),
                ],
                timeout_sec=60,
            )
            clip.path.unlink(missing_ok=True)
            return clip.model_copy(update={"path": mixed})

    pipeline = Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: ClipWithAudio(),
        tts_factory=lambda: MockTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )
    project = small_project(store, shots=1, shot_duration=1, dialogue="好。")
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)
    job = await pipeline.start_audio_generation(project.id)
    await wait_for_job(pipeline, job.id, timeout=120)

    job = await pipeline.start_compose(project.id, overrides={"compose_ambient_mode": "off"})
    await wait_for_job(pipeline, job.id, timeout=180)
    silent = store.load_compose_report(project.id)
    silent_bytes = (store.ensure_project_dir(project.id) / silent.output_path).read_bytes()

    job = await pipeline.start_compose(project.id, overrides={"compose_ambient_mode": "under"})
    await wait_for_job(pipeline, job.id, timeout=180)
    mixed = store.load_compose_report(project.id)
    mixed_bytes = (store.ensure_project_dir(project.id) / mixed.output_path).read_bytes()

    assert silent.ambient_mode_effective == "off"
    assert mixed.ambient_mode_effective == "under"
    assert mixed_bytes != silent_bytes, "开了环境音，成片内容必须变（否则分段没重渲染）"
