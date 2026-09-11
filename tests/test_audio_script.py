"""配音文案阶段（LLM 看着画面写/改台词）的测试。

覆盖三层：
1. 纯函数（念法解析 / 超长截断 / 目标镜头筛选）
2. service（写回分镜、断点续跑不重复花钱、失败降级）
3. pipeline + API（记账口径、状态流转、请求参数）
"""

from __future__ import annotations

import pytest

from tests.conftest import build_confirmed_project, wait_for_job
from tests.fakes import FakeBgmLibrary, FakeLLM, FakeTTSProvider, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.cost import CostStage
from videogen.models.project import ProjectStatus
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import ClipStatus, MediaKind, VideoClip, VideoManifest
from videogen.orchestrator.pipeline import Pipeline
from videogen.services import audio_script_service, cost_service
from videogen.utils.ffmpeg import ffmpeg_available, run_ffmpeg

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="环境里没有 ffmpeg，跳过真实抽帧用例"
)


def build_pipeline(store, llm, tts=None, video=None) -> Pipeline:
    return Pipeline(
        store,
        llm_factory=lambda: llm,
        video_factory=lambda: video or FakeVideoProvider(),
        tts_factory=lambda: tts or FakeTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )


async def video_ready(store, pipeline: Pipeline, llm: FakeLLM):
    """文本链路 + 假视频，返回 (project, storyboard)。"""
    project = await build_confirmed_project(store, pipeline)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)
    return store.load_project(project.id), store.load_storyboard(project.id)


# ── 纯函数 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "kind", "expected"),
    [
        ("-10%", "rate", "-10%"),
        ("+15%", "rate", "+15%"),
        ("10", "rate", "+10%"),
        ("慢 10%", "rate", "+10%"),
        ("-200%", "rate", "-50%"),  # 夹到下限
        ("300", "rate", "+100%"),  # 夹到上限
        ("-20Hz", "pitch", "-20Hz"),
        ("+80", "pitch", "+50Hz"),
        ("", "rate", ""),
        ("随便说说", "rate", ""),
        ("1e3", "rate", "+1%"),  # 只认第一个数字，异常写法不会爆炸
    ],
)
def test_coerce_delivery(raw, kind, expected):
    assert audio_script_service.coerce_delivery(raw, kind=kind) == expected


def test_coerce_delivery_keeps_default():
    assert audio_script_service.coerce_delivery("", kind="rate", default="+5%") == "+5%"


@pytest.mark.parametrize(
    ("text", "limit", "expected_prefix"),
    [
        ("短句", 10, "短句"),
        ("一二三四五六七八九十", 10, "一二三四五六七八九十"),
        ("今天必须把方案交出来，否则后果自负。", 10, "今天必须把方案交出来，"),
        ("没有标点的超长一句话要硬切掉多余部分", 8, "没有标点的超长一"),
    ],
)
def test_clamp_text(text, limit, expected_prefix):
    clamped, truncated = audio_script_service._clamp_text(text, limit)
    assert clamped.startswith(expected_prefix.rstrip("，"))
    assert len(clamped) <= max(8, limit)
    assert truncated is (len(text) > limit)


def _shot(shot_id: str, dialogue: str = "", narration: str = "", source: str = "") -> Shot:
    return Shot(
        id=shot_id,
        scene_id="scene_1",
        shot_no=1,
        dialogue_text=dialogue or None,
        narration=narration or None,
        voice_script_source=source,
    )


def test_script_targets_by_mode():
    storyboard = Storyboard(
        shots=[
            _shot("s1", dialogue="有台词"),
            _shot("s2", narration="有旁白"),
            _shot("s3"),
            _shot("s4", narration="已生成过", source="llm:narration"),
        ]
    )
    narration_groups, skipped = audio_script_service.script_targets(
        storyboard, "narration", force=False
    )
    assert [s.id for _, shots in narration_groups for s in shots] == ["s3"]
    assert skipped == 1  # s4 已经用 narration 模式写过

    polish_ids = [
        s.id
        for _, shots in audio_script_service.script_targets(storyboard, "polish", force=False)[0]
        for s in shots
    ]
    # s4 是 narration 模式写的，polish 是另一件事，所以仍要处理
    assert polish_ids == ["s1", "s2", "s4"]

    full_ids = [
        s.id
        for _, shots in audio_script_service.script_targets(storyboard, "full", force=False)[0]
        for s in shots
    ]
    assert full_ids == ["s1", "s2", "s3", "s4"]


def test_script_targets_force_includes_generated():
    storyboard = Storyboard(shots=[_shot("s1", narration="已生成", source="llm:full")])
    groups, skipped = audio_script_service.script_targets(storyboard, "full", force=True)
    assert skipped == 0
    assert len(groups) == 1


def test_normalize_mode_rejects_typos():
    assert audio_script_service.normalize_mode("FULL") == "full"
    assert audio_script_service.normalize_mode("fll") == "off"
    assert audio_script_service.normalize_mode("") == "off"


def test_estimate_is_cheap_but_nonzero():
    usd = audio_script_service.estimate_voice_script_usd(15, frames=15)
    assert 0 < usd < 0.05


# ── service：写回分镜 / 降级 / 续跑 ────────────────────


@pytest.mark.asyncio
async def test_narration_mode_writes_silent_shots(store, make_media_pipeline):
    llm = FakeLLM(voice_text="镜头里她攥紧了拳头")
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, storyboard = await video_ready(store, pipeline, llm)
    silent = [s.id for s in storyboard.shots if not s.dialogue_text and not s.narration]
    assert silent, "假分镜里应该有没台词的镜头"

    outcome = await audio_script_service.apply_voice_script(
        llm=llm,
        project=project,
        storyboard=storyboard,
        script=store.load_script(project.id),
        bible=store.load_bible(project.id),
        video=store.load_video_manifest(project.id),
        project_dir=store.ensure_project_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        mode="narration",
        use_vision=False,
    )

    assert outcome.mode == "narration"
    assert sorted(outcome.applied) == sorted(silent)
    for shot_id in silent:
        shot = next(s for s in storyboard.shots if s.id == shot_id)
        assert shot.narration and "攥紧了拳头" in shot.narration
        assert shot.voice_script_source == "llm:narration"
    # 有台词的镜头一个字都不许动
    for shot in storyboard.shots:
        if shot.id not in silent:
            assert shot.voice_script_source == ""
            assert shot.dialogue_text == "（咬牙）这个项目是我做的。" or shot.dialogue_text


@pytest.mark.asyncio
async def test_polish_keeps_speaker_and_records_original(store, make_media_pipeline):
    llm = FakeLLM(voice_text="这个方案从头到尾都是我做的")
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, storyboard = await video_ready(store, pipeline, llm)

    await audio_script_service.apply_voice_script(
        llm=llm,
        project=project,
        storyboard=storyboard,
        script=None,
        bible=None,
        video=None,
        project_dir=store.ensure_project_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        mode="polish",
        use_vision=False,
    )

    shot = next(s for s in storyboard.shots if s.dialogue_speaker)
    assert "从头到尾" in (shot.dialogue_text or "")
    assert shot.dialogue_speaker == "char_1"  # 说话人不变
    assert shot.voice_script_source == "llm:polish"
    assert shot.voice_script_original == "这个项目是我做的。"  # 改写前原文留档


@pytest.mark.asyncio
async def test_empty_text_means_keep_original(store, make_media_pipeline):
    llm = FakeLLM()
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, storyboard = await video_ready(store, pipeline, llm)
    shot = next(s for s in storyboard.shots if s.dialogue_text)
    original = shot.dialogue_text
    llm.voice_empty_shots = {s.id for s in storyboard.shots}

    outcome = await audio_script_service.apply_voice_script(
        llm=llm,
        project=project,
        storyboard=storyboard,
        script=None,
        bible=None,
        video=None,
        project_dir=store.ensure_project_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        mode="polish",
        use_vision=False,
    )

    assert outcome.applied == []
    assert shot.dialogue_text == original
    assert shot.voice_script_source == ""


@pytest.mark.asyncio
async def test_delivery_direction_is_recorded(store, make_media_pipeline):
    llm = FakeLLM(voice_rate="-12%")
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, storyboard = await video_ready(store, pipeline, llm)

    await audio_script_service.apply_voice_script(
        llm=llm,
        project=project,
        storyboard=storyboard,
        script=None,
        bible=None,
        video=None,
        project_dir=store.ensure_project_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        mode="full",
        use_vision=False,
    )

    direction = next(s for s in storyboard.shots if s.voice_direction).voice_direction
    assert direction is not None
    assert direction.rate == "-12%"
    assert direction.note


@pytest.mark.asyncio
async def test_llm_failure_never_blocks_audio(store, make_media_pipeline):
    llm = FakeLLM(fail_at="audio_script")
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, storyboard = await video_ready(store, pipeline, llm)
    before = {s.id: (s.dialogue_text, s.narration) for s in storyboard.shots}

    job = await pipeline.start_audio_generation(
        project.id, script_mode="narration", use_vision=False
    )
    finished = await wait_for_job(pipeline, job.id)

    assert finished.status.value == "succeeded"
    assert store.load_project(project.id).status == ProjectStatus.AUDIO_READY
    manifest = store.load_audio_manifest(project.id)
    assert manifest and manifest.ready_clips
    assert any("配音文案" in w for w in manifest.warnings)
    after = {s.id: (s.dialogue_text, s.narration) for s in store.load_storyboard(project.id).shots}
    assert after == before  # 文案没写成，但分镜没被改坏


@pytest.mark.asyncio
async def test_audio_stage_records_llm_tokens(store, make_media_pipeline):
    llm = FakeLLM()
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, _ = await video_ready(store, pipeline, llm)

    job = await pipeline.start_audio_generation(
        project.id, script_mode="narration", use_vision=False
    )
    await wait_for_job(pipeline, job.id)

    report = cost_service.load_report(store, project.id)
    audio_entries = [e for e in report.entries if e.stage == CostStage.AUDIO]
    llm_entries = [e for e in audio_entries if e.unit_kind == "token"]
    assert llm_entries, "配音文案的 token 必须记进 AUDIO 阶段"
    assert sum(e.total_tokens for e in llm_entries) > 0
    manifest = store.load_audio_manifest(project.id)
    assert manifest.script_mode == "narration"
    assert manifest.script_lines > 0
    assert manifest.script_model


@pytest.mark.asyncio
async def test_rerun_does_not_call_llm_again(store, make_media_pipeline):
    llm = FakeLLM()
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, _ = await video_ready(store, pipeline, llm)

    first_round = 0
    for index in range(2):
        job = await pipeline.start_audio_generation(
            project.id, script_mode="narration", use_vision=False
        )
        await wait_for_job(pipeline, job.id)
        if index == 0:
            first_round = len(llm.voice_modes)

    # 假分镜有 5 场，所以一轮是 5 次调用；第二轮一次都不该有
    first_round = len(llm.voice_modes)
    assert first_round > 0
    assert len(llm.voice_modes) == first_round, "第二轮不该再调 LLM 花钱"


@pytest.mark.asyncio
async def test_force_reruns_script(store, make_media_pipeline):
    llm = FakeLLM()
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, _ = await video_ready(store, pipeline, llm)

    for _ in range(2):
        job = await pipeline.start_audio_generation(
            project.id, script_mode="narration", use_vision=False, force_script=True
        )
        await wait_for_job(pipeline, job.id)

    assert len(llm.voice_modes) == 10  # 5 场 × 2 轮（强制重跑才会再花一次）


@pytest.mark.asyncio
async def test_rewritten_text_triggers_resynthesis(store, make_media_pipeline):
    """LLM 改了文案就必须重新合成，不能留着旧音频。"""
    llm = FakeLLM(voice_text="第一版文案")
    tts = FakeTTSProvider()
    pipeline = make_media_pipeline(tts, llm=llm)
    project, _ = await video_ready(store, pipeline, llm)

    job = await pipeline.start_audio_generation(
        project.id, script_mode="narration", use_vision=False
    )
    await wait_for_job(pipeline, job.id)
    first_calls = dict.fromkeys(tts.calls)
    clip = next(
        c for c in store.load_audio_manifest(project.id).clips if c.script_source == "llm:narration"
    )
    assert "第一版文案" in clip.text

    llm.voice_text = "第二版文案"
    job = await pipeline.start_audio_generation(
        project.id, script_mode="narration", use_vision=False, force_script=True
    )
    await wait_for_job(pipeline, job.id)

    updated = store.load_audio_manifest(project.id).get(clip.id)
    assert updated and "第二版文案" in updated.text
    assert tts.calls.count(clip.id) == 2, "文案变了必须重新合成这条"
    assert clip.id in first_calls


# ── 真实抽帧（需要 ffmpeg）────────────────────────────


@requires_ffmpeg
@pytest.mark.asyncio
async def test_extract_frames_and_data_url(store, tmp_path):
    clip = tmp_path / "clip.mp4"
    await run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x224466:s=540x960:d=3:r=24",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(clip),
        ],
        timeout_sec=60,
    )
    out_dir = tmp_path / "frames"
    frames = await audio_script_service.extract_frames(
        clip, out_dir, shot_id="s1", count=2, width=320
    )
    assert len(frames) == 2
    assert all(f.is_file() and f.stat().st_size > 0 for f in frames)

    url = audio_script_service.frame_data_url(frames[0])
    assert url.startswith("data:image/jpeg;base64,")

    # 第二次直接复用缓存，不重新抽
    again = await audio_script_service.extract_frames(
        clip, out_dir, shot_id="s1", count=2, width=320
    )
    assert again == frames


@requires_ffmpeg
@pytest.mark.asyncio
async def test_vision_sends_frames_to_model(store, make_media_pipeline):
    llm = FakeLLM()
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, storyboard = await video_ready(store, pipeline, llm)

    # 把假片段换成真视频（抽帧需要真视频流）
    clips_dir = store.clips_dir(project.id)
    manifest = VideoManifest(provider="test", model="test")
    for shot in storyboard.shots:
        path = clips_dir / f"{shot.id}.mp4"
        await run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x3355aa:s=540x960:d={max(1, shot.duration_sec)}:r=24",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(path),
            ],
            timeout_sec=60,
        )
        manifest.upsert(
            VideoClip(
                shot_id=shot.id,
                scene_id=shot.scene_id,
                status=ClipStatus.READY,
                media_kind=MediaKind.VIDEO,
                path=str(path.relative_to(store.ensure_project_dir(project.id))),
                duration_sec=shot.duration_sec,
            )
        )
    store.save_video_manifest(project.id, manifest)
    storyboard = store.load_storyboard(project.id)

    outcome = await audio_script_service.apply_voice_script(
        llm=llm,
        project=project,
        storyboard=storyboard,
        script=None,
        bible=None,
        video=manifest,
        project_dir=store.ensure_project_dir(project.id),
        frames_dir=store.frames_dir(project.id),
        mode="narration",
        use_vision=True,
    )

    assert outcome.frames == outcome.touched, "每个候选镜头都该带一张画面"
    assert outcome.frames > 0
    assert llm.image_count == outcome.frames
    assert llm.vision_calls == 5  # 每场一次调用（假分镜 5 场）
    assert list(store.frames_dir(project.id).glob("*.jpg"))


# ── 设置与 API ────────────────────────────────────────


@pytest.mark.asyncio
async def test_mode_from_settings_is_used(store, make_media_pipeline, monkeypatch):
    llm = FakeLLM()
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, _ = await video_ready(store, pipeline, llm)
    settings = get_settings()
    monkeypatch.setattr(settings, "audio_llm_script", "narration")
    monkeypatch.setattr(settings, "audio_llm_vision", False)

    job = await pipeline.start_audio_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert store.load_audio_manifest(project.id).script_mode == "narration"
    assert set(llm.voice_modes) == {"narration"}


@pytest.mark.asyncio
async def test_off_mode_never_calls_llm(store, make_media_pipeline):
    llm = FakeLLM()
    pipeline = make_media_pipeline(FakeTTSProvider(), llm=llm)
    project, _ = await video_ready(store, pipeline, llm)

    job = await pipeline.start_audio_generation(project.id, script_mode="off")
    await wait_for_job(pipeline, job.id)

    assert llm.voice_modes == []
    manifest = store.load_audio_manifest(project.id)
    assert manifest.script_mode == "off"
    assert manifest.script_lines == 0
