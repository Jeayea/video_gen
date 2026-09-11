"""测试用的项目构造器（直接落盘，跳过 LLM 链路，跑得快）。"""

from __future__ import annotations

from videogen.models.bible import Character, StoryBible, StyleGuide
from videogen.models.project import Project, ProjectStatus
from videogen.models.script import DialogueLine, Scene, Script
from videogen.models.storyboard import Shot, Storyboard
from videogen.utils.file_store import FileStore


def small_project(
    store: FileStore,
    *,
    shots: int = 3,
    shot_duration: int = 1,
    dialogue: str = "好。",
    status: ProjectStatus = ProjectStatus.STORYBOARD_CONFIRMED,
) -> Project:
    """一个「已确认分镜」的最小项目：1 场 / N 镜 / 每镜一句台词。"""
    project = Project(
        title="合成测试", input_text="测试", target_duration_sec=shots * shot_duration
    )
    project.status = status
    store.save_project(project)
    store.save_bible(
        project.id,
        StoryBible(
            characters=[Character(id="char_1", name="林晚", role="主角", description="女白领")],
            style=StyleGuide(genre="都市复仇"),
        ),
    )
    store.save_script(
        project.id,
        Script(
            title="合成测试",
            scenes=[
                Scene(
                    id="scene_1",
                    heading="INT. 办公室 - 日",
                    summary="对峙",
                    duration_sec=shots * shot_duration,
                    dialogue=[DialogueLine(speaker="char_1", line=dialogue)],
                )
            ],
        ),
    )
    store.save_storyboard(
        project.id,
        Storyboard(
            shots=[
                Shot(
                    id=f"scene_1_shot_{index}",
                    scene_id="scene_1",
                    shot_no=index,
                    duration_sec=shot_duration,
                    camera="中景 · 固定",
                    visual_description="林晚站在会议室中央",
                    video_prompt="medium shot, a young Chinese woman in an office",
                    mood="紧张",
                    dialogue_text=dialogue,
                    dialogue_speaker="char_1",
                )
                for index in range(1, shots + 1)
            ]
        ),
    )
    return store.load_project(project.id)
