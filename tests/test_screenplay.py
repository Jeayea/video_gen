"""剧本层的动作描写 / 括注 / 时空 / 转场，以及 $0 的文学剧本导出。"""

from __future__ import annotations

import pytest

from tests.fakes import FakeLLM
from videogen.models.bible import Character, Setting, StoryBible
from videogen.models.llm_io import SceneResponse
from videogen.models.project import Project
from videogen.models.script import DialogueLine, OutlineScene, Scene, Script
from videogen.models.storyboard import Shot, Storyboard
from videogen.models.world_state import WorldState
from videogen.services import screenplay_service, script_service, storyboard_service


def bible() -> StoryBible:
    return StoryBible(
        characters=[
            Character(
                id="char_1", name="林晓", age=24, description="江西姑娘，在上海做文员。勤快。"
            ),
            Character(id="char_2", name="母亲", age=50, description="在东莞做过十年流水线。"),
            Character(id="char_6", name="房东", description="说话很快。"),
        ],
        settings=[Setting(id="loc_1", name="出租屋")],
    )


def script() -> Script:
    return Script(
        title="沪漂",
        logline="char_1 在上海的最后一个月。",
        scenes=[
            Scene(
                id="scene_1",
                heading="INT. 出租屋 - 夜",
                time_label="现在 · 2024 年 11 月",
                transition_in="fade",
                action="char_1 坐在床沿，手里攥着工牌。",
                dialogue=[
                    DialogueLine(
                        speaker="char_1", line="妈，我挺好的。", parenthetical="电话里",
                        action="她把手机贴近耳朵。",
                    )
                ],
                closing="她把工牌放进抽屉。",
                duration_sec=12,
                emotion="压抑",
            ),
            Scene(
                id="scene_2",
                heading="EXT. 东莞工厂门口 - 日",
                time_label="回忆 · 1998 年夏",
                transition_in="叠化",
                action="年轻的 char_2 拉开抽屉，取出工牌。",
                dialogue=[DialogueLine(speaker="char_2", line="再干一年就回家。")],
                duration_sec=10,
            ),
            Scene(
                id="scene_3",
                heading="黑场",
                transition_in="fade",
                action="黑屏。",
                dialogue=[
                    DialogueLine(speaker="char_6", line="小林啊，房租这周要交了。",
                                 parenthetical="电话里")
                ],
                duration_sec=5,
            ),
        ],
    )


# ── 模型 ──────────────────────────────────────────────


def test_scene_transition_aliases_and_old_scripts_load():
    assert script().scenes[1].transition_in == "dissolve"
    old = Scene.model_validate({"id": "scene_1", "heading": "x", "dialogue": [{"speaker": "a"}]})
    assert old.action == "" and old.dialogue[0].action == "" and old.transition_in == ""
    response = SceneResponse.model_validate({"action": None, "closing": None, "time_label": None})
    assert response.action == "" and response.closing == ""


def test_split_parenthetical_keeps_tone_but_not_in_spoken_line():
    assert script_service.split_parenthetical("（咬牙）这个项目是我做的。") == (
        "咬牙", "这个项目是我做的。"
    )
    assert script_service.split_parenthetical("（低声）（电话里）喂？") == ("低声，电话里", "喂？")
    assert script_service.split_parenthetical("没有括号") == ("", "没有括号")


@pytest.mark.asyncio
async def test_generate_scene_keeps_actions_and_moves_action_only_rows():
    class SceneLLM(FakeLLM):
        async def chat_json(self, messages, schema=None, **kwargs):
            self.last_system = messages[0].content
            return schema.model_validate(
                {
                    "heading": "INT. 出租屋 - 夜",
                    "summary": "她接到电话",
                    "time_label": "现在",
                    "transition_in": "Dissolve",
                    "action": "台灯下，char_1 在改简历。",
                    "dialogue": [
                        {"speaker": "char_1", "action": "她停下手。", "line": "（低声）喂？"},
                        {"speaker": "char_1", "action": "她挂断电话。", "line": ""},
                    ],
                    "closing": "屏幕暗下去。",
                    "duration_sec": 10,
                    "emotion": "压抑",
                }
            )

    llm = SceneLLM()
    scene, _ = await script_service.generate_scene(
        llm,
        Project(id="p", title="沪漂", target_duration_sec=10),
        bible(),
        OutlineScene(id="scene_1", title="电话", duration_budget_sec=10),
        [],
        WorldState(),
        1,
    )
    assert "逐场编写短剧剧本" in llm.last_system  # FakeLLM 的锚点没被改掉
    assert scene.transition_in == "dissolve" and scene.time_label == "现在"
    assert scene.action == "台灯下，char_1 在改简历。"
    assert len(scene.dialogue) == 1
    line = scene.dialogue[0]
    assert (line.line, line.parenthetical, line.action) == ("喂？", "低声", "她停下手。")
    assert scene.closing == "她挂断电话。屏幕暗下去。"  # 只有动作的一行并进场末画面


def test_action_without_subject_gets_the_speakers_name():
    cast = bible()
    assert script_service.with_subject("指节扣了扣隔板", "char_1", cast) == "林晓指节扣了扣隔板"
    assert script_service.with_subject("她停下手。", "char_1", cast) == "她停下手。"
    assert script_service.with_subject("母亲推开门", "char_1", cast) == "母亲推开门"
    assert script_service.with_subject("门开了", "narrator", cast) == "门开了"
    assert script_service.with_subject("窗外雷声炸响", "char_1", cast) == "窗外雷声炸响"
    assert script_service.with_subject("手机屏幕亮起", "char_1", cast) == "手机屏幕亮起"


def test_prev_scene_context_carries_closing_for_match_cuts():
    text = script_service._format_prev_scenes(script().scenes[:1])
    assert "结尾画面: 她把工牌放进抽屉。" in text and "故事时间: 现在" in text


# ── 分镜 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_storyboard_prompt_has_actions_and_first_shot_gets_scene_transition():
    class ShotLLM(FakeLLM):
        async def chat_json(self, messages, schema=None, **kwargs):
            self.prompt = messages[1].content
            return schema.model_validate(
                {"shots": [{"duration_sec": 6, "video_prompt": "a"},
                           {"duration_sec": 4, "video_prompt": "b"}]}
            )

    llm = ShotLLM()
    scene = script().scenes[1]
    shots = await storyboard_service.generate_shots_for_scene(
        llm, Project(id="p", title="t"), bible(), scene
    )
    assert "开场画面: 年轻的 char_2 拉开抽屉" in llm.prompt
    assert "故事时间: 回忆 · 1998 年夏" in llm.prompt
    assert [s.transition for s in shots] == ["dissolve", ""]

    revised = await storyboard_service.generate_shots_for_scene(
        llm, Project(id="p", title="t"), bible(), scene, target_shot="scene_2_shot_2"
    )
    assert revised[0].transition == ""  # 只重写第二镜时不能把场转场安到它头上


# ── 文学剧本导出 ──────────────────────────────────────


def test_screenplay_from_script_reads_like_a_screenplay():
    result = screenplay_service.build(script(), bible())
    md = result["markdown"]
    assert "char_" not in md and "loc_" not in md
    assert "## 1. 内景　出租屋 - 夜" in md
    assert "## 2. 外景　东莞工厂门口 - 日" in md
    assert "*现在 · 2024 年 11 月 · 约 12 秒 · 压抑*" in md
    assert md.index("**淡入：**") < md.index("林晓坐在床沿")
    assert "**林晓**（电话里）：妈，我挺好的。" in md
    assert md.index("她把工牌放进抽屉。") < md.index("**叠化至：**") < md.index("## 2.")
    assert md.index("**淡出。**") < md.index("## 3.") < md.index("**淡入：**", md.index("## 3."))
    assert "- 林晓（24 岁）：江西姑娘，在上海做文员。" in md
    assert "房东" in md and md.rstrip().endswith("（完）")
    assert result["stats"]["transitions"] == 3 and not result["warnings"]


def test_screenplay_warns_about_scripts_without_action():
    old = Script(scenes=[Scene(id="scene_1", dialogue=[DialogueLine(speaker="char_1", line="嗯")])])
    result = screenplay_service.build(old, bible())
    assert result["warnings"] and "scene_1" in result["warnings"][0]


def test_screenplay_prefers_storyboard_transitions_and_expands_shots():
    board = Storyboard(
        shots=[
            Shot(id="scene_1_shot_1", scene_id="scene_1", shot_no=1, camera="近景 · 固定",
                 visual_description="char_1 盯着手机", dialogue_text="妈，我挺好的。",
                 dialogue_speaker="char_1"),
            Shot(id="scene_2_shot_1", scene_id="scene_2", shot_no=1, transition="",  # 分镜改成硬切
                 visual_description="抽屉特写"),
            Shot(id="scene_3_shot_1", scene_id="scene_3", shot_no=1, black_screen=True,
                 transition="fade", dialogue_text="房租这周要交了。", dialogue_speaker="char_6"),
        ]
    )
    md = screenplay_service.build(script(), bible(), board)["markdown"]
    assert "叠化至" not in md  # 片子实际是硬切，以分镜为准

    shots = screenplay_service.build(script(), bible(), board, mode="shots")
    md = shots["markdown"]
    assert "`镜 1 · 近景 · 固定 · 4 秒`　林晓盯着手机" in md
    assert "`镜 1 · 黑场 · 4 秒`　黑屏。" in md
    assert md.count("黑屏。") == 1  # 剧本的「黑屏。」和黑场镜头不重复印
    assert "**房东**（画外）：房租这周要交了。" in md
    assert shots["stats"]["shots"] == 3


def test_screenplay_shots_mode_without_storyboard_falls_back():
    result = screenplay_service.build(script(), bible(), None, mode="shots")
    assert result["mode"] == "script" and result["warnings"]


def test_screenplay_endpoint(store, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.factories import small_project
    from videogen import main
    from videogen.orchestrator.pipeline import Pipeline

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "pipeline", Pipeline(store, llm_factory=lambda: FakeLLM()))
    client = TestClient(main.app)
    project = small_project(store)
    response = client.get(f"/api/projects/{project.id}/screenplay")
    assert response.status_code == 200
    body = response.json()
    assert body["blocks"][0]["type"] == "title" and body["markdown"].startswith("# ")
    assert client.get(f"/api/projects/{project.id}/screenplay?mode=x").status_code == 400
    assert client.get("/api/projects/nope/screenplay").status_code == 404


# ── 分镜卡片：预览最终 prompt ─────────────────────────


def test_preview_prompt_uses_the_same_builder_as_generation():
    from videogen.models.video import ClipStatus, VideoClip, VideoManifest
    from videogen.services import video_service

    project = Project(id="p", title="t")
    shots = [
        Shot(id="scene_1_shot_1", scene_id="scene_1", shot_no=1, segment_id="seg_1",
             video_prompt="a woman at a desk", character_in_frame=["char_1"]),
        Shot(id="scene_1_shot_2", scene_id="scene_1", shot_no=2, segment_id="seg_1",
             video_prompt="close-up of a phone"),
        Shot(id="scene_2_shot_1", scene_id="scene_2", shot_no=1, video_prompt="a factory gate"),
        Shot(id="scene_3_shot_1", scene_id="scene_3", shot_no=1, black_screen=True),
    ]
    board = Storyboard(shots=shots)
    seg = video_service.preview_prompt(board, "scene_1_shot_2", project, bible())
    assert seg["kind"] == "segment" and seg["shot_ids"] == ["scene_1_shot_1", "scene_1_shot_2"]
    assert seg["prompt"] == video_service.build_segment_prompt(shots[:2], project, bible())
    one = video_service.preview_prompt(
        board, "scene_2_shot_1", project, bible(),
        VideoManifest(clips=[VideoClip(shot_id="scene_2_shot_1", status=ClipStatus.READY,
                                       prompt="old prompt")]),
    )
    assert one["kind"] == "shot" and one["prompt"] == video_service.build_prompt(
        shots[2], project, bible()
    )
    assert one["sent_prompt"] == "old prompt" and one["sent_differs"]
    assert video_service.preview_prompt(board, "scene_3_shot_1", project, bible())["kind"] == (
        "black"
    )
    with pytest.raises(KeyError):
        video_service.preview_prompt(board, "nope", project, bible())


def test_screenplay_marks_chapter_changes():
    md = screenplay_service.build(
        script(), bible(),
        chapters={
            "scene_1": "第一章：脚本", "scene_2": "第一章：脚本", "scene_3": "第十六章：黑屏"
        },
    )["markdown"]
    assert md.count("**—— 第一章：脚本 ——**") == 1
    assert md.index("第一章") < md.index("## 1.") < md.index("第十六章") < md.index("## 3.")
