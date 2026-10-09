"""角色一致性（参考图 / seed）与多模型切换。"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from tests.conftest import wait_for_job
from tests.fakes import FakeLLM, FakeVideoProvider
from videogen.config import get_settings
from videogen.models.bible import Character, StoryBible
from videogen.models.project import Project, ProjectStatus
from videogen.models.script import DialogueLine, Scene, Script
from videogen.models.storyboard import Shot, Storyboard
from videogen.orchestrator.pipeline import Pipeline, PipelineError
from videogen.services import consistency_service, script_service, video_service
from videogen.utils.ffmpeg import ffmpeg_available
from videogen.utils.file_store import FileStore

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes").decode()


def project_with_character(store: FileStore, *, reference: str = "") -> Project:
    project = Project(title="一致性", input_text="测试", target_duration_sec=10)
    project.status = ProjectStatus.STORYBOARD_CONFIRMED
    store.save_project(project)
    store.save_bible(
        project.id,
        StoryBible(
            characters=[
                Character(
                    id="char_1",
                    name="林晚",
                    role="主角",
                    description="女白领",
                    reference_image=reference,
                )
            ]
        ),
    )
    store.save_storyboard(
        project.id,
        Storyboard(
            shots=[
                Shot(
                    id="scene_1_shot_1",
                    scene_id="scene_1",
                    shot_no=1,
                    duration_sec=1,
                    video_prompt="medium shot",
                    character_in_frame=["char_1"],
                )
            ]
        ),
    )
    return project


def make_pipeline(store: FileStore, video) -> Pipeline:
    return Pipeline(store, llm_factory=lambda: FakeLLM(), video_factory=lambda: video)


# ── 参考图解析 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reference_image_is_passed_to_provider(store):
    project = project_with_character(store, reference="refs/char_1.png")
    ref = store.refs_dir(project.id) / "char_1.png"
    ref.write_bytes(b"reference-image")

    provider = FakeVideoProvider(supports_reference_image=True)
    pipeline = make_pipeline(store, provider)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.reference_image is not None
    assert request.reference_image.name == "char_1.png"
    assert request.reference_image.is_file()


@pytest.mark.asyncio
async def test_reference_image_ignored_when_provider_unsupported(store):
    project = project_with_character(store, reference="refs/char_1.png")
    (store.refs_dir(project.id) / "char_1.png").write_bytes(b"x")

    provider = FakeVideoProvider(supports_reference_image=False)
    pipeline = make_pipeline(store, provider)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert provider.requests[0].reference_image is None


@pytest.mark.asyncio
async def test_video_seed_is_passed(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "video_seed", 4242)
    project = project_with_character(store)

    provider = FakeVideoProvider()
    pipeline = make_pipeline(store, provider)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert provider.requests[0].seed == 4242


@pytest.mark.asyncio
async def test_character_seed_overrides_global(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "video_seed", 1)
    project = project_with_character(store, reference="refs/char_1.png")
    (store.refs_dir(project.id) / "char_1.png").write_bytes(b"x")
    bible = store.load_bible(project.id)
    bible.characters[0] = bible.characters[0].model_copy(update={"seed": 777})
    store.save_bible(project.id, bible)

    provider = FakeVideoProvider(supports_reference_image=True)
    pipeline = make_pipeline(store, provider)
    job = await pipeline.start_video_generation(project.id)
    await wait_for_job(pipeline, job.id)

    assert provider.requests[0].seed == 777


def test_missing_reference_file_falls_back(store):
    project = project_with_character(store, reference="refs/nope.png")
    bible = store.load_bible(project.id)
    shot = store.load_storyboard(project.id).shots[0]

    reference, seed = video_service.resolve_reference(
        shot,
        bible,
        store.ensure_project_dir(project.id),
        FakeVideoProvider(supports_reference_image=True),
        0,
    )
    assert reference is None and seed is None


def test_reference_outside_project_dir_is_rejected(store):
    project = project_with_character(store, reference="../../../etc/passwd")
    bible = store.load_bible(project.id)
    shot = store.load_storyboard(project.id).shots[0]

    reference, _ = video_service.resolve_reference(
        shot,
        bible,
        store.ensure_project_dir(project.id),
        FakeVideoProvider(supports_reference_image=True),
    )
    assert reference is None


def test_seed_is_passed_even_without_reference_support(store):
    """seed 是通用参数：provider 不支持参考图也不影响 seed 透传。"""
    project = project_with_character(store, reference="refs/char_1.png")
    (store.refs_dir(project.id) / "char_1.png").write_bytes(b"x")
    shot = store.load_storyboard(project.id).shots[0]

    reference, seed = video_service.resolve_reference(
        shot, store.load_bible(project.id), store.ensure_project_dir(project.id),
        FakeVideoProvider(supports_reference_image=False), 99,
    )
    assert reference is None
    assert seed == 99  # seed 是通用参数，与参考图无关


@pytest.mark.asyncio
async def test_mock_provider_tints_by_reference_image(tmp_path):
    """mock provider 用参考图取色：换了参考图，画面颜色必须变（证明参数真的传到了）。"""
    from videogen.providers.video.base import VideoRequest
    from videogen.providers.video.mock import MockVideoProvider

    if not ffmpeg_available():
        pytest.skip("环境里没有 ffmpeg")

    ref_a = tmp_path / "a.png"
    ref_b = tmp_path / "b.png"
    ref_a.write_bytes(b"reference-A")
    ref_b.write_bytes(b"reference-B")

    provider = MockVideoProvider()
    clip_a = await provider.generate(
        VideoRequest(
            shot_id="s1",
            prompt="p",
            duration_sec=1,
            output_path=tmp_path / "a.mp4",
            reference_images=[ref_a],
        )
    )
    clip_b = await provider.generate(
        VideoRequest(
            shot_id="s1",
            prompt="p",
            duration_sec=1,
            output_path=tmp_path / "b.mp4",
            reference_images=[ref_b],
        )
    )
    assert clip_a.path.read_bytes() != clip_b.path.read_bytes()


# ── 严格模式：结构硬伤拦确认 ─────────────────────────


def _script(*scenes: Scene) -> Script:
    return Script(title="t", scenes=list(scenes))


def test_strict_mode_flags_duplicate_scene_as_error():
    script = _script(
        Scene(id="scene_1", heading="INT. 办公室 - 日", summary="林悦被裁员"),
        Scene(id="scene_2", heading="INT. 办公室 - 日", summary="林悦被裁员"),  # 一字不差
    )
    bible = StoryBible(characters=[Character(id="char_1", name="林悦", role="主角")])

    lenient = consistency_service.validate_script(script, bible)
    strict = consistency_service.validate_script(script, bible, strict=True)

    assert not consistency_service.has_errors(lenient)  # 宽松模式只提示
    errors = consistency_service.has_errors(strict)
    assert errors and any("没有推进" in issue.message for issue in errors)


def test_strict_mode_keeps_question_ending_as_warning_only():
    """结尾带问号**不能**拦确认 —— 正常台词里问句太多了，这是提示不是证据。"""
    script = _script(
        Scene(id="scene_1", heading="INT. - 日", summary="她决定反击"),
        Scene(id="scene_2", heading="INT. - 夜", summary="她真的能翻盘吗？"),  # 结尾悬着
    )
    bible = StoryBible(characters=[Character(id="char_1", name="林悦", role="主角")])
    report = consistency_service.validate_script(script, bible, strict=True)
    assert not consistency_service.has_errors(report)
    hints = [i for i in report.issues if i.category == "structure"]
    assert hints and all(i.severity.value == "warning" for i in hints)


def test_strict_mode_flags_empty_scene_as_error():
    script = _script(
        Scene(id="scene_1", heading="INT. - 日", summary="有内容"),
        Scene(id="scene_2", heading="INT. - 日"),  # 空场
    )
    bible = StoryBible(characters=[Character(id="char_1", name="林悦", role="主角")])
    report = consistency_service.validate_script(script, bible, strict=True)
    assert any(i.severity.value == "error" for i in consistency_service.has_errors(report))
    lenient = consistency_service.validate_script(script, bible)
    assert not consistency_service.has_errors(lenient)


@pytest.mark.asyncio
async def test_confirm_script_is_blocked_by_structure_errors(store):
    """严格模式下有 error 时「确认剧本」被拒；force=true 可以强制继续。"""
    pipeline = Pipeline(store, llm_factory=lambda: FakeLLM())
    project = Project(title="t", input_text="测试", target_duration_sec=30)
    project.status = ProjectStatus.SCRIPT_REVIEW
    store.save_project(project)
    store.save_bible(
        project.id,
        StoryBible(characters=[Character(id="char_1", name="林悦", role="主角")]),
    )
    store.save_script(
        project.id,
        _script(
            Scene(id="scene_1", heading="INT. - 日", summary="同一场"),
            Scene(id="scene_2", heading="INT. - 日", summary="同一场"),
        ),
    )
    bible = store.load_bible(project.id)
    report = consistency_service.validate_script(
        store.load_script(project.id), bible, strict=True
    )
    store.save_consistency(project.id, report)

    with pytest.raises(PipelineError, match="结构校验"):
        await pipeline.confirm_script(project.id)

    project = await pipeline.confirm_script(project.id, force=True)
    assert project.status == ProjectStatus.SCRIPT_CONFIRMED


# ── 确定性修复：说话人不在画面里 ─────────────────────


def test_repair_storyboard_adds_speaker_to_frame():
    """说话人不在画面里 = 穿帮，而且会让该镜头拿不到这个角色的参考图/外貌锚点。

    这条不需要创作者决策，属于可以确定性修的：直接补进 character_in_frame。
    """
    bible = StoryBible(
        characters=[
            Character(id="char_1", name="林悦", role="主角", description="女白领"),
            Character(id="char_2", name="李薇儿", role="反派", description="当红女模特"),
        ]
    )
    script = Script(title="t", scenes=[Scene(id="scene_1", heading="INT. 办公室 - 日")])
    storyboard = Storyboard(
        shots=[
            Shot(
                id="scene_1_shot_1",
                scene_id="scene_1",
                shot_no=1,
                duration_sec=3,
                video_prompt="a woman turns around",
                character_in_frame=["char_1"],
                dialogue_text="为什么？",
                dialogue_speaker="char_2",
            ),
            Shot(
                id="scene_1_shot_2",
                scene_id="scene_1",
                shot_no=2,
                duration_sec=3,
                video_prompt="a woman smiles",
                character_in_frame=["char_1"],
                dialogue_text="走吧。",
                dialogue_speaker="char_1",  # 已经在画面里，不该被动
            ),
        ]
    )

    fixed, notes = consistency_service.repair_storyboard(storyboard, script, bible)

    assert len(notes) == 1 and "char_2" in notes[0]
    assert fixed.shots[0].character_in_frame == ["char_1", "char_2"]
    assert fixed.shots[1].character_in_frame == ["char_1"]
    # 修完之后校验里不该再有 presence 警告
    issues = consistency_service.validate_storyboard(fixed, script, bible)
    assert not [i for i in issues if i.category == "presence"]


def test_repair_storyboard_leaves_unknown_speaker_alone():
    """说话人本身就不在圣经里 —— 不能猜，交给校验报 ERROR。"""
    bible = StoryBible(characters=[Character(id="char_1", name="甲", role="主角")])
    script = Script(title="t", scenes=[Scene(id="scene_1", heading="INT. - 日")])
    storyboard = Storyboard(
        shots=[
            Shot(
                id="scene_1_shot_1",
                scene_id="scene_1",
                shot_no=1,
                duration_sec=3,
                video_prompt="x",
                character_in_frame=["char_1"],
                dialogue_text="台词",
                dialogue_speaker="char_ghost",
            )
        ]
    )
    fixed, notes = consistency_service.repair_storyboard(storyboard, script, bible)
    assert notes == []
    assert fixed.shots[0].character_in_frame == ["char_1"]
    issues = consistency_service.validate_storyboard(fixed, script, bible)
    assert any(i.severity.value == "error" for i in issues)


# ── 多模型切换 ────────────────────────────────────────


def test_fake_llm_records_model_switch():
    llm = FakeLLM()
    assert llm.with_model("google/gemini-2.5-pro") is llm
    assert llm.models_used == ["google/gemini-2.5-pro"]
    assert llm.model == "google/gemini-2.5-pro"


@pytest.mark.asyncio
async def test_generate_uses_requested_model(store, fake_llm):
    pipeline = Pipeline(store, llm_factory=lambda: fake_llm)
    project = Project(input_text="换模型", target_duration_sec=30)
    store.save_project(project)

    await pipeline.generate_bible(project.id, model="google/gemini-2.5-pro")
    assert fake_llm.models_used == ["google/gemini-2.5-pro"]
    # 成本账本要记下用的是哪个模型
    entry = store.load_cost_report(project.id).entries[0]
    assert entry.model == "google/gemini-2.5-pro"


@pytest.mark.asyncio
async def test_empty_model_keeps_default(store, fake_llm):
    pipeline = Pipeline(store, llm_factory=lambda: fake_llm)
    project = Project(input_text="默认模型", target_duration_sec=30)
    store.save_project(project)

    await pipeline.generate_bible(project.id)
    assert fake_llm.models_used == []
    assert fake_llm.model == "fake-model"


@pytest.mark.asyncio
async def test_project_model_is_used_by_default(store, fake_llm):
    """项目上保存的模型应该在不传参数时生效（前端下拉用）。"""
    pipeline = Pipeline(store, llm_factory=lambda: fake_llm)
    project = Project(input_text="项目模型", target_duration_sec=30)
    store.save_project(project)
    updated = await pipeline.update_project(
        project.id, {"llm_model": "anthropic/claude-3.5-haiku"}
    )
    assert updated.llm_model == "anthropic/claude-3.5-haiku"

    await pipeline.generate_bible(project.id, model=updated.llm_model)
    assert fake_llm.models_used == ["anthropic/claude-3.5-haiku"]


def test_candidate_models_from_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "llm_model_candidates", "a/model-1, b/model-2, a/model-1, ,"
    )
    monkeypatch.setattr(settings, "openrouter_model", "default/model")
    assert settings.candidate_models[0] == "default/model"
    assert settings.candidate_models == ["default/model", "a/model-1", "b/model-2"]


def test_reference_request_model_validation():
    from videogen.main import ReferenceImageRequest

    with pytest.raises(ValidationError):
        ReferenceImageRequest(content_base64="")
    assert ReferenceImageRequest(filename="a.png", content_base64=PNG).filename == "a.png"


# ── 台词量 / 一镜一个说话人 / 改编评审（NOVEL 实测暴露的问题）─────────────────


def _two_people() -> StoryBible:
    return StoryBible(characters=[
        Character(id="char_1", name="我", role="主角"),
        Character(id="char_2", name="女朋友", role="主角"),
    ])


def test_dialogue_overload_warns_then_blocks_in_strict_mode():
    """12 秒的场塞 120 字台词（≈27 秒）：配音会把镜头拉长，成片远超目标时长。"""
    line = "你什么都知道，像上帝一样，真讨厌！" * 2  # 30 字
    lines = [DialogueLine(speaker="char_2", line=line) for _ in range(4)]
    script = _script(Scene(id="scene_1", summary="吵架", dialogue=lines, duration_sec=12))

    lenient = consistency_service.validate_script(script, _two_people())
    strict = consistency_service.validate_script(script, _two_people(), strict=True)

    hits = [i for i in lenient.issues if i.category == "duration" and "念完约" in i.message]
    assert hits and hits[0].severity.value == "warning"
    assert any("念完约" in i.message for i in consistency_service.has_errors(strict))


def test_dialogue_that_fits_is_not_flagged():
    script = _script(Scene(
        id="scene_1", summary="会合", duration_sec=12,
        dialogue=[DialogueLine(speaker="char_1", line="你有点着急了吧。")],
    ))
    report = consistency_service.validate_script(script, _two_people(), strict=True)
    assert not [i for i in report.issues if "念完约" in i.message]


def test_pronoun_led_phrases_are_not_names():
    """第一人称小说：「她大笑」「我也不知道」「谁知道」不是人名。"""
    summary = "她大笑说好。我也不知说什么。谁知说完就变了。"
    scene = Scene(id="scene_1", summary=summary, duration_sec=12)
    report = consistency_service.validate_script(_script(scene), _two_people())
    assert not [i for i in report.issues if "未定义的人名" in i.message]


def test_storyboard_flags_other_speakers_line_in_shot():
    """一条配音只有一个声音：别人的台词混进来会被同一个音色念掉。"""
    script = _script(Scene(id="scene_2", duration_sec=12, dialogue=[
        DialogueLine(speaker="char_2", line="你就这么往地下坐呀！"),
        DialogueLine(speaker="char_1", line="我累了！我走不动了。"),
    ]))
    storyboard = Storyboard(shots=[Shot(
        id="scene_2_shot_2", scene_id="scene_2", shot_no=1, duration_sec=6,
        character_in_frame=["char_1", "char_2"], video_prompt="two people on a street",
        dialogue_text="你就这么往地下坐呀！我累了！我走不动了。", dialogue_speaker="char_2",
    )])
    issues = consistency_service.validate_storyboard(storyboard, script, _two_people())
    mixed = [i for i in issues if "混进了" in i.message]
    assert mixed and mixed[0].severity.value == "error" and "char_1" in mixed[0].message


def test_storyboard_warns_when_shot_line_is_far_too_long():
    script = _script(Scene(id="scene_1", duration_sec=12))
    storyboard = Storyboard(shots=[Shot(
        id="scene_1_shot_1", scene_id="scene_1", shot_no=1, duration_sec=3,
        character_in_frame=["char_2"], video_prompt="a woman talking",
        dialogue_text="我昨天好像是个男人。我现在真不知该站在男人的立场上，还是该站在女人的立场上了。",
        dialogue_speaker="char_2",
    )])
    issues = consistency_service.validate_storyboard(storyboard, script, _two_people())
    assert any(i.category == "duration" and "念完约" in i.message for i in issues)


class _ReviewLLM:
    async def chat_json(self, messages, schema, **kwargs):
        return schema.model_validate({"issues": [
            {"severity": "error", "message": "身体互换的触发机制不明"},
        ]})


async def test_adaptation_review_never_blocks():
    """改编时评审看不到原著，原著自带的荒诞前提不能被报成 ERROR 拦住确认。"""
    script = _script(Scene(id="scene_1", summary="两人身体互换", duration_sec=12))
    original = await script_service.review_structure(_ReviewLLM(), script, _two_people())
    adapted = await script_service.review_structure(
        _ReviewLLM(), script, _two_people(), adaptation=True
    )
    assert original[0].severity.value == "error"
    assert adapted[0].severity.value == "warning"
