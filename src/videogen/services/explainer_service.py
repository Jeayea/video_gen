"""知识讲解型科普（`content_type=explainer`）。

设计见 docs/EXPLAINER_DESIGN.md：沿用镜头模型，一句旁白一个镜头，每句旁白挂到知识底稿里的事实上。
现有 service 只在入口处按内容类型分派到这里，不在短剧的函数里堆分支。
"""

from __future__ import annotations

from videogen.config import Settings, get_settings
from videogen.models.project import ContentType, Project

#: 科普配乐比全局 BGM_GAIN_DB 再低多少 dB：旁白是主线，音乐只垫底
MUSIC_GAIN_OFFSET_DB = -6.0
#: 科普默认烧字幕样式：观众常静音看，半透明底条在任何画面上都读得清
SUBTITLE_STYLE = "box"
MUSIC_MOOD = "neutral"


def is_explainer(project: Project | None) -> bool:
    return getattr(project, "content_type", None) == ContentType.EXPLAINER


def apply_creation_defaults(project: Project, settings: Settings | None = None) -> Project:
    """建项目时给科普项目填默认值（用户没填的才填；短剧原样返回）。

    写在项目字段上而不是运行时判断：面板上看得见、改得了，和用户自己设的值走同一条路。
    """
    if not is_explainer(project):
        return project
    settings = settings or get_settings()
    if not project.subtitle_style:
        project.subtitle_style = SUBTITLE_STYLE
    if not project.music_mood:
        project.music_mood = MUSIC_MOOD
    if project.music_gain_db is None:
        project.music_gain_db = max(-60.0, float(settings.bgm_gain_db) + MUSIC_GAIN_OFFSET_DB)
    return project
