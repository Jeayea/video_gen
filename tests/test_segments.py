"""段落分镜：同一场相邻镜头编成一段，一次生成（段内硬切），再切回单镜片段。

要守住的几件事：
- 编段是确定性的：按场、按地点断开，装到目标秒数/上限/镜头数就收段，场尾太短并进前段
- 一段只调一次模型、只记一次账（扣费记在段内第一个镜头上），切出来的单镜片段时长对得上
- 切点吸附到画面里真实的硬切；探测不到就按计划时间切
- 整段已买到但切分失败：不重买，下次直接重切；账照记
- 预算/preflight 按「段」计费：3 个 3 秒镜头 = 一次 9 秒，而不是 3×5 秒
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import FakeLLM, FakeVideoProvider
from videogen.models.bible import Character, Setting, StoryBible
from videogen.models.project import Project, ProjectStatus
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.video import ClipStatus, VideoClip, VideoManifest
from videogen.orchestrator.pipeline import Pipeline
from videogen.providers.video.base import GeneratedClip, VideoRequest
from videogen.providers.video.mock import MockVideoProvider
from videogen.services import storyboard_service, video_service
from videogen.utils.ffmpeg import ffmpeg_available, probe_duration, run_ffmpeg

requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="需要 ffmpeg")


@pytest.fixture(autouse=True)
def segment_mode(settings, monkeypatch):
    monkeypatch.setattr(settings, "video_segment_mode", True)
    return settings


def _shot(sid: str, scene: str, sec: int, **extra) -> Shot:
    return Shot(id=sid, scene_id=scene, shot_no=1, duration_sec=sec, **extra)


# ── 编段 ──────────────────────────────────────────────


def test_assign_segments_packs_by_target_and_breaks_on_scene_and_location():
    storyboard = Storyboard(
        shots=[
            _shot("a1", "scene_1", 3, location="loc_1"),
            _shot("a2", "scene_1", 4, location="loc_1"),
            _shot("a3", "scene_1", 3, location="loc_1"),
            _shot("a4", "scene_1", 4, location="loc_1"),  # 3+4+3=10 <12，再装 14 ≤15 → 收在 14
            _shot("a5", "scene_1", 5, location="loc_2"),  # 换地点 → 新段
            _shot("b1", "scene_2", 4, location="loc_1"),  # 换场 → 新段
        ]
    )
    segments = storyboard_service.assign_segments(storyboard, target_sec=12, max_sec=15)
    assert [s.shot_ids for s in segments] == [["a1", "a2", "a3", "a4"], ["a5"], ["b1"]]
    assert [s.id for s in segments] == ["scene_1_seg_1", "scene_1_seg_2", "scene_2_seg_1"]
    assert storyboard.shots[0].segment_id == "scene_1_seg_1"


def test_assign_segments_respects_max_shots_and_max_sec():
    storyboard = Storyboard(shots=[_shot(f"s{i}", "scene_1", 2) for i in range(1, 7)])
    segments = storyboard_service.assign_segments(
        storyboard, target_sec=20, max_sec=20, max_shots=4
    )
    assert [len(s.shots) for s in segments] == [4, 2]

    storyboard = Storyboard(shots=[_shot(f"s{i}", "scene_1", 6) for i in range(1, 4)])
    segments = storyboard_service.assign_segments(storyboard, target_sec=15, max_sec=15)
    assert [s.duration_sec for s in segments] == [12, 6]


def test_short_tail_merges_into_previous_segment_when_it_fits():
    storyboard = Storyboard(
        shots=[_shot("s1", "scene_1", 5), _shot("s2", "scene_1", 5), _shot("s3", "scene_1", 3)]
    )
    segments = storyboard_service.assign_segments(
        storyboard, target_sec=10, max_sec=15, min_sec=5
    )
    assert [s.shot_ids for s in segments] == [["s1", "s2", "s3"]]


def test_segments_of_treats_unsegmented_shots_as_singletons():
    storyboard = Storyboard(
        shots=[
            _shot("s1", "scene_1", 3, segment_id="g1"),
            _shot("s2", "scene_1", 3, segment_id="g1"),
            _shot("s3", "scene_1", 3),
        ]
    )
    assert [s.shot_ids for s in storyboard_service.segments_of(storyboard)] == [
        ["s1", "s2"],
        ["s3"],
    ]


# ── prompt / 参考图 ───────────────────────────────────


def test_segment_prompt_lists_timestamps_dialogue_and_anchors_once(settings):
    bible = StoryBible(
        characters=[
            Character(id="char_1", name="林晚", visual_prompt="a young Chinese woman, long hair"),
            Character(id="char_2", name="赵明远", visual_prompt="a middle-aged man in a suit"),
        ],
        settings=[Setting(id="loc_1", name="办公室", visual_prompt="glass-walled office")],
    )
    shots = [
        _shot("s1", "scene_1", 3, camera="中景 · 固定", video_prompt="she looks up",
              character_in_frame=["char_1"], location="loc_1", dialogue_text="这是我的方案。"),
        _shot("s2", "scene_1", 4, camera="特写 · 推", video_prompt="he smirks",
              character_in_frame=["char_2"], location="loc_1"),
    ]
    prompt = video_service.build_segment_prompt(shots, Project(title="t"), bible)
    assert "2 shots joined by hard cuts, 7 seconds" in prompt
    assert "Shot 1 (0-3s): medium shot" in prompt and "Shot 2 (3-7s): close-up" in prompt
    assert "这是我的方案。" in prompt
    assert prompt.count("glass-walled office") == 1
    assert "a young Chinese woman" in prompt and "a middle-aged man" in prompt


def test_segment_references_union_characters_first(tmp_path, settings):
    refs = tmp_path / "refs"
    refs.mkdir()
    for name in ("char_1", "char_2", "loc_1"):
        (refs / f"{name}.png").write_bytes(b"png")
    bible = StoryBible(
        characters=[
            Character(id="char_1", name="a", reference_image="refs/char_1.png"),
            Character(id="char_2", name="b", reference_image="refs/char_2.png"),
        ],
        settings=[Setting(id="loc_1", name="o", reference_image="refs/loc_1.png")],
    )
    shots = [
        _shot("s1", "scene_1", 3, character_in_frame=["char_2"], location="loc_1"),
        _shot("s2", "scene_1", 3, character_in_frame=["char_1", "char_2"]),
    ]

    class Refs:
        supports_reference_image = True

    paths, _ = video_service.resolve_segment_references(shots, bible, tmp_path, Refs())
    assert [p.stem for p in paths] == ["char_2", "char_1", "loc_1"]


# ── 切分 ──────────────────────────────────────────────


async def _two_color_video(path: Path, first: float, second: float) -> None:
    """红 first 秒 + 蓝 second 秒，中间一个真实硬切。"""
    await run_ffmpeg(
        [
            "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=red:s=320x240:d={first}:r=24",
            "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={second}:r=24",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ]
    )


@requires_ffmpeg
@pytest.mark.asyncio
async def test_split_snaps_to_real_cut(tmp_path):
    source = tmp_path / "seg.mp4"
    await _two_color_video(source, 3.5, 2.5)
    snapped = await video_service.split_segment(source, [3, 3], snap=True)
    assert abs(snapped[0][1] - 3.5) < 0.1 and abs(snapped[1][0] - 3.5) < 0.1
    planned = await video_service.split_segment(source, [3, 3], snap=False)
    assert planned[0] == (0.0, 3.0)


def test_match_cuts_pairs_in_order_when_counts_agree():
    # Seedance 实测：计划 4 / 7 秒切，模型切在 2.54 / 5.96 —— 超出 ±1 秒窗口也要对上
    assert video_service._match_cuts([4.0, 7.0], [2.542, 5.958], 10.0) == [2.542, 5.958]
    assert video_service._match_cuts([4.0, 7.0], [5.417, 7.333], 10.0) == [5.417, 7.333]


def test_match_cuts_falls_back_to_window_when_counts_differ():
    # 少切一刀：能吸附的吸附，吸附不到的按计划
    assert video_service._match_cuts([3.0, 7.0], [3.2], 10.0) == [3.2, 7.0]
    # 多出一个片头闪光（0.1 秒，太靠边被忽略）后数量对上
    assert video_service._match_cuts([3.0, 7.0], [0.1, 3.1, 7.2], 10.0) == [3.1, 7.2]
    # 三刀对两个计划切点：窗口吸附，远处那刀不要
    assert video_service._match_cuts([3.0, 7.0], [3.1, 5.0, 7.2], 10.0) == [3.1, 7.2]
    assert video_service._match_cuts([3.0, 7.0], [], 10.0) == [3.0, 7.0]


@requires_ffmpeg
@pytest.mark.asyncio
async def test_split_uses_far_cut_when_it_is_the_only_one(tmp_path):
    source = tmp_path / "seg.mp4"
    await _two_color_video(source, 4.6, 1.4)  # 计划 3 秒切，模型 4.6 秒才切
    pieces = await video_service.split_segment(source, [3, 3], snap=True)
    assert abs(pieces[0][1] - 4.6) < 0.1 and abs(pieces[1][0] - 4.6) < 0.1


@requires_ffmpeg
@pytest.mark.asyncio
async def test_resplit_segments_recuts_without_provider(tmp_path):
    source = tmp_path / "clips" / "segments" / "seg_1.mp4"
    source.parent.mkdir(parents=True)
    await _two_color_video(source, 4.6, 1.4)
    storyboard = Storyboard(
        shots=[
            _shot("s1", "scene_1", 3, video_prompt="a"),
            _shot("s2", "scene_1", 3, video_prompt="b"),
        ]
    )
    manifest = VideoManifest(
        clips=[
            VideoClip(
                shot_id=sid,
                scene_id="scene_1",
                status=ClipStatus.READY,
                path=f"clips/{sid}.mp4",
                segment_id="seg_1",
                segment_path="clips/segments/seg_1.mp4",
                segment_offset_sec=offset,
            )
            for sid, offset in (("s1", 0.0), ("s2", 3.0))
        ]
    )
    preview = await video_service.resplit_segments(
        storyboard, manifest, tmp_path, tmp_path / "clips", dry_run=True
    )
    assert abs(preview[1]["new_offset"] - 4.6) < 0.1
    assert manifest.get("s2").segment_offset_sec == 3.0, "预览不改清单"
    assert not (tmp_path / "clips" / "s2.mp4").exists()

    await video_service.resplit_segments(storyboard, manifest, tmp_path, tmp_path / "clips")
    assert abs(manifest.get("s2").segment_offset_sec - 4.6) < 0.1
    length = await probe_duration(tmp_path / "clips" / "s1.mp4")
    assert length is not None and abs(length - 4.6) < 0.1


def test_storyboard_impact_only_flags_the_segment_that_changed():
    from videogen.models.video import MediaKind

    old = _segmented_storyboard()  # scene_1 三镜一段 + scene_2 一镜一段
    manifest = VideoManifest(
        clips=[
            VideoClip(
                shot_id=s.id,
                scene_id=s.scene_id,
                status=ClipStatus.READY,
                media_kind=MediaKind.VIDEO,
                segment_id=s.segment_id,
            )
            for s in old.shots
        ]
    )
    new = old.model_copy(deep=True)
    new.shots[3].video_prompt = "a different take"
    new.shots[0].voice_script_source = "llm:perform"  # 不影响画面
    impact = video_service.storyboard_impact(old, new, manifest)
    assert impact["stale_shots"] == ["scene_2_shot_1"]
    assert impact["kept_segments"] == 1

    # 段内镜头顺序变了：整段重做
    swapped = old.model_copy(deep=True)
    swapped.shots[0], swapped.shots[1] = swapped.shots[1], swapped.shots[0]
    impact = video_service.storyboard_impact(old, swapped, manifest)
    assert sorted(impact["stale_shots"]) == ["scene_1_shot_1", "scene_1_shot_2", "scene_1_shot_3"]


class CountingMock(MockVideoProvider):
    """真出片（ffmpeg）+ 记录请求 + 模拟 provider 实报扣费。"""

    def __init__(self, cost: float = 0.5) -> None:
        super().__init__(fps=12, crf=35)
        self.requests: list[VideoRequest] = []
        self.cost = cost

    async def generate(self, request: VideoRequest) -> GeneratedClip:
        self.requests.append(request)
        clip = await super().generate(request)
        return clip.model_copy(update={"actual_cost_usd": self.cost})


def _segmented_storyboard() -> Storyboard:
    storyboard = Storyboard(
        shots=[
            _shot("scene_1_shot_1", "scene_1", 3, video_prompt="a"),
            _shot("scene_1_shot_2", "scene_1", 2, video_prompt="b"),
            _shot("scene_1_shot_3", "scene_1", 3, video_prompt="c"),
            _shot("scene_2_shot_1", "scene_2", 4, video_prompt="d"),
        ]
    )
    storyboard_service.assign_segments(storyboard, target_sec=12, max_sec=15)
    return storyboard


@requires_ffmpeg
@pytest.mark.asyncio
async def test_generate_clips_one_call_per_segment_and_cost_once(tmp_path):
    provider = CountingMock()
    project = Project(title="t")
    manifest = await video_service.generate_clips(
        project, _segmented_storyboard(), provider, tmp_path / "clips", tmp_path,
        retry_backoff_sec=0,
    )
    assert [r.shot_id for r in sorted(provider.requests, key=lambda r: r.shot_id)] == [
        "scene_1_seg_1",
        "scene_2_shot_1",  # 单镜头的段走原来的单镜路径
    ]
    seg_request = next(r for r in provider.requests if r.shot_id == "scene_1_seg_1")
    assert seg_request.duration_sec == 8 and "Shot 3 (5-8s)" in seg_request.prompt

    assert manifest.is_usable
    clips = {c.shot_id: c for c in manifest.clips}
    for sid, expected in (("scene_1_shot_1", 3), ("scene_1_shot_2", 2), ("scene_1_shot_3", 3)):
        clip = clips[sid]
        assert clip.segment_id == "scene_1_seg_1" and clip.segment_path
        actual = await probe_duration(tmp_path / clip.path)
        assert actual is not None and abs(actual - expected) < 0.3
    # 一段只记一次扣费：第一镜带着整段的钱，其余为 0
    assert clips["scene_1_shot_1"].cost_usd == 0.5
    assert clips["scene_1_shot_2"].cost_usd == 0 and clips["scene_1_shot_3"].cost_usd == 0


@requires_ffmpeg
@pytest.mark.asyncio
async def test_split_failure_keeps_segment_and_retry_does_not_rebuy(tmp_path, monkeypatch):
    provider = CountingMock()
    project = Project(title="t")
    storyboard = _segmented_storyboard()
    real_cut = video_service.cut_clip

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(video_service, "cut_clip", broken)
    manifest = await video_service.generate_clips(
        project, storyboard, provider, tmp_path / "clips", tmp_path, retry_backoff_sec=0
    )
    failed = [c for c in manifest.clips if c.segment_id == "scene_1_seg_1"]
    assert all(c.status == ClipStatus.FAILED for c in failed)
    assert "不重新计费" in failed[0].error and failed[0].cost_usd == 0.5
    calls = len(provider.requests)

    monkeypatch.setattr(video_service, "cut_clip", real_cut)
    manifest = await video_service.generate_clips(
        project, storyboard, provider, tmp_path / "clips", tmp_path,
        existing=manifest, retry_backoff_sec=0,
    )
    assert len(provider.requests) == calls  # 直接重切，没有重新提交
    assert manifest.is_usable


# ── 编排：分镜阶段编段 / 预算按段 ─────────────────────


@pytest.mark.asyncio
async def test_storyboard_generation_assigns_segments_and_tells_llm(store):
    llm = FakeLLM()
    pipeline = Pipeline(store, llm_factory=lambda: llm)
    project = Project(title="段落", input_text="一个逆袭故事", target_duration_sec=30)
    store.save_project(project)
    await pipeline.generate_bible(project.id)
    await pipeline.generate_script(project.id)
    await pipeline.confirm_script(project.id, force=True)
    storyboard = await pipeline.generate_storyboard(project.id)

    assert storyboard.shots and all(s.segment_id for s in storyboard.shots)
    assert len(storyboard.segment_ids()) < len(storyboard.shots)
    prompts = [str(c[-1].content) for c in llm.calls if "专业分镜师" in str(c[0].content)]
    assert prompts and all("段落节奏" in p for p in prompts)


@pytest.mark.asyncio
async def test_preflight_and_budget_bill_per_segment(store, settings, monkeypatch):
    video = FakeVideoProvider(duration_options=[5, 10, 15], price_usd_per_sec=0.1)
    pipeline = Pipeline(store, llm_factory=FakeLLM, video_factory=lambda: video)
    project = Project(title="t", status=ProjectStatus.STORYBOARD_CONFIRMED)
    store.save_project(project)
    store.save_storyboard(
        project.id,
        Storyboard(shots=[_shot(f"scene_1_shot_{i}", "scene_1", 3) for i in (1, 2, 3)]),
    )
    plan = (await pipeline.preflight(project.id, action="video"))["video"]
    assert plan["segment_mode"] and plan["segments_total"] == 1
    assert plan["billed_seconds"] == 10  # 9 秒的一段抬到 10 秒档；逐镜要买 3×5=15 秒
    assert plan["merge_plan"] == {}

    monkeypatch.setattr(settings, "video_segment_mode", False)
    plan = (await pipeline.preflight(project.id, action="video"))["video"]
    assert plan["billed_seconds"] == 15 and not plan["segment_mode"]


@requires_ffmpeg
@pytest.mark.asyncio
async def test_segment_mode_full_media_chain_composes(store):
    """段落模式下视频 → 配音（会拉长镜头）→ 合成整条链路都能跑通，且模型只被调一次。"""
    from tests.conftest import wait_for_job
    from tests.factories import small_project
    from tests.fakes import FakeBgmLibrary
    from videogen.providers.tts.mock import MockTTSProvider

    video = CountingMock(cost=0.0)
    pipeline = Pipeline(
        store,
        llm_factory=FakeLLM,
        video_factory=lambda: video,
        tts_factory=MockTTSProvider,
        bgm_factory=FakeBgmLibrary,
    )
    project = small_project(store, shots=3, shot_duration=2, dialogue="好的，我知道了。")
    for step in (pipeline.start_video_generation, pipeline.start_audio_generation):
        job = await step(project.id)
        finished = await wait_for_job(pipeline, job.id, timeout=120)
        assert finished.status.value == "succeeded", finished.error
    assert [r.shot_id for r in video.requests] == ["scene_1_seg_1"]

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    assert len(report.segments) == 3
    assert store.load_project(project.id).status == ProjectStatus.DONE


class CountingTTS:
    """记一下有没有人调 TTS（原声模式下一次都不该调）。"""

    calls = 0

    def __init__(self) -> None:
        from videogen.providers.tts.mock import MockTTSProvider

        self._inner = MockTTSProvider()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def synthesize(self, *args, **kwargs):
        CountingTTS.calls += 1
        return await self._inner.synthesize(*args, **kwargs)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_native_dub_mode_skips_tts_and_passes_segments_through(store, settings, monkeypatch):
    """默认原声模式：配音阶段不调 TTS、$0；合成整段直通（不切镜重编码）。"""
    from tests.conftest import wait_for_job
    from tests.factories import small_project
    from tests.fakes import FakeBgmLibrary

    monkeypatch.setattr(settings, "audio_dub_mode", "native")
    monkeypatch.setattr(settings, "compose_keep_segments", True)
    CountingTTS.calls = 0
    pipeline = Pipeline(
        store,
        llm_factory=FakeLLM,
        video_factory=lambda: CountingMock(cost=0.0),
        tts_factory=CountingTTS,
        bgm_factory=FakeBgmLibrary,
    )
    project = small_project(store, shots=3, shot_duration=2, dialogue="好的，我知道了。")

    pre = await pipeline.preflight(project.id, action="audio")
    assert pre["audio"]["estimated_usd"] == 0 and pre["audio"]["dub_mode"] == "native"

    for step in (pipeline.start_video_generation, pipeline.start_audio_generation):
        job = await step(project.id)
        finished = await wait_for_job(pipeline, job.id, timeout=120)
        assert finished.status.value == "succeeded", finished.error
    assert CountingTTS.calls == 0
    audio = store.load_audio_manifest(project.id)
    assert audio.provider == "native" and not audio.clips

    job = await pipeline.start_compose(project.id)
    finished = await wait_for_job(pipeline, job.id, timeout=180)
    assert finished.status.value == "succeeded", finished.error
    report = store.load_compose_report(project.id)
    assert any("整段直通：1 段（3 个镜头）" in w for w in report.warnings), report.warnings
    # mock 片段没有音轨：原声模式必须明说这些镜头是无声的
    assert any("没有音轨" in w for w in report.warnings), report.warnings
    rendered = {p.name for p in store.segments_dir(project.id).glob("*.mp4")}
    assert "scene_1_seg_1.mp4" in rendered and "scene_1_shot_1.mp4" not in rendered
    final = store.ensure_project_dir(project.id) / report.output_path
    assert abs((await probe_duration(final)) - report.duration_sec) < 0.2


@requires_ffmpeg
@pytest.mark.asyncio
async def test_project_can_opt_into_tts_dub_mode(store, settings, monkeypatch):
    from tests.conftest import wait_for_job
    from tests.factories import small_project
    from tests.fakes import FakeBgmLibrary

    monkeypatch.setattr(settings, "audio_dub_mode", "native")
    CountingTTS.calls = 0
    pipeline = Pipeline(
        store,
        llm_factory=FakeLLM,
        video_factory=lambda: CountingMock(cost=0.0),
        tts_factory=CountingTTS,
        bgm_factory=FakeBgmLibrary,
    )
    project = small_project(store, shots=2, shot_duration=2, dialogue="好的，我知道了。")
    await pipeline.update_project(project.id, {"dub_mode": "tts"})
    for step in (pipeline.start_video_generation, pipeline.start_audio_generation):
        job = await step(project.id)
        finished = await wait_for_job(pipeline, job.id, timeout=120)
        assert finished.status.value == "succeeded", finished.error
    assert CountingTTS.calls == 2
    job = await pipeline.start_compose(project.id)
    await wait_for_job(pipeline, job.id, timeout=180)
    report = store.load_compose_report(project.id)
    assert not any("整段直通" in w for w in report.warnings), "有 TTS 配音的段不能直通"


def test_segment_prompt_drops_no_cut_directive_but_keeps_the_rest(settings):
    """单镜表演要求里的「single continuous take with no cuts」和段内硬切自相矛盾。"""
    shots = [_shot("s1", "scene_1", 3, video_prompt="a door"),
             _shot("s2", "scene_1", 3, video_prompt="a bed")]
    prompt = video_service.build_segment_prompt(shots, Project(title="t"), StoryBible())
    assert "no cuts" not in prompt and "continuous take" not in prompt
    assert "natural blinking and breathing" in prompt and "no morphing" in prompt
    single = video_service.build_prompt(shots[0], Project(title="t"), StoryBible())
    assert "single continuous take with no cuts" in single  # 单镜生成照旧


def test_offscreen_voice_is_not_lip_synced_on_screen(settings):
    """门外喊话：prompt 写「画外音」，不再要求画面里的人对口型。"""
    shot = _shot("s1", "scene_1", 3, video_prompt="a closed door", dialogue_text="格里高！快开门！")
    shot.dialogue_speaker = "char_3"
    shot.voice_offscreen = True
    prompt = video_service.build_segment_prompt([shot], Project(title="t"), StoryBible())
    assert "off-screen voice" in prompt and "on-screen speaker" not in prompt
    assert "格里高！快开门！" in prompt


def test_narration_goes_to_video_model_only_when_enabled(settings, monkeypatch):
    """原生直出：旁白写进 prompt 让模型念（画外音、没人动嘴）；默认关，避免和 TTS 旁白叠两层。"""
    shot = _shot("s1", "scene_1", 3, video_prompt="a papermaker at a vat",
                 narration="两手持帘入水一荡。")
    talk = _shot("s2", "scene_1", 3, video_prompt="he speaks", dialogue_text="对呀对呀！",
                 narration="不该被念")
    prompt = video_service.build_segment_prompt([shot, talk], Project(title="t"), StoryBible())
    assert "两手持帘入水一荡" not in prompt

    monkeypatch.setattr(settings, "video_prompt_narration", True)
    monkeypatch.setattr(settings, "video_narrator_voice", "a calm female narrator")
    prompt = video_service.build_segment_prompt([shot, talk], Project(title="t"), StoryBible())
    said = 'a calm female narrator says this exact Mandarin narration off-screen: "两手持帘入水'
    assert said in prompt
    assert "nobody on screen speaks" in prompt
    assert "不该被念" not in prompt  # 有台词的镜头只念台词
    single = video_service.build_prompt(shot, Project(title="t"), StoryBible())
    assert "两手持帘入水一荡" in single
