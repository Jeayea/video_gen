"""项目状态机：各操作的入口状态白名单 + 「按现有产物该落在哪个状态」。

只有纯函数和常量，读写磁盘留给 `Pipeline`（它负责拿锁、落盘）。
"""

from __future__ import annotations

from videogen.models.audio import AudioManifest
from videogen.models.project import Project, ProjectStatus
from videogen.models.storyboard import Storyboard
from videogen.models.video import ClipStatus, VideoManifest
from videogen.orchestrator.errors import PipelineError

# ── 各操作的入口状态白名单（唯一事实来源）──────────────────

GENERATE_BIBLE_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED}
GENERATE_SCRIPT_STATUSES = {
    ProjectStatus.DRAFT,
    ProjectStatus.FAILED,
    ProjectStatus.SCRIPT_REVIEW,
}
#: 长篇大纲入口：与剧本同权限（退回剧本阶段后可重出/续写大纲）
GENERATE_STORY_STATUSES = set(GENERATE_SCRIPT_STATUSES)
CONFIRM_SCRIPT_STATUSES = {ProjectStatus.SCRIPT_REVIEW}
REOPEN_SCRIPT_STATUSES = {
    ProjectStatus.SCRIPT_CONFIRMED,
    ProjectStatus.STORYBOARD_REVIEW,
    ProjectStatus.STORYBOARD_CONFIRMED,
    # 已经出片的项目也要能退回改剧本（改文本时才作废媒体）
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}
GENERATE_STORYBOARD_STATUSES = {
    ProjectStatus.SCRIPT_CONFIRMED,
    ProjectStatus.STORYBOARD_REVIEW,
    ProjectStatus.FAILED,
}
CONFIRM_STORYBOARD_STATUSES = {ProjectStatus.STORYBOARD_REVIEW}
#: 退回配音阶段：换音色 / 换念法 / 改台词后重做配音（成片会一起作废）
REOPEN_AUDIO_STATUSES = {
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}

REOPEN_STORYBOARD_STATUSES = {
    ProjectStatus.STORYBOARD_CONFIRMED,
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}

EDIT_BIBLE_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED, ProjectStatus.SCRIPT_REVIEW}
EDIT_SCRIPT_STATUSES = {ProjectStatus.DRAFT, ProjectStatus.FAILED, ProjectStatus.SCRIPT_REVIEW}
EDIT_STORYBOARD_STATUSES = {ProjectStatus.SCRIPT_CONFIRMED, ProjectStatus.STORYBOARD_REVIEW}
#: 确认之后（甚至成片之后）也能改分镜：只作废受影响的段，其余已生成的片段保留
EDIT_CONFIRMED_STORYBOARD_STATUSES = {
    ProjectStatus.STORYBOARD_CONFIRMED,
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.DONE,
}

#: 视频生成入口：确认分镜后开跑，或在 VIDEO_READY 下补跑失败/未生成的镜头。
#: 允许 VIDEO_GENERATING 是为了让「任务已死但状态没收拾干净」的项目也能手动重试。
GENERATE_VIDEO_STATUSES = {
    ProjectStatus.STORYBOARD_CONFIRMED,
    ProjectStatus.VIDEO_READY,
    ProjectStatus.VIDEO_GENERATING,
    # 配音/合成之后也允许补跑镜头（配音与文本无关，不会被作废）
    ProjectStatus.AUDIO_READY,
    ProjectStatus.COMPOSING,
    ProjectStatus.DONE,
}

#: 进程重启后需要收尾的「进行中」状态
TRANSIENT_STATUSES = {
    ProjectStatus.VIDEO_GENERATING,
    ProjectStatus.AUDIO_GENERATING,
    ProjectStatus.COMPOSING,
}

#: 配音入口：视频就绪后开跑；AUDIO_READY 下允许补跑失败条目
GENERATE_AUDIO_STATUSES = {
    ProjectStatus.VIDEO_READY,
    ProjectStatus.AUDIO_READY,
    ProjectStatus.AUDIO_GENERATING,
}

#: 合成入口：配音就绪后合成；DONE 下允许重新合成（换 BGM / 换字幕模式）
COMPOSE_STATUSES = {
    ProjectStatus.AUDIO_READY,
    ProjectStatus.COMPOSING,
    ProjectStatus.DONE,
}


def require(project: Project, allowed: set[ProjectStatus], action: str) -> None:
    """当前状态不在白名单里就拒绝（错误信息带上需要的状态）。"""
    if project.status not in allowed:
        expected = "/".join(sorted(s.value for s in allowed))
        raise PipelineError(f"当前状态 '{project.status.value}' 不允许{action}（需要 {expected}）")


# ── 按产物推状态 ──────────────────────────────────────────


def after_video(video: VideoManifest | None, audio: AudioManifest | None) -> ProjectStatus:
    """视频任务结束 / 被取消 / 中断后：有可用片段就进 VIDEO_READY（配音仍在则保持 AUDIO_READY），
    否则退回分镜确认态。"""
    if not video or not video.ready_clips:
        return ProjectStatus.STORYBOARD_CONFIRMED
    if audio is not None and audio.is_usable and audio.ready_clips:
        return ProjectStatus.AUDIO_READY
    return ProjectStatus.VIDEO_READY


def after_audio(audio: AudioManifest | None, video: VideoManifest | None) -> ProjectStatus:
    """配音 / 合成结束、被取消或中断后：配音齐全（含「本来就没台词」）就进 AUDIO_READY，
    否则按视频片段退回 VIDEO_READY / 分镜确认态。"""
    if not video or not video.ready_clips:
        return ProjectStatus.STORYBOARD_CONFIRMED
    if audio is not None and audio.is_usable:
        return ProjectStatus.AUDIO_READY
    return ProjectStatus.VIDEO_READY


def after_clips_changed(storyboard: Storyboard, video: VideoManifest | None) -> ProjectStatus:
    """改分镜 / 恢复历史片段之后：每个镜头都有就绪片段才算 VIDEO_READY（配音和成片已作废）。"""
    ready = video is not None and all(
        (clip := video.get(shot.id)) is not None and clip.status == ClipStatus.READY
        for shot in storyboard.shots
    )
    return ProjectStatus.VIDEO_READY if ready else ProjectStatus.STORYBOARD_CONFIRMED
