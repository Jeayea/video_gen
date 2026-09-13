"""逐帧口型相关的测量与适配层测试。

- `utils/lip_sync.py`：区间判读是纯函数；真测量用 ffmpeg 生成的片段
- `services/lipsync_service.py`：命令拼装、可用性检查、镜头筛选，
  以及用 **ffmpeg 当假引擎** 跑一遍完整流程（生成 → 接回原音轨 → 诊断）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videogen.config import get_settings
from videogen.models.storyboard import Shot
from videogen.services import lipsync_service
from videogen.utils import lip_sync
from videogen.utils.ffmpeg import ffmpeg_available, probe_streams, run_ffmpeg

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="环境里没有 ffmpeg，跳过真实媒体用例"
)

#: 用 ffmpeg 当假引擎：只保留画面、丢掉音轨 —— 正好考验「接回原音轨」那一步
FAKE_ENGINE = (
    "ffmpeg -y -hide_banner -loglevel error -i {video} -i {audio} -map 0:v:0 -an "
    "-c:v libx264 -preset ultrafast -pix_fmt yuv420p {out}"
)


# ── 区间判读（纯函数）─────────────────────────────────


def test_activity_span_finds_start_and_end():
    series = [0.0, 0.0, 0.9, 0.8, 0.85, 0.0, 0.0, 0.0]
    span = lip_sync.activity_span(series, fps=2.0)
    assert span is not None
    assert span.start == pytest.approx(1.0)
    # 最后一段活动窗口是下标 4，覆盖 [2.0, 2.5) → 结束时刻 2.5s
    assert span.end == pytest.approx(2.5)


def test_activity_span_does_not_stop_at_a_pause():
    """中间的自然停顿不能截断区间，否则「配音说完了嘴还在动」量不出来。"""
    series = [0.9, 0.9, 0.1, 0.1, 0.1, 0.9, 0.9]
    span = lip_sync.activity_span(series, fps=1.0)
    assert span is not None
    assert span.start == pytest.approx(0.0)
    assert span.end == pytest.approx(7.0)


def test_activity_span_needs_enough_run_to_start():
    """单帧噪声不算「开始动」。"""
    series = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    span = lip_sync.activity_span(series, fps=4.0, min_sec=0.5)
    # 只有第一帧超阈值、凑不满 2 个窗口 → 起点顺延到下一次连续超阈值处
    assert span is None or span.start >= 0.25


def test_activity_span_handles_empty_curve():
    assert lip_sync.activity_span([], fps=24.0) is None
    assert lip_sync.activity_span([0.0] * 10, fps=24.0) is None


def test_compare_reports_gaps_and_tail():
    compare = lip_sync.LipSyncCompare(
        dub=lip_sync.ActivitySpan(start=0.0, end=1.4, peak_time=0.0),
        before=lip_sync.ActivitySpan(start=1.2, end=4.6, peak_time=2.0),
        after=lip_sync.ActivitySpan(start=0.1, end=1.6, peak_time=0.4),
    )
    assert compare.before_onset_gap == pytest.approx(1.2)
    assert compare.after_onset_gap == pytest.approx(0.1)
    assert compare.before_tail == pytest.approx(3.2)
    assert compare.after_tail == pytest.approx(0.2)
    assert compare.improved
    assert "开口错位" in compare.text()


# ── 真媒体测量 ────────────────────────────────────────


async def _make_clip(path: Path, source: str, *, seconds: float = 1.0, audio: bool = False):
    args = [
        "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", source,
    ]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    args += ["-t", f"{seconds:.2f}", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(str(path))
    await run_ffmpeg(args, timeout_sec=60)


@requires_ffmpeg
async def test_mouth_activity_detects_motion(tmp_path: Path):
    moving = tmp_path / "moving.mp4"
    still = tmp_path / "still.mp4"
    await _make_clip(moving, "testsrc2=size=160x160:rate=12", seconds=1.0)
    await _make_clip(still, "color=c=gray:size=160x160:rate=12", seconds=1.0)
    activity = await lip_sync.mouth_activity(moving)
    assert activity and max(activity) > 0
    assert not any(await lip_sync.mouth_activity(still))


@requires_ffmpeg
async def test_rewrite_activity_ignores_encoding_noise(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    other = tmp_path / "other.mp4"
    await _make_clip(clip, "testsrc2=size=160x160:rate=12", seconds=1.0)
    await _make_clip(other, "testsrc2=size=160x160:rate=12", seconds=1.0)
    # 同一段内容重新编码：差异应该被 12/255 的门槛吃掉
    series = await lip_sync.rewrite_activity(other, clip)
    assert series
    assert max(series) < 0.004


@requires_ffmpeg
async def test_lip_sync_compare_end_to_end(tmp_path: Path):
    original = tmp_path / "original.mp4"
    synced = tmp_path / "synced.mp4"
    await _make_clip(original, "color=c=gray:size=160x160:rate=12", seconds=1.0)
    await _make_clip(synced, "testsrc2=size=160x160:rate=12", seconds=1.0)
    audio = tmp_path / "dub.wav"
    await run_ffmpeg(
        ["-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", str(audio)],
        timeout_sec=30,
    )
    compare = await lip_sync.lip_sync_compare(original, synced, audio)
    assert compare.dub is not None
    assert compare.before is None  # 全静止 → 没有「在动」的区间
    assert compare.after is not None
    assert compare.rewritten is not None
    assert "配音" in compare.text()
    assert compare.summary()["improved"] in {True, False}


# ── 服务层：命令 / 可用性 / 镜头筛选 ──────────────────


def test_shot_eligible_defaults_to_dialogue(monkeypatch):
    monkeypatch.setattr(get_settings(), "lipsync_scope", "dialogue")
    speaking = Shot(
        id="s1", scene_id="sc", shot_no=1, duration_sec=3,
        dialogue_text="你好", dialogue_speaker="char_1", character_in_frame=["char_1"],
    )
    narration = Shot(
        id="s2", scene_id="sc", shot_no=2, duration_sec=3, narration="旁白而已"
    )
    offscreen = Shot(
        id="s3", scene_id="sc", shot_no=3, duration_sec=3,
        dialogue_text="你好", dialogue_speaker="char_1", character_in_frame=["char_2"],
    )
    assert lipsync_service.shot_eligible(speaking, scope="dialogue")
    assert not lipsync_service.shot_eligible(narration, scope="dialogue")
    assert not lipsync_service.shot_eligible(offscreen, scope="dialogue")
    assert lipsync_service.shot_eligible(narration, scope="all")
    assert not lipsync_service.shot_eligible(None)


def test_command_for_cli_template(tmp_path: Path):
    settings = get_settings().model_copy(update={"lipsync_engine": "cli"})
    settings.lipsync_cmd = "my-engine --in {video} --speech {audio} --save {out}"
    command = lipsync_service.command_for(
        settings,
        video=tmp_path / "v.mp4",
        audio=tmp_path / "a.wav",
        out=tmp_path / "o.mp4",
        report=tmp_path / "o.report.json",
    )
    assert command == [
        "my-engine", "--in", str(tmp_path / "v.mp4"), "--speech",
        str(tmp_path / "a.wav"), "--save", str(tmp_path / "o.mp4"),
    ]


def test_command_for_cli_requires_placeholders():
    settings = get_settings().model_copy(
        update={"lipsync_engine": "cli", "lipsync_cmd": "my-engine {video}"}
    )
    with pytest.raises(ValueError, match="占位符"):
        lipsync_service.command_for(
            settings, video=Path("v.mp4"), audio=Path("a.wav"), out=Path("o.mp4"),
            report=Path("o.json"),
        )


def test_command_for_resolves_relative_paths(tmp_path: Path, monkeypatch):
    """`.env` 里的相对路径必须解析成绝对路径。

    踩过的坑：命令带着 cwd=项目目录执行时，`.venv-lipsync/bin/python` 被解析成
    「项目目录/.venv-lipsync/bin/python」，直接 FileNotFoundError（真实跑才暴露）。
    """
    settings = get_settings().model_copy(
        update={"lipsync_engine": "wav2lip-onnx", "lipsync_model_dir": "assets/lipsync"}
    )
    command = lipsync_service.command_for(
        settings,
        video=Path("clips/v.mp4"),
        audio=Path("audio/a.wav"),
        out=Path("out/o.mp4"),
        report=Path("out/o.json"),
    )
    for token in command:
        if token.startswith(("clips/", "audio/", "out/", "assets/", ".venv")):
            raise AssertionError(f"命令里还有相对路径：{token}")
    assert command[0].startswith("/")
    assert any(token.endswith("tools/lipsync/wav2lip_onnx.py") for token in command)


def test_absolute_path_does_not_follow_symlinks(tmp_path: Path, monkeypatch):
    """venv 里的 bin/python 是软链：解析软链会绕过整个环境（真跑时踩到过）。"""
    target = tmp_path / "system-python"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    link = tmp_path / "venv-python"
    link.symlink_to(target)
    monkeypatch.chdir(tmp_path)
    assert lipsync_service._absolute("venv-python") == link
    assert lipsync_service._absolute("venv-python") != target


def test_engine_problem_flags_missing_cli_and_missing_binary(tmp_path: Path):
    settings = get_settings().model_copy(
        update={"lipsync_engine": "cli", "lipsync_cmd": ""}
    )
    assert "LIPSYNC_CMD" in lipsync_service.engine_problem(settings)

    settings = get_settings().model_copy(
        update={
            "lipsync_engine": "wav2lip-onnx",
            "lipsync_python": str(tmp_path / "nope" / "python"),
        }
    )
    assert "install.sh" in lipsync_service.engine_problem(settings)


def test_engine_problem_is_empty_when_disabled():
    settings = get_settings().model_copy(update={"lipsync_engine": "none"})
    assert lipsync_service.engine_problem(settings) == ""


def test_settings_normalize_bad_engine():
    settings = get_settings().model_copy(update={"lipsync_engine": "LatentSync"})
    assert settings.lipsync == "none"
    assert not settings.lipsync_enabled
    assert get_settings().lipsync_scope_mode in {"dialogue", "all"}


# ── 服务层：真跑一遍（ffmpeg 当假引擎）────────────────


@requires_ffmpeg
async def test_sync_shot_muxes_original_audio_back(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    audio = tmp_path / "dub.wav"
    await _make_clip(video, "testsrc2=size=160x160:rate=12", seconds=1.0, audio=True)
    await run_ffmpeg(
        ["-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=660:duration=1", str(audio)],
        timeout_sec=30,
    )
    settings = get_settings().model_copy(
        update={
            "lipsync_engine": "cli",
            "lipsync_cmd": FAKE_ENGINE,
            "lipsync_measure": False,
        }
    )
    out = tmp_path / "synced.mp4"
    outcome = await lipsync_service.sync_shot(
        video, audio, out, shot_id="s1", settings=settings
    )
    assert outcome.ok, outcome.note
    assert out.is_file() and out.stat().st_size > 0
    # 引擎只给了画面，服务层必须把原片段音轨接回来
    assert "audio" in (await probe_streams(out) or set())
    assert outcome.engine == "cli"


@requires_ffmpeg
async def test_sync_shot_reports_engine_failure(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    audio = tmp_path / "dub.wav"
    await _make_clip(video, "color=c=gray:size=160x160:rate=12", seconds=0.5, audio=True)
    await run_ffmpeg(
        ["-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=660:duration=1", str(audio)],
        timeout_sec=30,
    )
    settings = get_settings().model_copy(
        update={
            "lipsync_engine": "cli",
            "lipsync_cmd": "/bin/sh -c 'exit 7' {video} {audio} {out}",
        }
    )
    out = tmp_path / "synced.mp4"
    outcome = await lipsync_service.sync_shot(
        video, audio, out, shot_id="s1", settings=settings
    )
    assert not outcome.ok
    assert "退出码 7" in outcome.note
    assert not out.exists()


@requires_ffmpeg
async def test_sync_shot_rejects_noop_engine(tmp_path: Path):
    """引擎输出和输入一样（口型静默失效）时必须判失败，而不是当成成功。"""
    video = tmp_path / "clip.mp4"
    audio = tmp_path / "dub.wav"
    await _make_clip(video, "color=c=gray:size=160x160:rate=12", seconds=0.5, audio=True)
    await run_ffmpeg(
        ["-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=660:duration=1", str(audio)],
        timeout_sec=30,
    )

    class FakeSettings:
        pass

    settings = get_settings().model_copy(
        update={
            "lipsync_engine": "cli",
            # 假装引擎跑了但没改画面，并自报 mean_patch_change=0
            "lipsync_cmd": (
                "ffmpeg -y -hide_banner -loglevel error -i {video} -i {audio} "
                "-map 0:v:0 -an -c:v libx264 -preset ultrafast "
                "-pix_fmt yuv420p {out}"
            ),
            "lipsync_measure": False,
        }
    )
    out = tmp_path / "synced.mp4"
    report = tmp_path / "synced.report.json"
    report.write_text('{"mean_patch_change": 0.0, "frames": 12}', encoding="utf-8")
    outcome = await lipsync_service.sync_shot(
        video, audio, out, shot_id="s1", settings=settings
    )
    assert not outcome.ok
    assert "口型可能没生效" in outcome.note
