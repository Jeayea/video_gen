from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import wait_for_job
from tests.fakes import (
    AlwaysFailTTSProvider,
    FakeBgmLibrary,
    FakeLLM,
    FakeTTSProvider,
    FakeVideoProvider,
)
from videogen.models.audio import AudioKind, AudioManifest, BgmTrack
from videogen.models.project import ProjectStatus
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import ClipStatus, MediaKind
from videogen.orchestrator.pipeline import Pipeline, PipelineError
from videogen.providers.bgm.base import match_score
from videogen.providers.bgm.local import LocalBgmLibrary
from videogen.providers.tts.base import TTSProviderError, TTSRequest
from videogen.providers.tts.edge import EdgeTTSProvider
from videogen.services import audio_service


def build_pipeline(store, llm, tts, video=None, bgm=None) -> Pipeline:
    return Pipeline(
        store,
        llm_factory=lambda: llm,
        video_factory=lambda: video or FakeVideoProvider(),
        tts_factory=lambda: tts,
        bgm_factory=lambda: bgm or FakeBgmLibrary(),
    )


async def run_audio(store, project, provider, **kwargs) -> AudioManifest:
    """直接驱动 audio_service（并像 pipeline 一样落盘，便于测续跑）。"""
    storyboard = store.load_storyboard(project.id)
    if kwargs.get("existing", "unset") == "unset":
        kwargs["existing"] = store.load_audio_manifest(project.id)
    manifest = await audio_service.synthesize_all(
        project,
        storyboard,
        store.load_bible(project.id),
        provider,
        store.audio_dir(project.id),
        store.ensure_project_dir(project.id),
        retry_backoff_sec=0,
        **kwargs,
    )
    store.save_audio_manifest(project.id, manifest)
    return manifest


def dialog_shots(store, project) -> list[str]:
    """分镜里真正有台词/旁白的镜头。"""
    storyboard = store.load_storyboard(project.id)
    ids = []
    for shot in storyboard.shots:
        if shot.dialogue_text or shot.narration:
            ids.append(shot.id)
    return ids


# ── service 层 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesizes_one_clip_per_line(store, video_ready_project):
    provider = FakeTTSProvider()
    manifest = await run_audio(store, video_ready_project, provider)
    expected = dialog_shots(store, video_ready_project)

    assert len(manifest.clips) == len(expected)
    assert len(manifest.ready_clips) == len(expected)
    assert not manifest.failed_clips
    assert manifest.is_usable
    assert manifest.provider == "fake"

    for clip in manifest.clips:
        assert clip.path and not clip.path.startswith("/")
        assert (store.ensure_project_dir(video_ready_project.id) / clip.path).exists()
        assert clip.status == ClipStatus.READY
        assert clip.media_kind == MediaKind.AUDIO
        assert clip.duration_sec > 0
        assert clip.voice


def test_voice_map_is_gendered_and_stable(store, video_ready_project):
    bible = store.load_bible(video_ready_project.id)
    storyboard = Storyboard(
        shots=[
            Shot(
                id="s1",
                scene_id="scene_1",
                shot_no=1,
                dialogue_text="这个项目是我做的。",
                dialogue_speaker="char_1",
            ),
            Shot(
                id="s2",
                scene_id="scene_1",
                shot_no=2,
                dialogue_text="你？别开玩笑了。",
                dialogue_speaker="char_2",
            ),
            Shot(
                id="s3",
                scene_id="scene_1",
                shot_no=3,
                narration="夜色渐深。",
            ),
        ]
    )
    voices = audio_service.build_voice_map(storyboard, bible)

    # 女角色必须是女声、男角色必须是男声，且两个角色不能撞音色
    from videogen.services import voice_catalog

    assert voice_catalog.voice_gender(voices["char_1"]) == "female"
    assert voice_catalog.voice_gender(voices["char_2"]) == "male"
    assert voices["char_1"] != voices["char_2"]
    assert voices["narrator"].endswith("YunjianNeural")  # 旁白单独一个

    # 已有分配必须原样保留（重跑不能换人声音）
    again = audio_service.build_voice_map(storyboard, bible, existing=voices)
    assert again == voices


@pytest.mark.asyncio
async def test_voice_map_survives_rerun(store, video_ready_project):
    provider = FakeTTSProvider()
    manifest = await run_audio(store, video_ready_project, provider)
    assert manifest.voice_map["char_1"].endswith("XiaoxiaoNeural")

    again = FakeTTSProvider()
    manifest2 = await run_audio(store, video_ready_project, again)
    assert manifest2.voice_map == manifest.voice_map
    assert again.calls == []  # 而且不会重复合成


@pytest.mark.asyncio
async def test_narration_clip_uses_narrator_voice(store, video_ready_project):
    storyboard = store.load_storyboard(video_ready_project.id)
    storyboard.shots[0].dialogue_text = None
    storyboard.shots[0].dialogue_speaker = None
    storyboard.shots[0].narration = "夜色渐深，城市依旧喧嚣。"
    store.save_storyboard(video_ready_project.id, storyboard)

    provider = FakeTTSProvider()
    manifest = await run_audio(store, video_ready_project, provider)

    clip = manifest.for_shot(storyboard.shots[0].id)
    assert clip.kind == AudioKind.NARRATION
    assert clip.speaker == "narrator"
    assert clip.voice == manifest.voice_map["narrator"]


@pytest.mark.asyncio
async def test_narration_skipped_when_dialogue_present(store, video_ready_project):
    storyboard = store.load_storyboard(video_ready_project.id)
    storyboard.shots[0].narration = "旁白不该被念出来"
    store.save_storyboard(video_ready_project.id, storyboard)

    manifest = await run_audio(store, video_ready_project, FakeTTSProvider())

    assert manifest.for_shot(storyboard.shots[0].id).kind == AudioKind.DIALOGUE
    assert any("旁白" in w for w in manifest.warnings)


@pytest.mark.asyncio
async def test_failure_is_isolated_to_one_clip(store, video_ready_project):
    target = dialog_shots(store, video_ready_project)[0]
    provider = FakeTTSProvider(fail_clips={target}, fail_times=99)

    manifest = await run_audio(store, video_ready_project, provider, max_retries=1)

    assert [c.id for c in manifest.failed_clips] == [target]
    assert manifest.failed_clips[0].attempts == 2
    assert "fake 配音失败" in manifest.failed_clips[0].error
    assert not manifest.is_usable
    assert not (store.audio_dir(video_ready_project.id) / f"{target}.mp3").exists()


@pytest.mark.asyncio
async def test_retry_then_success(store, video_ready_project):
    target = dialog_shots(store, video_ready_project)[0]
    provider = FakeTTSProvider(fail_clips={target}, fail_times=1)

    manifest = await run_audio(store, video_ready_project, provider, max_retries=2)

    assert manifest.get(target).status == ClipStatus.READY
    assert manifest.get(target).attempts == 2


@pytest.mark.asyncio
async def test_resume_only_regenerates_failed(store, video_ready_project):
    target = dialog_shots(store, video_ready_project)[0]
    failing = FakeTTSProvider(fail_clips={target}, fail_times=99)
    manifest = await run_audio(store, video_ready_project, failing, max_retries=0)
    assert len(manifest.failed_clips) == 1

    healthy = FakeTTSProvider()
    manifest2 = await run_audio(store, video_ready_project, healthy)
    assert healthy.calls == [target]
    assert manifest2.is_usable


@pytest.mark.asyncio
async def test_changing_voice_regenerates_that_clip(store, video_ready_project):
    await run_audio(store, video_ready_project, FakeTTSProvider())

    new_provider = FakeTTSProvider()
    manifest = await run_audio(
        store,
        video_ready_project,
        new_provider,
        voice_overrides={"char_1": "zh-CN-XiaoyiNeural"},
    )

    # 只重合成换过音色的角色（这几场台词都是 char_1 说的）
    assert len(new_provider.calls) == len(manifest.clips)
    assert all(manifest.get(cid).speaker == "char_1" for cid in new_provider.calls)
    assert manifest.voice_map["char_1"] == "zh-CN-XiaoyiNeural"
    assert all(manifest.get(cid).voice == "zh-CN-XiaoyiNeural" for cid in new_provider.calls)

    # 给没出场的角色换音色不该触发任何重新合成
    untouched = FakeTTSProvider()
    await run_audio(
        store,
        video_ready_project,
        untouched,
        voice_overrides={"char_2": "zh-CN-YunyangNeural"},
    )
    assert untouched.calls == []


@pytest.mark.asyncio
async def test_placeholder_audio_is_not_usable(store, video_ready_project):
    provider = FakeTTSProvider(media_kind=MediaKind.PLACEHOLDER)
    manifest = await run_audio(store, video_ready_project, provider)

    assert manifest.placeholder_clips
    assert not manifest.is_usable
    assert manifest.summary()["placeholder"] == len(manifest.clips)


@pytest.mark.asyncio
async def test_concurrency_is_bounded(store, video_ready_project):
    provider = FakeTTSProvider(delay=0.05)
    await run_audio(store, video_ready_project, provider, concurrency=2)
    assert provider.max_active <= 2, "并发不能突破上限"
    assert provider.max_active >= 2, "应该真的并发跑，而不是串行"


@pytest.mark.asyncio
async def test_empty_storyboard_is_silent_not_error(store, video_ready_project):
    storyboard = store.load_storyboard(video_ready_project.id)
    for shot in storyboard.shots:
        shot.dialogue_text = None
        shot.narration = None
    store.save_storyboard(video_ready_project.id, storyboard)

    provider = FakeTTSProvider()
    manifest = await run_audio(store, video_ready_project, provider)

    assert manifest.clips == []
    assert provider.calls == []
    assert manifest.is_usable  # 无声片也是合法成片
    assert any("没有任何台词" in w for w in manifest.warnings)


# ── 音画对齐 ──────────────────────────────────────────


def make_manifest(shot_id: str, duration: float) -> AudioManifest:
    from videogen.models.audio import AudioClip

    manifest = AudioManifest(provider="fake")
    manifest.upsert(
        AudioClip(
            id=shot_id,
            shot_id=shot_id,
            scene_id="scene_1",
            text="台词",
            voice="v",
            status=ClipStatus.READY,
            duration_sec=duration,
        )
    )
    return manifest


def test_alignment_extends_but_never_shrinks():
    storyboard = Storyboard(
        shots=[
            Shot(id="s1", scene_id="scene_1", shot_no=1, duration_sec=4),
            Shot(id="s2", scene_id="scene_1", shot_no=2, duration_sec=4),
        ]
    )
    manifest = AudioManifest(provider="fake")
    manifest.upsert(make_manifest("s1", 6.2).clips[0])  # 需要 6.2+0.25 -> 7s
    manifest.upsert(make_manifest("s2", 1.0).clips[0])  # 比镜头短 -> 不动

    adjustments = audio_service.align_storyboard(storyboard, manifest, gap_sec=0.25)

    assert [s.duration_sec for s in storyboard.shots] == [7, 4]
    assert storyboard.audio_aligned is True
    assert len(adjustments) == 1
    assert adjustments[0].shot_id == "s1"
    assert adjustments[0].from_sec == 4 and adjustments[0].to_sec == 7
    assert manifest.adjustments == adjustments


def test_alignment_warns_when_audio_far_exceeds_shot():
    storyboard = Storyboard(shots=[Shot(id="s1", scene_id="s", shot_no=1, duration_sec=3)])
    manifest = make_manifest("s1", 12.0)

    audio_service.align_storyboard(storyboard, manifest, gap_sec=0.25, max_ratio=2.0)

    assert storyboard.shots[0].duration_sec == 13
    assert any("远超原时长" in w for w in manifest.warnings)


def test_alignment_ignores_clips_without_duration():
    storyboard = Storyboard(shots=[Shot(id="s1", scene_id="s", shot_no=1, duration_sec=4)])
    manifest = make_manifest("s1", 0.0)
    assert audio_service.align_storyboard(storyboard, manifest) == []
    assert storyboard.shots[0].duration_sec == 4


# ── BGM 曲库 ──────────────────────────────────────────


def write_library(tmp_path):
    for name in ("tense_01.mp3", "warm_01.mp3"):
        (tmp_path / name).write_bytes(b"fake")
    (tmp_path / "bgm.json").write_text(
        json.dumps(
            {
                "tracks": [
                    {
                        "id": "tense_01",
                        "title": "暗涌",
                        "file": "tense_01.mp3",
                        "moods": ["紧张", "压抑"],
                        "gain_db": -22,
                        "license": "CC0",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_local_library_reads_metadata_and_auto_discovers(tmp_path):
    library = LocalBgmLibrary(directory=write_library(tmp_path))
    tracks = {t.id: t for t in library.tracks()}

    assert set(tracks) == {"tense_01", "warm_01"}
    assert tracks["tense_01"].title == "暗涌"
    assert tracks["tense_01"].gain_db == -22
    # 没写进元数据的文件按文件名切情绪标签
    assert tracks["warm_01"].moods == ["warm"]


def test_local_library_pick_prefers_mood_match(tmp_path):
    library = LocalBgmLibrary(directory=write_library(tmp_path))
    assert library.pick(["紧张"]).id == "tense_01"
    assert library.pick(["不存在的情绪"], fallback=False) is None
    assert library.pick(["不存在的情绪"]).id is not None  # 兜底给一首
    assert library.pick([], preferred_id="warm_01").id == "warm_01"


def test_local_library_missing_dir_is_empty(tmp_path):
    library = LocalBgmLibrary(directory=tmp_path / "nope")
    assert library.tracks() == []
    assert library.pick(["紧张"]) is None


def test_local_library_skips_broken_metadata(tmp_path):
    (tmp_path / "broken.mp3").write_bytes(b"x")
    (tmp_path / "bgm.json").write_text("{ not json", encoding="utf-8")
    library = LocalBgmLibrary(directory=tmp_path)
    assert [t.id for t in library.tracks()] == ["broken"]


def test_match_score_is_tolerant():
    assert match_score(["紧张"], ["紧张"]) == 2.0
    assert match_score(["紧张的世界"], ["紧张"]) == 1.0
    assert match_score(["warm"], ["WARM"]) == 2.0
    assert match_score([], ["紧张"]) == 0.0


# ── pipeline / job 层 ─────────────────────────────────


@pytest.mark.asyncio
async def test_audio_job_end_to_end(store, media_pipeline, video_ready_project):
    expected = len(dialog_shots(store, video_ready_project))
    job = await media_pipeline.start_audio_generation(video_ready_project.id)
    assert job.kind == "audio"
    assert store.load_project(video_ready_project.id).status == ProjectStatus.AUDIO_GENERATING

    finished = await wait_for_job(media_pipeline, job.id)
    assert finished.status.value == "succeeded"
    assert finished.percent == 100

    assert store.load_project(video_ready_project.id).status == ProjectStatus.AUDIO_READY
    manifest = store.load_audio_manifest(video_ready_project.id)
    assert len(manifest.ready_clips) == expected
    assert manifest.is_usable
    # 假曲库匹配到了 BGM
    assert manifest.bgm is not None and manifest.bgm.id == "fake_bgm"


@pytest.mark.asyncio
async def test_audio_requires_video_first(store, pipeline, media_pipeline, ready_project):
    with pytest.raises(PipelineError) as exc:
        await media_pipeline.start_audio_generation(ready_project.id)
    assert "视频" in str(exc.value)


@pytest.mark.asyncio
async def test_audio_all_failed_falls_back_to_video_ready(store, video_ready_project, fake_llm):
    pipeline = build_pipeline(store, fake_llm, AlwaysFailTTSProvider())

    job = await pipeline.start_audio_generation(video_ready_project.id)
    finished = await wait_for_job(pipeline, job.id)

    assert finished.status.value == "failed"
    assert "全部" in finished.error
    assert store.load_project(video_ready_project.id).status == ProjectStatus.VIDEO_READY
    assert list(store.audio_dir(video_ready_project.id).glob("*.mp3")) == []


@pytest.mark.asyncio
async def test_duplicate_audio_job_is_rejected(store, video_ready_project, fake_llm):
    pipeline = build_pipeline(store, fake_llm, FakeTTSProvider(delay=0.2))

    job = await pipeline.start_audio_generation(video_ready_project.id)
    with pytest.raises(PipelineError) as exc:
        await pipeline.start_audio_generation(video_ready_project.id)
    assert "已有配音任务" in str(exc.value)

    await pipeline.cancel_audio_generation(video_ready_project.id)
    await wait_for_job(pipeline, job.id)
    assert store.load_project(video_ready_project.id).status == ProjectStatus.VIDEO_READY


@pytest.mark.asyncio
async def test_cancel_audio_job(store, video_ready_project, fake_llm):
    pipeline = build_pipeline(store, fake_llm, FakeTTSProvider(delay=0.3))

    job = await pipeline.start_audio_generation(video_ready_project.id)
    await asyncio.sleep(0.05)
    assert await pipeline.cancel_audio_generation(video_ready_project.id) == 1

    finished = await wait_for_job(pipeline, job.id)
    assert finished.status.value == "cancelled"
    assert store.load_project(video_ready_project.id).status == ProjectStatus.VIDEO_READY


@pytest.mark.asyncio
async def test_audio_status_shape(store, media_pipeline, video_ready_project):
    status = media_pipeline.audio_status(video_ready_project.id)
    assert status["manifest"] is None and status["job"] is None

    job = await media_pipeline.start_audio_generation(video_ready_project.id)
    await wait_for_job(media_pipeline, job.id)

    status = media_pipeline.audio_status(video_ready_project.id)
    assert status["summary"]["usable"] is True
    assert status["job"]["status"] == "succeeded"
    assert status["manifest"]["clips"][0]["path"].startswith("audio/")
    assert status["voice_map"]


@pytest.mark.asyncio
async def test_recover_interrupted_audio_status(store, media_pipeline, video_ready_project):
    def force_status(value: str) -> None:
        path = store.project_dir(video_ready_project.id) / "meta.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["status"] = value
        path.write_text(json.dumps(raw), encoding="utf-8")

    # 还没配音就中断 -> 回到 VIDEO_READY
    force_status("audio_generating")
    assert video_ready_project.id in await media_pipeline.recover_interrupted_jobs()
    assert store.load_project(video_ready_project.id).status == ProjectStatus.VIDEO_READY

    # 配音完成后中断 -> 恢复到 AUDIO_READY
    job = await media_pipeline.start_audio_generation(video_ready_project.id)
    await wait_for_job(media_pipeline, job.id)
    force_status("audio_generating")
    await media_pipeline.recover_interrupted_jobs()
    assert store.load_project(video_ready_project.id).status == ProjectStatus.AUDIO_READY


@pytest.mark.asyncio
async def test_rerunning_video_keeps_audio_but_drops_compose(
    store, media_pipeline, video_ready_project
):
    job = await media_pipeline.start_audio_generation(video_ready_project.id)
    await wait_for_job(media_pipeline, job.id)
    audio_before = store.load_audio_manifest(video_ready_project.id)
    assert audio_before is not None

    # 重跑视频：配音与文本无关，不该被作废（省掉重复 TTS 费用）
    output = store.output_dir(video_ready_project.id)
    (output / "final.mp4").write_bytes(b"stale")
    job2 = await media_pipeline.start_video_generation(video_ready_project.id)
    await wait_for_job(media_pipeline, job2.id)

    assert store.load_audio_manifest(video_ready_project.id) is not None
    # 片段一个都没重跑 → 成片不算过期，不该被删（否则「空跑」会白扔成片）
    assert (output / "final.mp4").exists()
    # 配音仍在 -> 状态保持 AUDIO_READY，可以直接重新合成
    assert store.load_project(video_ready_project.id).status == ProjectStatus.AUDIO_READY

    # 真的重跑片段（把一个片段标成失败，逼它重新生成）→ 成片必须作废
    manifest = store.load_video_manifest(video_ready_project.id)
    manifest.clips[0].status = ClipStatus.FAILED
    store.save_video_manifest(video_ready_project.id, manifest)
    job3 = await media_pipeline.start_video_generation(video_ready_project.id)
    await wait_for_job(media_pipeline, job3.id)
    assert not (store.project_dir(video_ready_project.id) / "output").exists()


@pytest.mark.asyncio
async def test_regenerating_storyboard_invalidates_media(
    store, media_pipeline, video_ready_project
):
    # 已经出过片也要能退回分镜重做（否则只能删项目）
    assert await media_pipeline.reopen_storyboard(video_ready_project.id)
    assert store.load_project(video_ready_project.id).status == ProjectStatus.SCRIPT_CONFIRMED
    await media_pipeline.generate_storyboard(video_ready_project.id)

    assert store.load_video_manifest(video_ready_project.id) is None
    assert store.load_audio_manifest(video_ready_project.id) is None
    assert not (store.project_dir(video_ready_project.id) / "clips").exists()


# ── provider 层 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_tts_produces_real_audio(tmp_path):
    from videogen.providers.tts.mock import MockTTSProvider
    from videogen.utils.ffmpeg import ffmpeg_available

    if not ffmpeg_available():
        pytest.skip("环境里没有 ffmpeg，跳过真实音频用例")

    provider = MockTTSProvider()
    audio = await provider.synthesize(
        TTSRequest(
            clip_id="scene_1_shot_1",
            text="这是十四个字的配音测试台词。",
            output_path=tmp_path / "a.mp3",
        )
    )
    assert audio.media_kind == MediaKind.AUDIO
    assert audio.path.exists() and audio.path.stat().st_size > 0
    assert audio.duration_sec > 0


@pytest.mark.asyncio
async def test_mock_tts_placeholder_without_ffmpeg(monkeypatch, tmp_path):
    from videogen.providers.tts.mock import MockTTSProvider

    monkeypatch.setattr("videogen.providers.tts.mock.ffmpeg_available", lambda: False)
    provider = MockTTSProvider()
    audio = await provider.synthesize(
        TTSRequest(clip_id="c1", text="没有 ffmpeg 的时候", output_path=tmp_path / "a.mp3")
    )
    assert audio.media_kind == MediaKind.PLACEHOLDER
    assert audio.path.suffix == ".json"
    assert "placeholder" in audio.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_edge_provider_rejects_empty_text(tmp_path):
    provider = EdgeTTSProvider()
    with pytest.raises(TTSProviderError):
        await provider.synthesize(
            TTSRequest(clip_id="c1", text="（只是舞台提示）", output_path=tmp_path / "a.mp3")
        )


def test_tts_provider_factory(monkeypatch):
    from videogen.config import get_settings
    from videogen.providers.tts import get_tts_provider
    from videogen.providers.tts.mock import MockTTSProvider

    settings = get_settings()
    monkeypatch.setattr(settings, "tts_provider", "mock")
    assert isinstance(get_tts_provider(), MockTTSProvider)

    monkeypatch.setattr(settings, "tts_provider", "edge")
    assert isinstance(get_tts_provider(), EdgeTTSProvider)

    monkeypatch.setattr(settings, "tts_provider", "nope")
    with pytest.raises(TTSProviderError):
        get_tts_provider()


def test_voice_map_falls_back_for_unknown_speaker():
    storyboard = Storyboard(
        shots=[
            Shot(
                id="s1",
                scene_id="scene_1",
                shot_no=1,
                dialogue_text="台词",
                dialogue_speaker="char_unknown",
            )
        ]
    )
    llm = FakeLLM()
    assert llm  # 保持 fixture 语义：这里用不到 LLM
    voices = audio_service.build_voice_map(storyboard, None)
    assert voices["char_unknown"].startswith("zh-CN-")


def test_bgm_track_ignores_unknown_preferred_id():
    library = FakeBgmLibrary()
    track = library.pick(["紧张"], preferred_id="不存在的曲子")
    assert isinstance(track, BgmTrack)
    assert track.id == "fake_bgm"


@pytest.mark.asyncio
async def test_deleted_line_drops_old_dub(store, video_ready_project):
    """回归：镜头还在、台词被删了，旧配音还留在清单里 —— 合成会照样念那句删掉的话。"""
    provider = FakeTTSProvider()
    manifest = await run_audio(store, video_ready_project, provider)
    victim = manifest.clips[0].shot_id

    storyboard = store.load_storyboard(video_ready_project.id)
    for shot in storyboard.shots:
        if shot.id == victim:
            shot.dialogue_text = ""
            shot.dialogue_speaker = ""
            shot.narration = ""
    store.save_storyboard(video_ready_project.id, storyboard)

    again = await run_audio(store, video_ready_project, provider)
    assert again.for_shot(victim) is None
