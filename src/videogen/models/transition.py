"""镜头 / 场次之间的转场写法（剧本和分镜共用）。"""

from __future__ import annotations

#: 镜头怎么从上一镜进来：空 = 硬切；`dissolve` = 叠化；`fade` = 经黑场（上一镜淡出、这一镜淡入）
TRANSITIONS = ("", "dissolve", "fade")
_TRANSITION_ALIASES = {
    "cut": "",
    "hard_cut": "",
    "硬切": "",
    "直切": "",
    "切": "",
    "切至": "",
    "dissolve": "dissolve",
    "crossfade": "dissolve",
    "cross_dissolve": "dissolve",
    "叠化": "dissolve",
    "叠化至": "dissolve",
    "溶解": "dissolve",
    "fade": "fade",
    "fade_black": "fade",
    "dip_to_black": "fade",
    "黑场": "fade",
    "淡入淡出": "fade",
    "淡入": "fade",
    "淡出": "fade",
    "淡出淡入": "fade",
    "黑场过渡": "fade",
}


def normalize_transition(value: object) -> str:
    """模型 / 人工写的转场 → `TRANSITIONS` 之一；认不出来的当硬切。"""
    key = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _TRANSITION_ALIASES.get(key, key if key in TRANSITIONS else "")
