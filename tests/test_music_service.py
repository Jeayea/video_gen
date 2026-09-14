"""配乐服务（`services/music_service.py`）的测试。

重点：三条引擎路径都要能走通、失败要能降级（绝不阻断出片）、
生成结果要能缓存复用、输入一变要重新生成。
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from tests.factories import small_project
from tests.fakes import FakeBgmLibrary
from videogen.config import Settings, get_settings
from videogen.models.audio import AudioClip, AudioManifest, BgmTrack
from videogen.models.music import MusicPlan, MusicRequest
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import ClipStatus, MediaKind, VideoClip, VideoManifest
from videogen.services import music_service
from videogen.utils import music as music_utils
from videogen.utils.file_store import FileStore


def make_storyboard(shots: int = 3, duration: int = 4, dialogue: str = "你别走") -> Storyboard:
    return Storyboard(
        shots=[
            Shot(
                id=f"scene_1_shot_{index}",
                scene_id="scene_1",
                shot_no=index,
                duration_sec=duration,
                camera="中景 · 固定",
                visual_description="林晚站在会议室中央",
                video_prompt="medium shot of a woman in an office",
                mood="紧张",
                dialogue_text=dialogue,
                dialogue_speaker="char_1",
            )
            for index in range(1, shots + 1)
        ]
    )


def make_audio(storyboard: Storyboard, duration: float = 1.5) -> AudioManifest:
    manifest = AudioManifest(provider="mock")
    for shot in storyboard.shots:
        manifest.upsert(
            AudioClip(
                id=f"{shot.id}_dialogue",
                shot_id=shot.id,
                scene_id=shot.scene_id,
                speaker="char_1",
                text="你别走",
                status=ClipStatus.READY,
                media_kind=MediaKind.AUDIO,
                path=f"audio/{shot.id}.mp3",
                duration_sec=duration,
            )
        )
    return manifest


# ── 引擎可用性 ─────────────────────────────────────────


def test_engine_problem_cli_requires_template(settings):
    settings.bgm_cli_cmd = ""
    assert "BGM_CLI_CMD" in music_service.engine_problem(settings, "cli")

    settings.bgm_cli_cmd = "musicgen --out {out}"
    assert "{prompt}" in music_service.engine_problem(settings, "cli")

    settings.bgm_cli_cmd = "musicgen --out {out} --prompt {prompt}"
    assert music_service.engine_problem(settings, "cli") == ""


def test_engine_problem_library_never_blocks(settings):
    assert music_service.engine_problem(settings, "library") == ""
    assert music_service.engine_problem(settings, "none") == ""


def test_engine_problem_generate_needs_ffmpeg(settings, monkeypatch):
    monkeypatch.setattr(music_service, "ffmpeg_available", lambda: False)
    assert "ffmpeg" in music_service.engine_problem(settings, "generate")
    assert "ffmpeg" in music_service.engine_problem(settings, "auto")
    monkeypatch.setattr(music_service, "ffmpeg_available", lambda: True)
    assert music_service.engine_problem(settings, "generate") == ""


def test_command_for_fills_placeholders(settings):
    settings.bgm_cli_cmd = (
        "python synth.py --seconds {duration} --text \"{prompt}\" --out {out}"
    )
    settings.bgm_cli_args = "--seed 7"
    command = music_service.command_for(
        settings, out=Path("/tmp/bgm_raw"), duration=12.5, prompt="calm piano, no vocals"
    )
    assert command[0] == "python"
    assert "/tmp/bgm_raw" in command
    assert "12.50" in command
    assert "calm piano, no vocals" in command
    assert command[-2:] == ["--seed", "7"]


def test_command_for_rejects_bad_template(settings):
    settings.bgm_cli_cmd = "musicgen"
    with pytest.raises(ValueError, match=r"placeholder|占位符"):
        music_service.command_for(
            settings, out=Path("/tmp/x"), duration=5, prompt="p"
        )


# ── 提示词 ─────────────────────────────────────────────


def test_music_prompt_describes_timeline():
    storyboard = make_storyboard(shots=3, duration=4)
    plan = music_utils.build_plan(
        shots=storyboard.shots, duration_sec=12, seed=1, moods=["紧张"]
    )
    prompt = music_service.music_prompt(plan)
    assert "no vocals" in prompt
    assert f"{plan.bpm} bpm" in prompt
    assert plan.key_name in prompt
    assert "arrangement timeline" in prompt
    assert "0:00" in prompt


# ── 选曲（library）────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_music_library_picks_track(settings, tmp_path):
    storyboard = make_storyboard()
    project = small_project(FileStore(tmp_path / "projects"))
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        output_dir=tmp_path,
        settings=settings,
        library=FakeBgmLibrary(),
        request=MusicRequest(engine="library"),
    )
    assert outcome.ok
    assert outcome.engine == "library"
    assert outcome.track is not None
    assert outcome.track.id == "fake_bgm"
    assert outcome.track.engine == "library"


@pytest.mark.asyncio
async def test_resolve_music_library_without_tracks_is_not_fatal(settings, tmp_path):
    storyboard = make_storyboard()
    project = small_project(FileStore(tmp_path / "projects"))
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        output_dir=tmp_path,
        settings=settings,
        library=None,
        request=MusicRequest(engine="library"),
    )
    # 「曲库为空」不是失败：只是这次没有音乐（合成时会清掉上一次的曲目）
    assert outcome.ok
    assert outcome.track is None
    assert "曲库" in outcome.note


@pytest.mark.asyncio
async def test_resolve_music_none_engine(settings, tmp_path):
    storyboard = make_storyboard()
    project = small_project(FileStore(tmp_path / "projects"))
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        output_dir=tmp_path,
        settings=settings,
        request=MusicRequest(engine="none"),
    )
    assert outcome.ok
    assert outcome.track is None
    assert "关闭" in outcome.note


@pytest.mark.asyncio
async def test_disabled_switch_beats_project_engine(settings, tmp_path):
    """项目里存着 music_engine=generate 时，「本次关掉配乐」仍然要真的关掉。"""
    settings.bgm_enabled = False
    storyboard = make_storyboard()
    project = small_project(FileStore(tmp_path / "projects"))
    project.music_engine = "generate"
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        output_dir=tmp_path,
        settings=settings,
        request=MusicRequest(),
    )
    assert outcome.track is None
    assert "BGM_ENABLED" in outcome.note

    # 但请求里显式指定引擎时，它说了算（允许「全局关掉、这次我偏要」）
    forced = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        output_dir=tmp_path,
        settings=settings,
        request=MusicRequest(engine="none"),
    )
    assert forced.track is None


@pytest.mark.asyncio
async def test_resolve_music_respects_disabled_switch(settings, tmp_path):
    settings.bgm_enabled = False
    storyboard = make_storyboard()
    project = small_project(FileStore(tmp_path / "projects"))
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        output_dir=tmp_path,
        settings=settings,
        library=FakeBgmLibrary(),
    )
    assert outcome.track is None
    assert "BGM_ENABLED" in outcome.note


# ── 本地作曲（generate）──────────────────────────────


@pytest.fixture
def offline_post(monkeypatch):
    """把「混响 + 响度」那一趟 ffmpeg 换成纯 Python 截断，让生成路径测试稳定又快。

    真实那趟会把音轨裁到片子长度，这里用 wave 模块做同样的事 ——
    「曲目长度 = 片子长度」这个契约仍然被测到。
    """

    async def fake_post(dry: Path, ir_path: Path, output: Path, *, reverb: float, duration: float):
        with wave.open(str(dry), "rb") as source:
            params = source.getparams()
            frames = source.readframes(source.getnframes())
        keep = int(duration * params.framerate) * params.nchannels * params.sampwidth
        with wave.open(str(output), "wb") as target:
            target.setparams(params)
            target.writeframes(frames[:keep])

    monkeypatch.setattr(music_service, "_post_process", fake_post)
    return fake_post


@pytest.mark.asyncio
async def test_generate_local_writes_plan_and_track(settings, tmp_path, offline_post):
    store = FileStore(tmp_path / "projects")
    project = small_project(store, shots=2, shot_duration=3)
    storyboard = store.load_storyboard(project.id)
    audio = make_audio(storyboard)

    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        audio_manifest=audio,
        output_dir=store.output_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        settings=settings,
        request=MusicRequest(engine="generate", note="压抑一点"),
    )
    assert outcome.ok, outcome.note
    assert outcome.engine == "generate"
    assert outcome.track is not None
    assert outcome.plan is not None
    assert outcome.track.loop is False
    assert outcome.track.engine == "generate"
    assert outcome.track.plan_path.endswith("music_plan.json")
    assert outcome.audio is not None and outcome.audio.is_file()

    plan_path = store.output_dir(project.id) / "music" / "music_plan.json"
    assert plan_path.is_file()
    plan = MusicPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    assert plan.sections
    assert plan.stats.get("fingerprint")
    assert plan.duration_sec == pytest.approx(6.0)
    # 说话窗口来自配音的真实时长
    assert plan.speech_windows
    assert outcome.measure.get("duration_sec") == pytest.approx(6.0, abs=0.05)


@pytest.mark.asyncio
async def test_generate_local_reuses_cache_until_input_changes(settings, tmp_path, offline_post):
    store = FileStore(tmp_path / "projects")
    project = small_project(store, shots=2, shot_duration=3)
    storyboard = store.load_storyboard(project.id)
    kwargs = {
        "project": project,
        "storyboard": storyboard,
        "audio_manifest": make_audio(storyboard),
        "output_dir": store.output_dir(project.id),
        "frames_dir": store.frames_dir(project.id),
        "settings": settings,
    }
    first = await music_service.resolve_music(request=MusicRequest(engine="generate"), **kwargs)
    assert first.ok and not first.cached

    second = await music_service.resolve_music(request=MusicRequest(engine="generate"), **kwargs)
    assert second.ok and second.cached

    # 镜头时长变了（等价于改了分镜）→ 指纹变了 → 必须重新生成
    storyboard.shots[0].duration_sec = 5
    third = await music_service.resolve_music(request=MusicRequest(engine="generate"), **kwargs)
    assert third.ok and not third.cached
    assert third.plan is not None
    assert third.plan.duration_sec == pytest.approx(8.0)

    # 显式 force 也要重新生成
    fourth = await music_service.resolve_music(
        request=MusicRequest(engine="generate", force=True), **kwargs
    )
    assert fourth.ok and not fourth.cached


@pytest.mark.asyncio
async def test_generate_local_uses_speech_windows_from_audio(settings, tmp_path, offline_post):
    storyboard = make_storyboard(shots=2, duration=6)
    manifest = make_audio(storyboard, duration=2.0)
    windows = music_service._speech_windows(storyboard.shots, manifest)
    assert len(windows) == 2
    # 第一句：镜头 0 起点 + 0.4s 起音，长度 2.0 + 0.2 余量
    assert windows[0][0] == pytest.approx(0.4)
    assert windows[0][1] == pytest.approx(2.6)
    # 第二句在第二个镜头里（6s 起）
    assert windows[1][0] == pytest.approx(6.4)


def test_seed_for_is_stable(settings):
    project = type("P", (), {"id": "abc123"})()
    storyboard = make_storyboard(shots=2)
    assert music_service._seed_for(project, storyboard) == music_service._seed_for(
        project, storyboard
    )
    other = type("P", (), {"id": "abc124"})()
    assert music_service._seed_for(project, storyboard) != music_service._seed_for(
        other, storyboard
    )


@pytest.mark.asyncio
async def test_generate_local_uses_motion_cache(settings, tmp_path, offline_post):
    """有视频片段时，运动实测要参与作曲（并落盘缓存供配音文案复用）。"""
    store = FileStore(tmp_path / "projects")
    project = small_project(store, shots=1, shot_duration=2)
    storyboard = store.load_storyboard(project.id)
    clip_path = store.clips_dir(project.id) / "scene_1_shot_1.mp4"
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(b"not-a-real-video")
    manifest = VideoManifest(
        provider="mock",
        clips=[
            VideoClip(
                id="scene_1_shot_1",
                shot_id="scene_1_shot_1",
                status=ClipStatus.READY,
                media_kind=MediaKind.VIDEO,
                path=str(clip_path),
                duration_sec=2.0,
            )
        ],
    )
    calls: list[Path] = []

    async def fake_analyze(path, *, fps=6.0, **kwargs):
        calls.append(Path(path))
        return None  # 假片段抽不出帧：走「运动未知」分支

    monkeypatch_targets = {"analyze_clip": fake_analyze}
    import videogen.utils.motion as motion_utils

    original = motion_utils.analyze_clip
    motion_utils.analyze_clip = fake_analyze
    try:
        outcome = await music_service.resolve_music(
            project=project,
            storyboard=storyboard,
            video_manifest=manifest,
            audio_manifest=make_audio(storyboard, duration=1.0),
            output_dir=store.output_dir(project.id),
            frames_dir=store.frames_dir(project.id),
            settings=settings,
            request=MusicRequest(engine="generate"),
        )
    finally:
        motion_utils.analyze_clip = original
        del monkeypatch_targets
    assert outcome.ok
    assert calls == [clip_path]
    assert any("运动" in line for line in (outcome.plan.notes if outcome.plan else []))


@pytest.mark.asyncio
async def test_generate_local_falls_back_when_post_process_silences(
    settings, tmp_path, monkeypatch
):
    """后处理把音乐弄没了（真实事故：afir 压掉 80dB）时要退回干燥音轨，而不是交一条静音。"""

    async def broken_post(
        dry: Path, ir_path: Path, output: Path, *, reverb: float, duration: float
    ):
        with wave.open(str(output), "wb") as handle:  # 全零 = 静音
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(music_utils.SR)
            handle.writeframes(bytes(4 * int(duration * music_utils.SR)))

    monkeypatch.setattr(music_service, "_post_process", broken_post)
    store = FileStore(tmp_path / "projects")
    project = small_project(store, shots=2, shot_duration=3)
    storyboard = store.load_storyboard(project.id)
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        audio_manifest=make_audio(storyboard),
        output_dir=store.output_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        settings=settings,
        request=MusicRequest(engine="generate"),
    )
    assert outcome.ok, outcome.note
    assert outcome.measure.get("post_fallback") is True
    assert outcome.measure["peak"] > 0.02, "退回的干燥音轨必须真的有声音"
    assert outcome.measure["post_broken"]["rms_db"] < -60
    assert outcome.plan is not None
    assert any("干燥音轨" in line for line in outcome.plan.notes)


def test_post_process_broken_judgement():
    assert music_service._post_process_broken({}, {})
    assert music_service._post_process_broken({"rms_db": -30}, {"peak": 0.0, "rms_db": -120})
    assert music_service._post_process_broken({"rms_db": -30}, {"peak": 0.5, "rms_db": -60})
    # 正常结果放过
    assert not music_service._post_process_broken({"rms_db": -31}, {"peak": 0.53, "rms_db": -20.4})
    # 干燥音轨本来就很小声时不按「比干燥小 18dB」判
    assert not music_service._post_process_broken({"rms_db": -55}, {"peak": 0.3, "rms_db": -22})


# ── 外部模型（cli）────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_cli_runs_command_and_normalizes(
    settings, tmp_path, monkeypatch, offline_post
):
    settings.bgm_cli_cmd = "fake-music --out {out} --prompt {prompt} --seconds {seconds}"
    store = FileStore(tmp_path / "projects")
    project = small_project(store, shots=1, shot_duration=2)
    storyboard = store.load_storyboard(project.id)
    ran: list[list[str]] = []

    async def fake_run(command, *, timeout_sec):
        ran.append(command)
        # 外部模型会产出真音频（这里产出合法的 WAV 静音），后处理链路才走得通
        target = Path(command[command.index("--out") + 1])
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(music_utils.SR)
            handle.writeframes(bytes(4 * music_utils.SR * 2))
        return 0, ""

    monkeypatch.setattr(music_service, "_run", fake_run)
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        audio_manifest=make_audio(storyboard, duration=1.0),
        output_dir=store.output_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        settings=settings,
        request=MusicRequest(engine="cli"),
    )
    assert outcome.ok, outcome.note
    assert outcome.engine == "cli"
    assert ran and ran[0][0] == "fake-music"
    assert outcome.track is not None and outcome.track.engine == "cli"
    assert (store.output_dir(project.id) / "music" / "music_prompt.txt").is_file()
    assert outcome.plan is not None and outcome.plan.engine == "cli"


@pytest.mark.asyncio
async def test_generate_cli_failure_is_reported(settings, tmp_path, monkeypatch):
    settings.bgm_cli_cmd = "fake-music --out {out} --prompt {prompt}"

    async def fake_run(command, *, timeout_sec):
        return 3, "no CUDA device"

    monkeypatch.setattr(music_service, "_run", fake_run)
    store = FileStore(tmp_path / "projects")
    project = small_project(store, shots=1, shot_duration=2)
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=store.load_storyboard(project.id),
        output_dir=store.output_dir(project.id),
        settings=settings,
        request=MusicRequest(engine="cli"),
    )
    assert not outcome.ok
    assert "no CUDA device" in outcome.note


# ── 音量与闪避 ─────────────────────────────────────────


def test_track_for_prefers_request_then_project(settings, tmp_path):
    plan = music_utils.build_plan(duration_sec=8, seed=1, moods=["温暖"])
    audio = tmp_path / "bgm.wav"
    audio.write_bytes(b"x")
    project = small_project(FileStore(tmp_path / "p"))

    default = music_service._track_for(project, plan, audio, MusicRequest(), settings)
    assert default.gain_db is None  # 留空 → 合成本身用全局设置
    assert default.duck is None

    project.music_gain_db = -14.0
    project.music_duck = False
    project_track = music_service._track_for(project, plan, audio, MusicRequest(), settings)
    assert project_track.gain_db == -14.0
    assert project_track.duck is False

    request_track = music_service._track_for(
        project, plan, audio, MusicRequest(gain_db=-9.0, duck=True), settings
    )
    assert request_track.gain_db == -9.0
    assert request_track.duck is True


def test_reverb_for_uses_color_and_override(settings):
    plan = music_utils.build_plan(duration_sec=8, seed=1, moods=["悲伤"])
    assert music_service._reverb_for(plan, settings) == pytest.approx(
        music_utils.COLORS[plan.color].reverb
    )
    settings.music_reverb = 0.1
    assert music_service._reverb_for(plan, settings) == pytest.approx(0.1)


def test_measure_wav_reads_real_levels(tmp_path):
    path = tmp_path / "tone.wav"
    plan = music_utils.build_plan(duration_sec=2, seed=1, moods=["温暖"])
    music_utils.render_plan(plan, path)
    measure = music_service._measure_wav(path)
    assert measure["sample_rate"] == music_utils.SR
    assert measure["channels"] == 2
    assert measure["duration_sec"] == pytest.approx(2 + music_utils.TAIL_SEC, abs=0.05)
    assert 0.01 < measure["peak"] <= 1.0
    assert measure["rms_db"] < 0
    with wave.open(str(path), "rb") as handle:
        assert handle.getnframes() > 0


def test_measure_wav_on_garbage(tmp_path):
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not a wav at all")
    assert music_service._measure_wav(path) == {}


# ── LLM 配乐建议（可选，付费）──────────────────────────


class _BriefLLM:
    """返回一段可用建议的假 LLM。"""

    model = "fake"

    def __init__(self, payload=None, boom: bool = False):
        self.payload = payload
        self.boom = boom

    async def chat_json(
        self, messages, *, schema=None, temperature=0.5, max_tokens=8192, retries=2
    ):
        if self.boom:
            from videogen.providers.llm.base import LLMError

            raise LLMError("model unavailable")
        return {
            "color": "sad",
            "bpm": 72,
            "note": "全片走压抑路线，结尾收在长音上",
            "sections": [
                {"mood": "压抑", "energy": 1},
                {"mood": "悲伤", "energy": 2},
                {"mood": "释怀", "energy": 1},
            ],
        }


@pytest.mark.asyncio
async def test_llm_brief_feeds_plan(settings):
    storyboard = make_storyboard(shots=4, duration=6)
    brief, note = await music_service._llm_brief(
        _BriefLLM(), project=small_project(FileStore(Path("/tmp/unused-music-test"))),
        shots=storyboard.shots, moods=["压抑"], note="",
    )
    assert brief and brief[0]["color"] == "sad"
    assert len(brief) >= 3
    assert "LLM 配乐建议" in note

    plan = music_utils.build_plan(
        shots=storyboard.shots, duration_sec=24, seed=1, moods=["压抑"], section_hint=brief
    )
    assert plan.source == "llm"
    assert plan.color == "sad"
    assert len(plan.sections) >= 2


@pytest.mark.asyncio
async def test_llm_brief_failure_falls_back(settings):
    storyboard = make_storyboard(shots=2, duration=4)
    brief, note = await music_service._llm_brief(
        _BriefLLM(boom=True),
        project=small_project(FileStore(Path("/tmp/unused-music-test-2"))),
        shots=storyboard.shots,
        moods=["压抑"],
        note="",
    )
    assert brief is None
    assert "失败" in note


@pytest.mark.asyncio
async def test_generate_local_with_llm_brief_marks_source(settings, tmp_path, offline_post):
    store = FileStore(tmp_path / "projects")
    project = small_project(store, shots=2, shot_duration=3)
    storyboard = store.load_storyboard(project.id)
    outcome = await music_service.resolve_music(
        project=project,
        storyboard=storyboard,
        audio_manifest=make_audio(storyboard),
        output_dir=store.output_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        settings=settings,
        request=MusicRequest(engine="generate", llm_brief=True),
        llm=_BriefLLM(),
    )
    assert outcome.ok
    assert outcome.plan is not None
    assert outcome.plan.source == "llm"
    assert outcome.plan.color == "sad"
    assert any("LLM" in line for line in outcome.plan.notes)


# ── 缓存文件校验 ───────────────────────────────────────


def test_cache_valid_handles_missing_and_broken(tmp_path):
    plan_path = tmp_path / "music_plan.json"
    audio_path = tmp_path / "bgm.wav"
    assert not music_service._cache_valid(plan_path, audio_path, "abc")
    audio_path.write_bytes(b"x")
    plan_path.write_text("不是 JSON", encoding="utf-8")
    assert not music_service._cache_valid(plan_path, audio_path, "abc")
    plan_path.write_text('{"stats": {"fingerprint": "zzz"}}', encoding="utf-8")
    assert not music_service._cache_valid(plan_path, audio_path, "abc")
    plan_path.write_text('{"stats": {"fingerprint": "abc"}}', encoding="utf-8")
    assert music_service._cache_valid(plan_path, audio_path, "abc")


def test_find_output_accepts_extension_variants(tmp_path):
    raw = tmp_path / "bgm_cli_raw"
    assert music_service._find_output(raw, tmp_path) is None
    (tmp_path / "bgm_cli_raw.wav").write_bytes(b"audio")
    assert music_service._find_output(raw, tmp_path) == tmp_path / "bgm_cli_raw.wav"
    raw.write_bytes(b"audio")
    assert music_service._find_output(raw, tmp_path) == raw


# ── 设置归一化 ─────────────────────────────────────────


def test_bgm_engine_normalization():
    assert Settings(_env_file=None, bgm_engine="GENERATE").bgm_engine_mode == "generate"
    assert Settings(_env_file=None, bgm_engine="乱写").bgm_engine_mode == "library"
    assert Settings(_env_file=None, bgm_engine="none").bgm_on is False
    assert Settings(_env_file=None, bgm_enabled=False).bgm_on is False
    assert Settings(_env_file=None, music_reverb=-1).music_reverb_ratio == -1
    assert Settings(_env_file=None, music_reverb=3).music_reverb_ratio == 1.0
    assert get_settings().bgm_engine_mode in music_service.MUSIC_ENGINES


def test_default_bgm_track_is_backwards_compatible():
    """老数据里的 BgmTrack 没有 engine/loop 字段，也要能读出来。"""
    track = BgmTrack.model_validate({"id": "x", "path": "/tmp/x.mp3"})
    assert track.engine == "library"
    assert track.loop is True
    assert track.duck is None
