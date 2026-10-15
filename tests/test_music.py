"""配乐作曲与合成（`utils/music.py`）的单元测试。

这一层的价值是「音乐真的跟着画面走」，所以测试也主要盯着这件事：
段落边界对不对齐镜头、重音落不落在动作爆发的时刻、有人说话时配器有没有让路、
同一个输入是不是同一首曲子，以及最后出来的到底是不是一个合法的 WAV。
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import pytest

from videogen.models.music import MusicPlan, MusicSection
from videogen.utils import music
from videogen.utils.motion import MotionAnalysis, MotionCurve

SR = music.SR


class Shot:
    """分镜镜头的最小替身（只需要作曲用到的几个字段）。"""

    def __init__(
        self,
        shot_id: str,
        *,
        duration: float = 6.0,
        mood: str = "紧张",
        dialogue: str = "",
        scene_id: str = "scene_1",
    ):
        self.id = shot_id
        self.scene_id = scene_id
        self.duration_sec = duration
        self.mood = mood
        self.dialogue_text = dialogue


def motion(*, mean: float = 0.03, peak: float = 0.06, peak_at: int = 6, count: int = 36):
    energy = [mean] * count
    energy[min(peak_at, count - 1)] = peak
    curve = MotionCurve(fps=6.0, size=64, energy=energy)
    return MotionAnalysis(fps=6.0, size=64, frames=count, overall=curve, subject=curve)


# ── 情绪 → 色调 ────────────────────────────────────────


def test_pick_color_counts_each_mood_once():
    """「悲伤」两个字不能被当成两个情绪，压过「紧张 + 激烈」。"""
    color = music.pick_color(["悲伤", "紧张", "激烈", "追逐"])
    assert color.key in {"tense", "action"}


def test_pick_color_single_mood():
    assert music.pick_color(["温馨的日常"]).key == "warm"
    assert music.pick_color(["悬疑"]).key == "mystery"
    assert music.pick_color(["喜剧"]).key == "playful"


def test_pick_color_override_and_free_text():
    assert music.pick_color(["紧张"], override="sad").key == "sad"
    # 中文关键词也能覆盖
    assert music.pick_color(["紧张"], override="轻快").key == "playful"
    # 用户要求参与判断（没有分镜情绪时全靠它）
    assert music.pick_color([], note="整片要压抑一点").key == "sad"
    assert music.pick_color([]).key == "neutral"


def test_note_to_freq_anchors():
    assert music.note_to_freq(69) == pytest.approx(440.0)
    assert music.note_to_freq(57) == pytest.approx(220.0)
    # 单位是半音：+12 半音 = 频率翻倍
    assert music.note_to_freq(81) == pytest.approx(880.0)


# ── 配乐谱 ─────────────────────────────────────────────


def test_plan_sections_align_to_shots():
    shots = [
        Shot("s1", duration=4, mood="压抑"),
        Shot("s2", duration=4, mood="压抑"),
        Shot("s3", duration=6, mood="悲伤", scene_id="scene_2"),
    ]
    plan = music.build_plan(shots=shots, duration_sec=14, seed=1, moods=["压抑", "悲伤"])
    starts = [section.start_sec for section in plan.sections]
    assert starts[0] == 0.0
    # 段落起点必须落在镜头边界上（音乐换和弦 = 画面切镜）
    boundaries = {0.0, 4.0, 8.0, 14.0}
    for section in plan.sections:
        assert section.start_sec in boundaries
        assert section.end_sec in boundaries
    assert plan.sections[-1].end_sec == pytest.approx(14.0)


def test_plan_first_section_is_thin_and_last_resolves():
    shots = [Shot(f"s{i}", duration=5, mood="激烈") for i in range(1, 5)]
    plan = music.build_plan(shots=shots, duration_sec=20, seed=3, moods=["激烈"])
    assert plan.sections[0].energy <= 1
    assert plan.sections[0].label == "intro"
    assert plan.sections[-1].label in {"resolve", "climax", "build", "bed"}


def test_plan_accents_follow_motion_bursts_and_cuts():
    shots = [Shot("s1", duration=6), Shot("s2", duration=6), Shot("s3", duration=6)]
    motions = {"s2": motion(mean=0.05, peak=0.18, peak_at=12)}
    plan = music.build_plan(
        shots=shots, motions=motions, duration_sec=18, seed=5, moods=["激烈", "动作"]
    )
    assert plan.accents, "应该至少有一个重音"
    hits = [accent for accent in plan.accents if accent.kind == "hit"]
    # 动作爆发落在 s2 的第 2 秒 → 全局 8 秒附近
    if hits:
        assert any(7.5 <= accent.at_sec <= 8.5 for accent in hits)
        assert all("画面动作" in accent.reason for accent in hits)
    # 段落开始的换镜头重音
    assert all(accent.at_sec >= 0 for accent in plan.accents)


def test_plan_accents_avoid_speech_windows():
    shots = [Shot("s1", duration=8, dialogue="你别走"), Shot("s2", duration=8, dialogue="我恨你")]
    speech = [(0.4, 3.0), (8.4, 11.0)]
    plan = music.build_plan(
        shots=shots,
        speech_windows=speech,
        motions={"s1": motion(peak=0.2, peak_at=6), "s2": motion(peak=0.2, peak_at=6)},
        duration_sec=16,
        seed=7,
        moods=["激烈"],
    )
    for accent in plan.accents:
        for start, end in speech:
            assert not (start <= accent.at_sec <= end)


def test_plan_dialogue_thins_layers():
    shots = [Shot("s1", duration=8, dialogue="你别走"), Shot("s2", duration=8, dialogue="")]
    plan = music.build_plan(
        shots=shots,
        speech_windows=[(0.4, 2.6)],
        motions={"s1": motion(mean=0.06, peak=0.15), "s2": motion(mean=0.06, peak=0.15)},
        duration_sec=16,
        seed=9,
        moods=["激烈", "动作"],
    )
    talking = [section for section in plan.sections if section.dialogue]
    assert talking, "应该识别出有人说话的段落"
    for section in talking:
        assert "perc" not in section.layers
        assert "bells" not in section.layers


def test_plan_is_deterministic_and_seed_sensitive():
    shots = [Shot(f"s{i}", duration=6, mood="紧张") for i in range(1, 5)]

    def build(seed: int) -> MusicPlan:
        return music.build_plan(shots=shots, duration_sec=24, seed=seed, moods=["紧张"])

    first, again = build(11), build(11)
    assert first.model_dump(exclude={"created_at"}) == again.model_dump(exclude={"created_at"})
    assert build(12).model_dump(exclude={"created_at"}) != first.model_dump(
        exclude={"created_at"}
    )


def test_plan_without_shots_still_covers_duration():
    plan = music.build_plan(duration_sec=12, seed=1, moods=["温暖"])
    assert plan.sections
    assert plan.sections[-1].end_sec == pytest.approx(12.0)
    assert plan.notes


def test_plan_reads_llm_section_hints():
    shots = [Shot(f"s{i}", duration=5, mood="压抑") for i in range(1, 5)]
    plan = music.build_plan(
        shots=shots,
        duration_sec=20,
        seed=2,
        moods=["压抑"],
        section_hint=[
            {"color": "action", "mood": "激烈", "energy": 3},
            {"mood": "悲伤", "energy": 0.3},
        ],
    )
    assert plan.color == "action"
    assert plan.source == "llm"
    assert any("LLM" in line for line in plan.notes)
    # 第二段能量 0.3 应换算成 1 档
    assert plan.sections[1].energy <= 1


def test_plan_rejects_garbage_hints():
    plan = music.build_plan(
        shots=[Shot("s1", duration=5)],
        duration_sec=5,
        seed=1,
        moods=["温暖"],
        section_hint=["不是字典", {"color": "不存在", "energy": "很响"}],
    )
    assert plan.color == "warm"


# ── 合成 ──────────────────────────────────────────────


def _read_wav(path: Path) -> tuple[wave._wave_params, bytes]:  # type: ignore[attr-defined]
    with wave.open(str(path), "rb") as handle:
        return handle.getparams(), handle.readframes(handle.getnframes())


def test_render_plan_writes_valid_stereo_wav(tmp_path):
    shots = [Shot("s1", duration=5, mood="温暖"), Shot("s2", duration=5, mood="希望")]
    plan = music.build_plan(shots=shots, duration_sec=10, seed=4, moods=["温暖", "希望"])
    out = tmp_path / "bgm.wav"
    stats = music.render_plan(plan, out)

    params, raw = _read_wav(out)
    assert params.nchannels == 2
    assert params.sampwidth == 2
    assert params.framerate == SR
    assert params.nframes == pytest.approx(int((10 + music.TAIL_SEC) * SR), abs=2)
    assert stats["notes"] > 0
    assert 0.02 < stats["peak"] <= 1.0
    assert -60 < stats["rms_db"] < 0
    assert raw


def test_render_plan_is_not_silent_and_has_variation(tmp_path):
    """不是「有声音就行」：要能量有起伏（全曲恒定音量 = 没在跟画面走）。"""
    shots = [
        Shot("s1", duration=4, mood="压抑"),
        Shot("s2", duration=4, mood="激烈"),
        Shot("s3", duration=4, mood="激烈"),
    ]
    plan = music.build_plan(
        shots=shots,
        motions={"s1": motion(mean=0.004, peak=0.005), "s2": motion(mean=0.06, peak=0.14)},
        duration_sec=12,
        seed=21,
        moods=["压抑", "激烈"],
    )
    out = tmp_path / "bgm.wav"
    music.render_plan(plan, out)
    with wave.open(str(out), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    samples = memoryview(raw).cast("h")
    half = len(samples) // 2

    def rms(values) -> float:
        total = 0.0
        count = 0
        for index in range(0, len(values), 97):  # 抽样即可
            value = values[index] / 32768.0
            total += value * value
            count += 1
        return math.sqrt(total / max(1, count))

    quiet, loud = rms(samples[:half]), rms(samples[half:])
    assert quiet > 0.0 and loud > 0.0
    assert loud > quiet, "高潮段应该比铺垫段更满"


def test_render_plan_rejects_wrong_sample_rate(tmp_path):
    plan = music.build_plan(shots=[Shot("s1", duration=2)], duration_sec=2, seed=1)
    with pytest.raises(ValueError, match="44100"):
        music.render_plan(plan, tmp_path / "x.wav", sr=48000)


def test_melody_skips_speech_windows():
    plan = MusicPlan(duration_sec=8, speech_windows=[(0.0, 8.0)], bpm=90, sections=[])
    section = MusicSection(index=0, start_sec=0.0, end_sec=8.0, energy=2, layers=["lead"])
    import array
    import random

    left = array.array("d", bytes(8 * SR * 9))
    right = array.array("d", bytes(8 * SR * 9))
    played = music._render_melody(
        left,
        right,
        plan,
        section,
        music.COLORS["warm"],
        60,
        random.Random(1),
        bars=4,
        bar=2.0,
        beat=0.5,
        start=0.0,
        chord_at=lambda index: music.MusicChord(root=0, quality="M", name="C"),
    )
    assert played == 0, "整段都在说话时不该写旋律"


def test_make_impulse_response(tmp_path):
    path = tmp_path / "ir.wav"
    music.make_impulse_response(path, seconds=0.5, seed=3)
    params, raw = _read_wav(path)
    assert params.nchannels == 2
    assert params.framerate == SR
    assert params.nframes == pytest.approx(int(0.5 * SR), abs=int(0.05 * SR))
    assert any(raw), "脉冲响应不能是全零（否则等于没有混响）"


def test_envelope_decays_and_fades():
    inst = music.INSTRUMENTS["piano"]
    env = music._envelope(inst, SR)  # 1 秒
    assert env[0] == 0.0
    assert max(env) <= 1.0
    assert env[-1] == 0.0
    assert env[SR // 2] > env[-2], "指数衰减：后面应该更小"


def test_out_of_range_bpm_and_unknown_key_are_reported_not_ignored():
    """回归：BPM 超出范围 / 调写错时以前静默用默认值，用户以为设置生效了。"""
    from videogen.utils import music

    fast = music.build_plan(duration_sec=10, bpm=1000)
    assert fast.bpm == 180 and any("BPM 1000" in n for n in fast.notes)
    odd = music.build_plan(duration_sec=10, key="Z#m")
    assert any("Z#m" in n for n in odd.notes)
