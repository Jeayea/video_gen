"""黑场镜头（本机生成、$0）与镜头转场（叠化 / 经黑场淡出淡入，不改时间轴）。"""

from __future__ import annotations

import pytest

from tests.conftest import wait_for_job
from tests.factories import small_project
from tests.fakes import FakeVideoProvider
from videogen.models.audio import AudioClip, AudioManifest
from videogen.models.compose import ComposeSegment
from videogen.models.project import ProjectStatus
from videogen.models.storyboard import Shot, ShotDraft, Storyboard
from videogen.models.video import ClipStatus, VideoClip, VideoManifest
from videogen.orchestrator.pipeline import Pipeline
from videogen.providers.tts.mock import MockTTSProvider
from videogen.providers.video.mock import MockVideoProvider
from videogen.services import compose_service, preflight_service, storyboard_service, video_service
from videogen.utils.ffmpeg import ffmpeg_available, probe_duration, run_ffmpeg_bytes

requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="环境里没有 ffmpeg")


def shot(index: int, **fields) -> Shot:
    data = {
        "id": f"scene_1_shot_{index}",
        "scene_id": "scene_1",
        "shot_no": index,
        "duration_sec": 2,
        "video_prompt": "a woman in an office",
    }
    data.update(fields)
    return Shot(**data)


# ── 模型 ──────────────────────────────────────────────


def test_transition_aliases_normalize():
    assert shot(1, transition="叠化").transition == "dissolve"
    assert shot(1, transition="Dip to black").transition == "fade"
    assert shot(1, transition="cut").transition == ""
    assert shot(1, transition="wipe").transition == ""  # 不认识的当硬切
    assert ShotDraft(transition="淡入淡出", black_screen="是").transition == "fade"
    assert shot(1, transition_sec="0").transition_sec is None


def test_black_screen_has_nobody_in_frame_and_speaks_offscreen():
    black = shot(
        1,
        black_screen=True,
        character_in_frame=["char_1"],
        props_in_frame=["prop_1"],
        dialogue_text="房租这周要交。",
        dialogue_speaker="char_6",
    )
    assert black.character_in_frame == [] and black.props_in_frame == []
    assert black.voice_offscreen is True


def test_signature_of_ordinary_shots_is_unchanged():
    """新增字段不能让老镜头的指纹变掉（否则已提交任务、已买的整段都会作废）。"""
    plain = shot(1)
    assert len(video_service.shot_signature(plain)) == len(video_service._VIDEO_FIELDS)
    assert video_service.shot_signature(shot(1, transition="fade")) == video_service.shot_signature(
        plain
    )
    assert video_service.shot_signature(shot(1, black_screen=True)) != (
        video_service.shot_signature(plain)
    )


# ── 编段与计费 ────────────────────────────────────────


def test_black_screen_breaks_segments_and_is_never_merged_across():
    board = Storyboard(
        shots=[shot(1), shot(2), shot(3, black_screen=True), shot(4, duration_sec=1)]
    )
    segments = storyboard_service.assign_segments(board, target_sec=12, max_sec=15, min_sec=5)
    ids = [seg.shot_ids for seg in segments]
    assert ["scene_1_shot_1", "scene_1_shot_2"] in ids
    assert ["scene_1_shot_4"] in ids  # 太短也不并进黑场前面那段
    assert board.shots[2].segment_id == ""
    units = storyboard_service.segments_of(board)
    assert [u.black for u in units] == [False, True, False]


def test_black_units_are_not_bought():
    board = Storyboard(shots=[shot(1), shot(2, black_screen=True)])
    provider = FakeVideoProvider()
    pending = video_service.pending_units(board, None, provider)
    assert [u.shot_ids for u in pending] == [["scene_1_shot_1"]]


@pytest.mark.asyncio
async def test_preflight_skips_black_seconds():
    board = Storyboard(shots=[shot(1, duration_sec=3), shot(2, duration_sec=4, black_screen=True)])
    plan = await preflight_service.video_plan(
        board, None, FakeVideoProvider(price_usd_per_sec=0.1)
    )
    assert plan["billed_seconds"] == 3
    assert plan["billed_seconds_if_all"] == 3
    assert plan["black_shots"] == 1
    assert plan["to_generate"] == 1


def test_preflight_audio_counts_black_lines_in_native_mode():
    board = Storyboard(
        shots=[
            shot(1, dialogue_text="原声念的这句不计费", dialogue_speaker="char_1"),
            shot(2, black_screen=True, dialogue_text="房租这周要交", dialogue_speaker="char_6"),
        ]
    )
    plan = preflight_service.audio_plan(board, None, dub_mode="native")
    assert plan["tts_chars"] == len("房租这周要交")
    assert any("黑场" in w for w in plan["warnings"])


def test_chain_never_uses_a_black_frame():
    board = Storyboard(shots=[shot(1), shot(2, black_screen=True), shot(3)])
    assert storyboard_service.chain_predecessors(storyboard_service.segments_of(board)) == {}


# ── 原声模式下的黑场配音 ──────────────────────────────


def test_black_voice_shots_and_native_voice_exclusion():
    board = Storyboard(
        shots=[
            shot(1, dialogue_text="你好", dialogue_speaker="char_1"),
            shot(2, black_screen=True, dialogue_text="喂？", dialogue_speaker="char_6"),
            shot(3, black_screen=True),  # 无声黑场
        ]
    )
    manifest = VideoManifest(
        clips=[
            VideoClip(shot_id=s.id, status=ClipStatus.READY, path=f"clips/{s.id}.mp4")
            for s in board.shots
        ]
    )
    assert video_service.black_voice_shots(board) == {"scene_1_shot_2"}
    assert video_service.native_voice_shots(board, manifest, dub_mode="native") == {
        "scene_1_shot_1"
    }


def test_native_compose_reads_only_black_shot_dubs(store):
    project = small_project(store)
    board = Storyboard(
        shots=[
            shot(1, dialogue_text="你好", dialogue_speaker="char_1"),
            shot(2, black_screen=True, dialogue_text="喂？", dialogue_speaker="char_6"),
        ]
    )
    audio = AudioManifest(
        clips=[
            AudioClip(id="a1", shot_id="scene_1_shot_1", status=ClipStatus.READY, text="你好"),
            AudioClip(id="a2", shot_id="scene_1_shot_2", status=ClipStatus.READY, text="喂？"),
        ]
    )
    project.dub_mode = "native"
    picked = compose_service.voice_audio_for(project, audio, store_settings(), board)
    assert picked is not None and [c.shot_id for c in picked.clips] == ["scene_1_shot_2"]
    project.dub_mode = "native"
    assert compose_service.voice_audio_for(project, audio, store_settings(), None) is None


def store_settings():
    from videogen.config import get_settings

    return get_settings()


# ── 转场规划（不改时长）───────────────────────────────


def segs(durations: list[float], black: tuple[int, ...] = ()) -> list[ComposeSegment]:
    return [
        ComposeSegment(
            shot_id=f"scene_1_shot_{i + 1}",
            index=i,
            duration_sec=d,
            video_path=f"clips/scene_1_shot_{i + 1}.mp4",
        )
        for i, d in enumerate(durations)
    ]


@pytest.mark.asyncio
async def test_apply_transitions_splits_fade_and_keeps_durations(tmp_path, settings):
    settings.compose_transition_sec = 1.0
    board = Storyboard(
        shots=[
            shot(1, transition="fade"),  # 第一镜：从黑淡入
            shot(2, transition="fade"),  # 两镜之间：各占一半
            shot(3, transition="dissolve", transition_sec=0.8),
            shot(4, black_screen=True, transition="fade"),  # 进黑场：上一镜独占淡出
            shot(5, transition="dissolve"),  # 出黑场：叠化 = 从黑淡入
        ]
    )
    segments = segs([2, 2, 2, 3, 2])
    used = await compose_service.apply_transitions(segments, board, tmp_path, settings)
    assert used == 5
    assert [s.duration_sec for s in segments] == [2, 2, 2, 3, 2]
    assert segments[0].fade_in_sec == 1.0
    assert segments[0].fade_out_sec == 0.5 and segments[1].fade_in_sec == 0.5
    assert segments[2].dissolve_sec == 0.8
    assert segments[2].dissolve_from == "clips/scene_1_shot_2.mp4"
    assert segments[2].fade_out_sec == 1.0 and segments[3].black_screen
    assert segments[3].fade_in_sec == 0 and segments[4].fade_in_sec == 1.0
    assert segments[4].dissolve_sec == 0


@pytest.mark.asyncio
async def test_transitions_never_exceed_half_a_shot(tmp_path, settings):
    board = Storyboard(shots=[shot(1), shot(2, transition="fade", transition_sec=5)])
    segments = segs([1.0, 4.0])
    await compose_service.apply_transitions(segments, board, tmp_path, settings)
    assert segments[0].fade_out_sec == 0.5 and segments[1].fade_in_sec == 0.5


@pytest.mark.asyncio
async def test_phone_call_black_lines_get_the_handset_voice(tmp_path, settings):
    """黑场带电话音效的那场 = 一通电话：画外台词（含没挂音效的那几镜）过听筒音色。"""
    board = Storyboard(
        shots=[
            shot(1, black_screen=True, sfx=["phone_ring"]),
            shot(2, black_screen=True, dialogue_text="房租这礼拜到账。", voice_offscreen=True),
            shot(3, dialogue_text="好。"),  # 不是黑场：不动
            shot(4, black_screen=True, dialogue_text="喂？", voice_offscreen=True, scene_id="s2"),
        ]
    )
    segments = segs([2, 2, 2, 2])
    await compose_service.apply_transitions(segments, board, tmp_path, settings)
    assert [s.voice_phone for s in segments] == [False, True, False, False]
    assert "lowpass=f=3400" in compose_service._segment_filter(
        1080, 1920, 2.0, 24, voice=True, ambient=False, voice_phone=True
    )
    assert "lowpass" not in compose_service._segment_filter(
        1080, 1920, 2.0, 24, voice=True, ambient=False
    )

    settings.compose_black_phone_voice = False
    await compose_service.apply_transitions(segments, board, tmp_path, settings)
    assert not any(s.voice_phone for s in segments)


# ── 真实 ffmpeg ──────────────────────────────────────


async def mean_luma(path, at: float) -> float:
    raw = await run_ffmpeg_bytes(
        [
            "-v", "error", "-ss", f"{at:.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", "scale=16:16,format=gray", "-f", "rawvideo", "-",
        ]
    )
    return sum(raw) / max(1, len(raw))


def media_pipeline(store) -> Pipeline:
    from tests.fakes import FakeBgmLibrary, FakeLLM

    return Pipeline(
        store,
        llm_factory=lambda: FakeLLM(),
        video_factory=lambda: MockVideoProvider(),
        tts_factory=lambda: MockTTSProvider(),
        bgm_factory=lambda: FakeBgmLibrary(),
    )


@requires_ffmpeg
@pytest.mark.asyncio
async def test_black_screen_and_transitions_end_to_end(store, settings):
    """黑场本机生成、原声模式下照样配音；转场烧进画面、成片时长不变。"""
    settings.audio_dub_mode = "native"
    settings.compose_transition_sec = 1.0
    project = small_project(store, shots=3, shot_duration=2)
    board = store.load_storyboard(project.id)
    board.shots[1].transition = "dissolve"
    board.shots[2] = board.shots[2].model_copy(
        update={
            "black_screen": True,
            "transition": "fade",
            "dialogue_text": "房租这周要交。",
            "voice_offscreen": True,
        }
    )
    store.save_storyboard(project.id, board)

    pipeline = media_pipeline(store)
    job = await pipeline.start_video_generation(project.id)
    assert (await wait_for_job(pipeline, job.id)).status.value == "succeeded"
    video = store.load_video_manifest(project.id)
    black_clip = video.get("scene_1_shot_3")
    assert black_clip.provider == video_service.BLACK_PROVIDER
    assert black_clip.cost_usd == 0 and black_clip.status == ClipStatus.READY
    project_dir = store.ensure_project_dir(project.id)
    assert await mean_luma(project_dir / black_clip.path, 0.5) < 2

    job = await pipeline.start_audio_generation(project.id)
    assert (await wait_for_job(pipeline, job.id)).status.value == "succeeded"
    audio = store.load_audio_manifest(project.id)
    assert [c.shot_id for c in audio.clips] == ["scene_1_shot_3"]  # 只有黑场走 TTS

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    final = project_dir / report.output_path
    total = sum(s.duration_sec for s in report.segments)
    assert abs((await probe_duration(final)) - total) < 0.1  # 转场不改时长
    second = report.segments[1]
    assert second.dissolve_sec == 1.0 and second.dissolve_from
    assert report.segments[1].fade_out_sec == 1.0  # 转进黑场：上一镜淡出到黑
    end_of_second = report.segments[0].duration_sec + second.duration_sec - 0.05
    assert await mean_luma(final, end_of_second) < 8  # 淡出到底接近全黑
    assert await mean_luma(final, report.segments[0].duration_sec + 1.2) > 20
    assert any("黑场" in w for w in report.warnings)
    assert store.load_project(project.id).status == ProjectStatus.DONE


# ── 黑场的画面文字与音效 ──────────────────────────────


def test_sfx_names_normalize_and_change_the_black_signature():
    black = shot(1, black_screen=True, sfx="铃声，接通, 不认识", screen_text=" 八个月前 ")
    assert black.sfx == ["phone_ring", "call_connect"] and black.screen_text == "八个月前"
    plain_black = shot(1, black_screen=True)
    assert video_service.shot_signature(black) != video_service.shot_signature(plain_black)
    # 非黑场镜头写了也不影响签名（只对黑场生效）
    assert video_service.shot_signature(shot(1, screen_text="x")) == video_service.shot_signature(
        shot(1)
    )


def test_sfx_track_layout_one_shots_and_loops(tmp_path):
    import wave

    from videogen.utils import sfx

    path = sfx.render_track(["phone_ring", "call_connect"], 2.0, tmp_path / "a.wav")
    with wave.open(str(path)) as wav:
        assert wav.getnchannels() == 2 and abs(wav.getnframes() - 2 * sfx.SAMPLE_RATE) <= 1
        frames = wav.readframes(wav.getnframes())
    import array

    pcm = array.array("h", frames)
    loud = max(abs(v) for v in pcm[: sfx.SAMPLE_RATE])  # 第一秒在响铃
    assert loud > 3000
    busy = sfx.render_track(["busy_tone"], 1.4, tmp_path / "b.wav")
    with wave.open(str(busy)) as wav:
        pcm = array.array("h", wav.readframes(wav.getnframes()))
    # 忙音：0.35 秒响、0.35 秒停
    off = pcm[2 * int(0.45 * sfx.SAMPLE_RATE): 2 * int(0.65 * sfx.SAMPLE_RATE)]
    assert max(abs(v) for v in off) == 0


def test_title_card_has_text_in_the_middle(tmp_path):
    from PIL import Image

    from videogen.utils import title_card

    path = title_card.render("八个月前", 360, 640, tmp_path / "card.png")
    image = Image.open(path).convert("L")
    assert image.getpixel((5, 5)) == 0
    middle = image.crop((60, 280, 300, 360))
    assert middle.getextrema()[1] > 150  # 中间一行字是亮的


@requires_ffmpeg
@pytest.mark.asyncio
async def test_black_card_and_sfx_end_up_in_the_film(store, settings):
    settings.audio_dub_mode = "native"
    project = small_project(store, shots=2, shot_duration=2)
    board = store.load_storyboard(project.id)
    board.shots[1] = board.shots[1].model_copy(
        update={"black_screen": True, "screen_text": "八个月前", "sfx": ["phone_ring"],
                "dialogue_text": None, "voice_offscreen": False}
    )
    store.save_storyboard(project.id, board)
    pipeline = media_pipeline(store)
    for start in (pipeline.start_video_generation, pipeline.start_audio_generation):
        job = await start(project.id)
        assert (await wait_for_job(pipeline, job.id)).status.value == "succeeded"
    project_dir = store.ensure_project_dir(project.id)
    clip = store.load_video_manifest(project.id).get(board.shots[1].id)
    assert clip.provider == video_service.BLACK_PROVIDER
    raw = await run_ffmpeg_bytes(
        ["-v", "error", "-ss", "1.0", "-i", str(project_dir / clip.path), "-frames:v", "1",
         "-vf", "format=gray", "-f", "rawvideo", "-"]
    )
    assert max(raw) > 150 and raw[0] < 10  # 中间有亮字，角落是黑的

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    start = report.segments[0].duration_sec
    pcm = await run_ffmpeg_bytes(
        ["-v", "error", "-ss", f"{start + 0.1:.2f}", "-t", "0.8", "-i",
         str(project_dir / report.output_path), "-ac", "1", "-f", "s16le", "-"]
    )
    import array

    samples = array.array("h", pcm)
    assert samples and max(abs(v) for v in samples) > 1500  # 黑场里听得见铃声


@pytest.mark.asyncio
async def test_black_shots_shrink_to_the_voice_but_keep_loops_and_one_shots(tmp_path):
    board = Storyboard(
        shots=[
            shot(1, black_screen=True, duration_sec=4,
                 dialogue_text="喂？", dialogue_speaker="char_6"),
            shot(2, black_screen=True, duration_sec=3, sfx=["phone_ring"],
                 dialogue_text="喂？", dialogue_speaker="char_6"),
            shot(3, black_screen=True, duration_sec=3, sfx=["call_connect", "hang_up"],
                 dialogue_text="嗯", dialogue_speaker="char_6"),
            shot(4, duration_sec=4, dialogue_text="你好", dialogue_speaker="char_1"),  # 不是黑场
            shot(5, black_screen=True, duration_sec=2),  # 没有配音
        ]
    )
    audio = AudioManifest(clips=[
        AudioClip(id=f"a{i}", shot_id=f"scene_1_shot_{i}", status=ClipStatus.READY, duration_sec=d,
                  path=f"audio/a{i}.wav")
        for i, d in ((1, 1.2), (2, 1.2), (3, 0.1), (4, 1.0))
    ])
    segments = segs([4, 3, 3, 4, 2])
    for seg in segments[:4]:
        seg.audio_path = f"audio/a{seg.index + 1}.wav"  # 文件不在：按配音时长算「说完」
    await compose_service._fit_black_to_voice(segments, board, audio, tmp_path, 0.4)
    assert [s.duration_sec for s in segments] == [1.6, 3, 0.5, 4, 2]
