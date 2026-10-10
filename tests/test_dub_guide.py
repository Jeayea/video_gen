"""原声引导配音：照视频原声的位置、长度和节奏重配。"""

from __future__ import annotations

import base64
import json
import wave
from pathlib import Path

import httpx
import pytest

from tests.fakes import FakeTTSProvider
from tests.test_audio import run_audio
from videogen.models.compose import ComposeSegment
from videogen.models.video import ClipStatus
from videogen.providers.tts.base import TTSRequest
from videogen.services import audio_service, dub_guide_service, subtitle_service
from videogen.services.dub_guide_service import Guide, atempo_chain, fit_to_span
from videogen.utils.ffmpeg import ffmpeg_available, probe_duration, run_ffmpeg

requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="没有 ffmpeg")


def test_atempo_chain_stays_within_ffmpeg_limits():
    assert atempo_chain(1.2) == "atempo=1.2000"
    assert atempo_chain(3.0) == "atempo=2.0,atempo=1.5000"
    assert atempo_chain(0.3) == "atempo=0.5,atempo=0.6000"


# ── 云端 TTS：带参考录音 ─────────────────────────────


def _wav(path: Path, seconds: float = 0.5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * int(24000 * seconds))
    return path


@pytest.mark.asyncio
async def test_cloud_tts_sends_reference_audio(tmp_path, monkeypatch):
    from tests.test_tts_openrouter import make_provider, ok_response
    from videogen.providers.tts import openrouter_audio

    async def fake_probe(path):
        return 1.5

    monkeypatch.setattr(openrouter_audio, "probe_duration", fake_probe)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return ok_response(transcript="窃书不能算偷！")

    reference = _wav(tmp_path / "guide.wav")
    await make_provider(handler).synthesize(
        TTSRequest(
            clip_id="s1",
            text="窃书不能算偷！",
            voice="onyx",
            reference_audio=reference,
            target_sec=1.8,
            output_path=tmp_path / "s1.wav",
        )
    )
    system, user = seen["json"]["messages"]
    assert "参考录音" in system["content"]
    assert "用你自己的音色" in system["content"]
    assert "1.8 秒" in system["content"]
    parts = user["content"]
    audio = next(part for part in parts if part["type"] == "input_audio")
    assert audio["input_audio"]["format"] == "wav"
    assert base64.b64decode(audio["input_audio"]["data"]) == reference.read_bytes()
    # 台词原文仍然原样在 user 消息里
    assert parts[-1] == {"type": "text", "text": "窃书不能算偷！"}


@pytest.mark.asyncio
async def test_cloud_tts_ignores_missing_reference(tmp_path, monkeypatch):
    from tests.test_tts_openrouter import make_provider, ok_response
    from videogen.providers.tts import openrouter_audio

    async def fake_probe(path):
        return 1.5

    monkeypatch.setattr(openrouter_audio, "probe_duration", fake_probe)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return ok_response(transcript="窃书不能算偷！")

    await make_provider(handler).synthesize(
        TTSRequest(
            clip_id="s1",
            text="窃书不能算偷！",
            reference_audio=tmp_path / "missing.wav",
            output_path=tmp_path / "s1.wav",
        )
    )
    system, user = seen["json"]["messages"]
    assert user["content"] == "窃书不能算偷！"
    assert "参考录音" not in system["content"]


# ── 配音阶段：位置 / 时长 / 续跑 ─────────────────────


@pytest.mark.asyncio
async def test_guided_dub_is_placed_at_native_onset(store, video_ready_project, monkeypatch):
    project = video_ready_project
    storyboard = store.load_storyboard(project.id)
    shot = next(s for s in storyboard.shots if s.dialogue_text)
    guide_file = _wav(store.audio_dir(project.id) / "guide" / f"{shot.id}.wav")
    guides = {shot.id: Guide(shot_id=shot.id, path=guide_file, start_sec=0.8, end_sec=2.4)}

    fitted: list[tuple[str, float]] = []

    async def fake_fit(audio, target_sec, *, max_ratio):
        fitted.append((Path(audio).name, target_sec))
        return Path(audio), target_sec, 1.1

    monkeypatch.setattr(dub_guide_service, "fit_to_span", fake_fit)
    tts = FakeTTSProvider()
    manifest = await run_audio(store, project, tts, guides=guides)

    clip = manifest.for_shot(shot.id)
    request = next(r for r in tts.requests if r.clip_id == shot.id)
    assert request.reference_audio == guide_file
    assert request.target_sec == pytest.approx(1.6)
    assert clip.offset_sec == pytest.approx(0.8)
    assert clip.duration_sec == pytest.approx(1.6)  # 已对齐到原声长度
    assert clip.guide_path.endswith(f"guide/{shot.id}.wav")
    # 别的镜头没有原声：照旧从 0 秒开始，不带参考录音
    others = [r for r in tts.requests if r.clip_id != shot.id]
    assert others and all(r.reference_audio is None for r in others)
    assert all(c.offset_sec == 0 for c in manifest.clips if c.shot_id != shot.id)

    # 同一份原声再跑一次：不重复花钱；原声变了（视频重做）才重配
    again = FakeTTSProvider()
    await run_audio(store, project, again, guides=guides)
    assert shot.id not in again.calls
    moved = {shot.id: Guide(shot_id=shot.id, path=guide_file, start_sec=1.0, end_sec=2.4)}
    third = FakeTTSProvider()
    await run_audio(store, project, third, guides=moved)
    assert third.calls == [shot.id]


def test_storyboard_makes_room_for_the_offset(store):
    from videogen.models.audio import AudioClip, AudioManifest
    from videogen.models.storyboard import Shot, Storyboard

    storyboard = Storyboard(
        shots=[Shot(id="s1", scene_id="scene_1", shot_no=1, duration_sec=3)]
    )
    manifest = AudioManifest(
        clips=[
            AudioClip(
                id="s1",
                shot_id="s1",
                duration_sec=2.0,
                offset_sec=1.2,
                guide_path="audio/guide/s1.wav",
                status=ClipStatus.READY,
            )
        ]
    )
    audio_service.align_storyboard(storyboard, manifest)
    assert storyboard.shots[0].duration_sec == 4  # 1.2 + 2.0 = 3.2 秒，3 秒的镜头装不下

    # 落在原声区间里（只越过镜头末尾一点点）：不为它多拉一秒冻结帧
    storyboard.shots[0].duration_sec = 3
    manifest.clips[0].offset_sec = 0.62
    manifest.clips[0].duration_sec = 2.39
    audio_service.align_storyboard(storyboard, manifest)
    assert storyboard.shots[0].duration_sec == 3


def test_subtitle_starts_when_the_line_starts():
    from videogen.models.audio import AudioClip, AudioManifest

    segments = [
        ComposeSegment(shot_id="s1", duration_sec=3.0, text="甲：你好"),
        ComposeSegment(
            shot_id="s2", duration_sec=4.0, text="乙：窃书不能算偷", voice_offset_sec=1.5
        ),
    ]
    audio = AudioManifest(
        clips=[
            AudioClip(id="s1", shot_id="s1", duration_sec=1.0, status=ClipStatus.READY),
            AudioClip(
                id="s2", shot_id="s2", duration_sec=1.5, offset_sec=1.5, status=ClipStatus.READY
            ),
        ]
    )
    srt = subtitle_service.build_srt(segments, audio)
    assert "00:00:00,000 --> 00:00:01,350" in srt
    assert "00:00:04,500 --> 00:00:06,350" in srt


# ── 真 ffmpeg：从原声里找台词、变速对齐 ──────────────


async def _tone_video(path: Path) -> Path:
    """1 秒静音 → 1.5 秒「说话」（正弦波）→ 0.5 秒静音，配一个纯色画面。"""
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=gray:s=64x64:d=3",
            "-f", "lavfi",
            "-i", "aevalsrc='if(between(t,1,2.5),0.5*sin(2*PI*220*t),0)':s=24000:d=3",
            "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path),
        ]
    )
    return path


@requires_ffmpeg
@pytest.mark.asyncio
async def test_prepare_guides_finds_the_line_in_native_audio(store, video_ready_project):
    project = video_ready_project
    project_dir = store.ensure_project_dir(project.id)
    storyboard = store.load_storyboard(project.id)
    video = store.load_video_manifest(project.id)
    shot = next(s for s in storyboard.shots if s.dialogue_text)
    clip = video.get(shot.id)
    clip.path = f"clips/{shot.id}.mp4"
    clip.status = ClipStatus.READY
    await _tone_video(project_dir / clip.path)
    for other in video.clips:
        if other.shot_id != shot.id:
            other.status = ClipStatus.PENDING  # 只留这一个带音轨的片段

    guides = await dub_guide_service.prepare_guides(
        storyboard, video, project_dir, store.audio_dir(project.id) / "guide"
    )
    assert list(guides) == [shot.id]
    guide = guides[shot.id]
    assert guide.start_sec == pytest.approx(1.0, abs=0.1)
    assert guide.end_sec == pytest.approx(2.5, abs=0.15)
    assert await probe_duration(guide.path) == pytest.approx(guide.span_sec, abs=0.1)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_fit_to_span_stretches_within_limits(tmp_path):
    source = tmp_path / "line.wav"
    await run_ffmpeg(
        ["-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=f=300:d=2", "-ar", "24000", str(source)]
    )
    path, seconds, factor = await fit_to_span(source, 1.7)
    assert path.name == "line.fit.wav"
    assert seconds == pytest.approx(1.7, abs=0.05)
    assert factor == pytest.approx(2 / 1.7, abs=0.01)

    # 差太多：只拉到上限（2 秒 → 最多压到 1.6 秒），不硬塞进 1 秒
    _, capped, factor = await fit_to_span(source, 1.0, max_ratio=1.25)
    assert factor == pytest.approx(1.25)
    assert capped == pytest.approx(1.6, abs=0.05)

    # 差得很少：不动
    same, _, factor = await fit_to_span(source, 1.97)
    assert same == source and factor == 1.0


@requires_ffmpeg
@pytest.mark.asyncio
async def test_fit_trims_silence_before_comparing(tmp_path):
    """云端 TTS 的尾巴上常有半秒静音：口径要和原声的「说话段」一致。"""
    source = tmp_path / "line.wav"
    await run_ffmpeg(
        ["-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "aevalsrc='if(lt(t,1.6),0.5*sin(2*PI*300*t),0)':s=24000:d=2.2", str(source)]
    )
    # 原声那边的说话段同样带着探测余量（≈1.65 秒）
    path, seconds, factor = await fit_to_span(source, 1.65)
    # 1.6 秒的说话 + 0.6 秒静音 → 只裁静音，不变速
    assert path.name == "line.fit.wav"
    assert factor == 1.0
    assert seconds == pytest.approx(1.67, abs=0.06)


@pytest.mark.asyncio
async def test_guided_request_drops_conflicting_pace_instructions(
    store, video_ready_project, monkeypatch
):
    project = video_ready_project
    storyboard = store.load_storyboard(project.id)
    shot = next(s for s in storyboard.shots if s.dialogue_text)
    guide_file = _wav(store.audio_dir(project.id) / "guide" / f"{shot.id}.wav")

    async def fake_fit(audio, target_sec, *, max_ratio):
        return Path(audio), target_sec, 1.0

    monkeypatch.setattr(dub_guide_service, "fit_to_span", fake_fit)
    tts = FakeTTSProvider()
    await run_audio(
        store,
        project,
        tts,
        guides={shot.id: Guide(shot_id=shot.id, path=guide_file, start_sec=0.5, end_sec=2.0)},
    )
    request = next(r for r in tts.requests if r.clip_id == shot.id)
    assert request.instruction == dub_guide_service.GUIDE_INSTRUCTION
