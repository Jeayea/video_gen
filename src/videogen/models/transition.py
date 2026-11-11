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


#: 黑场能用的音效（本机合成，见 `utils/sfx.py`）。循环类的铺满所在的时间，一次性的只响一下
SFX_NAMES = ("phone_ring", "phone_vibrate", "call_connect", "busy_tone", "hang_up")
_SFX_ALIASES = {
    "ring": "phone_ring",
    "ringtone": "phone_ring",
    "铃声": "phone_ring",
    "来电铃声": "phone_ring",
    "vibrate": "phone_vibrate",
    "vibration": "phone_vibrate",
    "震动": "phone_vibrate",
    "connect": "call_connect",
    "接通": "call_connect",
    "busy": "busy_tone",
    "忙音": "busy_tone",
    "hangup": "hang_up",
    "挂断": "hang_up",
}


def normalize_sfx(value: object) -> list[str]:
    """模型 / 人工写的音效列表 → `SFX_NAMES` 里的名字；认不出来的丢掉。"""
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace("，", ",").split(",")
    out = []
    for item in items:
        key = str(item or "").strip().lower().replace(" ", "_").replace("-", "_")
        name = _SFX_ALIASES.get(key, key)
        if name in SFX_NAMES:
            out.append(name)
    return out
