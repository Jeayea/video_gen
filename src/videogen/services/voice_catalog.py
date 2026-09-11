"""配音选角：给每个角色分配**自己的**声音。

为什么需要单独一个模块：`AUDIO_SERVICE` 早期只用「关键词判定男女声 + 3 个配置音色轮转」，
真实项目里直接翻车了 —— 4 个角色只用到 2 个音色，而且 22 岁的女反派被配了男声
（`_MALE_HINTS` 里混进了「反派」这种**身份词**，而男声判定在前）。

这里的规则：

1. **性别线索分强弱**：显式性别词（男/女/woman/man）> 亲属称谓（姐/父/母亲）；
   「反派 / 总裁 / 主角 / 导师」是身份词，**不参与**性别判定
2. **音色表带元数据**（性别 / 年龄区间 / 音色性格 / 方言标记），选角按
   「性别 → 角色定位 → 年龄 → 口音代价」打分，先在**同性别未占用**的音色里挑最好的
3. **绝不给两个角色同一个声音**：同性别音色用尽时，复用最合适的那个并下移/上移音高与
   语速做区分（`VoiceDirection`），同时留下明确说明 —— 音色池的硬限制要让人看得见
4. 选定结果会落盘（`audio_manifest.voice_map` + `Character.voice_id`），重跑不换人
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from videogen.config import Settings, get_settings
from videogen.models.bible import Character, StoryBible
from videogen.models.storyboard import VoiceDirection

logger = logging.getLogger(__name__)

__all__ = [
    "VOICE_CATALOG",
    "VoiceCasting",
    "VoiceProfile",
    "cast_voice_map",
    "catalog_rows",
    "infer_gender",
    "voice_gender",
]

NARRATOR = "narrator"


@dataclass(frozen=True)
class VoiceProfile:
    """一个可选音色（元数据来自 Edge TTS 的 zh 音色表 + 官方 VoiceTag）。"""

    id: str
    label: str
    gender: str  # female | male
    age: tuple[int, int]
    timbre: str
    #: 适合的角色定位（和 bible 的 role 对齐）
    tags: tuple[str, ...]
    locale: str = "zh-CN"
    dialect: bool = False

    @property
    def age_mid(self) -> float:
        return (self.age[0] + self.age[1]) / 2


#: Edge TTS 的 zh 音色全表（2026-09 实测 `edge_tts.list_voices()` 只有这 14 个）。
#: 通用普通话只有 8 个：4 女（其中 2 个是方言）4 男 —— 这是硬约束，
#: 所以角色超过同性别音色数时必须靠音高/语速区分（见 VoiceCasting.notes）。
VOICE_CATALOG: tuple[VoiceProfile, ...] = (
    VoiceProfile(
        id="zh-CN-XiaoxiaoNeural",
        label="晓晓 · 女声（温润知性）",
        gender="female",
        age=(18, 30),
        timbre="温润、亲切",
        tags=("女主角", "主角", "少女", "配角"),
    ),
    VoiceProfile(
        id="zh-CN-XiaoyiNeural",
        label="晓伊 · 女声（清亮张扬）",
        gender="female",
        age=(16, 26),
        timbre="清亮、活泼",
        tags=("反派", "少女", "女主", "配角"),
    ),
    VoiceProfile(
        id="zh-CN-YunxiNeural",
        label="云希 · 男声（清朗少年感）",
        gender="male",
        age=(18, 30),
        timbre="清朗、阳光",
        tags=("男主角", "主角", "少年", "配角"),
    ),
    VoiceProfile(
        id="zh-CN-YunjianNeural",
        label="云健 · 男声（有力，旁白）",
        gender="male",
        age=(28, 45),
        timbre="有力、激情",
        tags=("旁白", "主角", "导师"),
    ),
    VoiceProfile(
        id="zh-CN-YunyangNeural",
        label="云扬 · 男声（沉稳权威）",
        gender="male",
        age=(28, 55),
        timbre="沉稳、权威",
        tags=("导师", "配角", "反派", "长辈"),
    ),
    VoiceProfile(
        id="zh-CN-YunxiaNeural",
        label="云夏 · 男声（少年）",
        gender="male",
        age=(8, 16),
        timbre="少年、稚气",
        tags=("少年", "配角"),
    ),
    VoiceProfile(
        id="zh-CN-liaoning-XiaobeiNeural",
        label="晓北 · 女声（东北口音）",
        gender="female",
        age=(20, 45),
        timbre="幽默、接地气",
        tags=("配角", "长辈"),
        dialect=True,
    ),
    VoiceProfile(
        id="zh-CN-shaanxi-XiaoniNeural",
        label="晓妮 · 女声（陕西口音）",
        gender="female",
        age=(18, 35),
        timbre="明亮、爽利",
        tags=("配角",),
        dialect=True,
    ),
    VoiceProfile(
        id="zh-TW-HsiaoChenNeural",
        label="晓臻 · 女声（台湾国语）",
        gender="female",
        age=(20, 40),
        timbre="温柔、亲和",
        tags=("配角", "长辈"),
        locale="zh-TW",
    ),
    VoiceProfile(
        id="zh-TW-HsiaoYuNeural",
        label="晓雨 · 女声（台湾国语）",
        gender="female",
        age=(18, 35),
        timbre="甜美、亲和",
        tags=("少女", "配角"),
        locale="zh-TW",
    ),
    VoiceProfile(
        id="zh-TW-YunJheNeural",
        label="云哲 · 男声（台湾国语）",
        gender="male",
        age=(20, 45),
        timbre="亲和、自然",
        tags=("配角",),
        locale="zh-TW",
    ),
    VoiceProfile(
        id="zh-HK-HiuGaaiNeural",
        label="曉佳 · 女声（粤语）",
        gender="female",
        age=(20, 40),
        timbre="友好、积极",
        tags=("配角",),
        locale="zh-HK",
    ),
    VoiceProfile(
        id="zh-HK-HiuMaanNeural",
        label="曉曼 · 女声（粤语）",
        gender="female",
        age=(20, 40),
        timbre="友好、积极",
        tags=("配角",),
        locale="zh-HK",
    ),
    VoiceProfile(
        id="zh-HK-WanLungNeural",
        label="雲龍 · 男声（粤语）",
        gender="male",
        age=(20, 45),
        timbre="友好、积极",
        tags=("配角",),
        locale="zh-HK",
    ),
)

CATALOG_BY_ID = {voice.id: voice for voice in VOICE_CATALOG}

#: 显式性别词（权重 2）—— 这些是真正说明性别的
_STRONG_MALE = (
    "男",
    "男人",
    "男生",
    "男性",
    "男士",
    "男声",
    "先生",
    "少年",
    "汉子",
    "male",
    "man",
    "men",
    "boy",
    "gentleman",
    "sir",
)
_STRONG_FEMALE = (
    "女",
    "女人",
    "女生",
    "女性",
    "女士",
    "小姐",
    "女声",
    "少女",
    "姑娘",
    "female",
    "woman",
    "women",
    "girl",
    "lady",
    "madam",
)
#: 亲属/关系称谓（权重 1）
_WEAK_MALE = (
    "父",
    "爸",
    "哥",
    "弟",
    "叔",
    "爷",
    "兄",
    "儿子",
    "丈夫",
    "老公",
    "外公",
    "father",
    "brother",
    "son",
    "husband",
    "uncle",
)
_WEAK_FEMALE = (
    "母",
    "妈",
    "姐",
    "妹",
    "姨",
    "姑",
    "女儿",
    "妻子",
    "老婆",
    "外婆",
    "奶奶",
    "mother",
    "sister",
    "daughter",
    "wife",
    "aunt",
)

#: 角色定位 → 期望的音色定位（用于打分）
_ROLE_TAGS = {
    "主角": ("主角", "女主角", "男主角"),
    "女主": ("女主角", "主角"),
    "男主": ("男主角", "主角"),
    "反派": ("反派",),
    "次要反派": ("反派", "配角"),
    "导师": ("导师", "长辈"),
    "盟友": ("导师", "配角"),
    "配角": ("配角",),
    "龙套": ("配角",),
    "旁白": ("旁白",),
}


def _hits(text: str, hints: tuple[str, ...]) -> int:
    """中文按包含、英文按整词（woman ≠ man）计数。"""
    count = 0
    for hint in hints:
        if hint.isascii():
            if re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", text):
                count += 1
        elif hint in text:
            count += 1
    return count


def infer_gender(character: Character | None) -> tuple[str | None, str]:
    """推断角色性别，返回 `(male|female|None, 依据)`。

    显式性别词 2 分、亲属称谓 1 分，取分高的一侧；打平（或都没命中）返回 None。
    **身份词（反派/总裁/主角/导师）不参与判定** —— 这正是当初把女反派配成男声的原因。
    """
    if character is None:
        return None, ""
    text = " ".join(
        str(part).lower()
        for part in (
            getattr(character, "role", ""),
            getattr(character, "description", ""),
            getattr(character, "visual_prompt", ""),
            getattr(character, "name", ""),
            " ".join(getattr(character, "personality", []) or []),
        )
    )
    male = 2 * _hits(text, _STRONG_MALE) + _hits(text, _WEAK_MALE)
    female = 2 * _hits(text, _STRONG_FEMALE) + _hits(text, _WEAK_FEMALE)
    if male > female:
        return "male", f"文本线索 男{male}:女{female}"
    if female > male:
        return "female", f"文本线索 女{female}:男{male}"
    return None, "文本里没有明确性别线索"


def voice_gender(voice_id: str) -> str | None:
    """已知音色的性别（音色表里没有的返回 None，例如用户自定义/付费音色）。"""
    profile = CATALOG_BY_ID.get(voice_id)
    return profile.gender if profile else None


def catalog_rows() -> list[dict[str, object]]:
    """给前端下拉用的音色表。"""
    return [
        {
            "id": voice.id,
            "label": voice.label,
            "gender": voice.gender,
            "age": list(voice.age),
            "timbre": voice.timbre,
            "tags": list(voice.tags),
            "locale": voice.locale,
            "dialect": voice.dialect,
        }
        for voice in VOICE_CATALOG
    ]


@dataclass
class VoiceCasting:
    """选角结果：谁用什么音色 + 为区分而复用的念法 + 给人看的说明。"""

    voice_map: dict[str, str]
    delivery: dict[str, VoiceDirection]
    notes: list[str]
    genders: dict[str, str]


def _score(voice: VoiceProfile, character: Character | None, settings: Settings) -> float:
    """音色对某个角色的匹配分（越大越好）。"""
    score = 0.0
    role = str(getattr(character, "role", "") or "").strip()
    wanted = _ROLE_TAGS.get(role, ())
    if wanted:
        if any(tag in voice.tags for tag in wanted):
            score += 3.0
        else:
            score -= 1.0
    age = getattr(character, "age", None)
    if isinstance(age, int) and age > 0:
        score -= abs(voice.age_mid - age) / 10.0
    if voice.locale != "zh-CN":
        score -= 6.0  # 同一部戏里混口音很出戏
    if voice.dialect:
        score -= 8.0
    if voice.id == settings.tts_voice_female and voice.gender == "female":
        score += 0.5  # 用户配置的默认女声稍微优先
    if voice.id == settings.tts_voice_male and voice.gender == "male":
        score += 0.5
    return score


def _reuse_delivery(voice: VoiceProfile, index: int) -> VoiceDirection:
    """同一个音色被两个角色共用时，用音高/语速区分（第 1 次复用下移，第 2 次上移）。"""
    shifts = (
        VoiceDirection(pitch="-12Hz", rate="-6%", note="与同音色角色区分：音高下移、语速放慢"),
        VoiceDirection(pitch="+14Hz", rate="+6%", note="与同音色角色区分：音高上移、语速略快"),
    )
    return shifts[index % len(shifts)]


#: 音色标注的年龄区间之外再超出这么多岁，就算「明显不匹配」
_AGE_SLACK = 8


def _age_mismatch(voice_id: str, character: Character | None) -> bool:
    profile = CATALOG_BY_ID.get(voice_id)
    age = getattr(character, "age", None)
    if profile is None or not isinstance(age, int) or age <= 0:
        return False
    return age < profile.age[0] - _AGE_SLACK or age > profile.age[1] + _AGE_SLACK


def _best_unused(
    current: str,
    character: Character | None,
    settings: Settings,
    used: dict[str, str],
    gender: str | None,
) -> VoiceProfile | None:
    """找一个「空闲 + 同性别 + 明显更合适」的音色；没有就返回 None。"""
    current_profile = CATALOG_BY_ID.get(current)
    if current_profile is None:
        return None
    pool = [v for v in VOICE_CATALOG if (not gender or v.gender == gender) and v.id not in used]
    if not settings.tts_allow_accent_voices:
        pool = [v for v in pool if v.locale == "zh-CN" and not v.dialect]
    baseline = _score(current_profile, character, settings)
    best = max(pool, key=lambda v: (_score(v, character, settings), v.id), default=None)
    if best is None:
        return None
    return best if _score(best, character, settings) - baseline >= 1.0 else None


def _speakers_of(storyboard) -> list[str]:
    speakers: list[str] = []
    for shot in storyboard.shots:
        speaker = (shot.dialogue_speaker or "").strip()
        if speaker and speaker not in speakers:
            speakers.append(speaker)
    if any(shot.narration for shot in storyboard.shots) and NARRATOR not in speakers:
        speakers.append(NARRATOR)
    return speakers


def cast_voice_map(
    storyboard,
    bible: StoryBible | None,
    settings: Settings | None = None,
    *,
    existing: dict[str, str] | None = None,
) -> VoiceCasting:
    """给每个说话人分配音色（确定性：同样的输入永远得到同样的选角）。

    已有分配会保留，**除非它和推断出的性别冲突** —— 那种情况下必须重配，
    否则「女角配男声」这种错误会跟着 `voice_map` 一直续跑下去。
    """
    settings = settings or get_settings()
    characters = {c.id: c for c in (bible.characters if bible else [])}
    speakers = _speakers_of(storyboard)

    mapping: dict[str, str] = {}
    delivery: dict[str, VoiceDirection] = {}
    notes: list[str] = []
    genders: dict[str, str] = {}
    used: dict[str, str] = {}  # voice_id -> 第一个用它的 speaker
    reuse_index = 0

    for speaker in speakers:
        if speaker == NARRATOR:
            mapping[speaker] = settings.tts_voice_narrator
            genders[speaker] = voice_gender(settings.tts_voice_narrator) or ""
            used.setdefault(settings.tts_voice_narrator, NARRATOR)
            continue

        character = characters.get(speaker)
        gender, reason = infer_gender(character)
        if gender:
            genders[speaker] = gender
        name = getattr(character, "name", "") or speaker

        # 角色自己写死的音色（bible.voice_id）优先，但仍要过性别校验
        pinned = str(getattr(character, "voice_id", "") or "").strip()
        previous = (existing or {}).get(speaker) or ""
        candidate = pinned or previous
        if candidate:
            clash = bool(gender) and voice_gender(candidate) not in (None, gender)
            if clash:
                notes.append(
                    f"{name}（{speaker}）原有音色 {candidate} 与推断性别「{gender}」冲突，"
                    f"已重新选角（{reason}）"
                )
                candidate = ""  # 必须清掉，否则下面还会把这个错误音色写回去
            elif pinned:
                # 人工显式指定的音色永远优先（可能就是要这个反差）
                mapping[speaker] = candidate
                used.setdefault(candidate, speaker)
                continue
            elif _age_mismatch(candidate, character):
                # 年龄明显不匹配（真实样本：45 岁的杂志主编挂着「清朗少年感」的云希）：
                # 有空闲且更合适的音色就换，没有就保留
                better = _best_unused(candidate, character, settings, used, gender)
                if better is not None:
                    notes.append(
                        f"{name}（{speaker}）原音色 {candidate} 与年龄不符"
                        f"（{getattr(character, 'age', '?')} 岁），已换为 {better.id}"
                    )
                    candidate = ""
                else:
                    mapping[speaker] = candidate
                    used.setdefault(candidate, speaker)
                    continue
            if candidate:
                mapping[speaker] = candidate
                owner = used.setdefault(candidate, speaker)
                if owner != speaker:
                    delivery[speaker] = _reuse_delivery(
                        CATALOG_BY_ID.get(candidate, VOICE_CATALOG[0]), reuse_index
                    )
                    reuse_index += 1
                    notes.append(
                        f"{name}（{speaker}）与他人共用音色 {candidate}，"
                        f"已用念法区分（{delivery[speaker].note}）"
                    )
                continue

        pool = [
            voice
            for voice in VOICE_CATALOG
            if not gender or voice.gender == gender
        ]
        if not settings.tts_allow_accent_voices:
            same_locale = [v for v in pool if v.locale == "zh-CN" and not v.dialect]
            pool = same_locale or pool
        by_score = sorted(
            pool, key=lambda v: (-_score(v, character, settings), v.id)
        )
        if not by_score:
            mapping[speaker] = settings.tts_voice
            notes.append(f"{name}（{speaker}）没有可用音色，退回默认 {settings.tts_voice}")
            continue

        free = [v for v in by_score if v.id not in used]
        chosen = free[0] if free else by_score[0]
        mapping[speaker] = chosen.id
        if chosen.id in used:
            delivery[speaker] = _reuse_delivery(chosen, reuse_index)
            reuse_index += 1
            notes.append(
                f"{name}（{speaker}）与 {used[chosen.id]} 共用音色 {chosen.label}："
                f"同性别可用音色已用尽，已用念法区分（{delivery[speaker].note}）。"
                "想让每个人都完全不同，可接入付费 TTS 或手工换音色"
            )
        else:
            used[chosen.id] = speaker
        logger.info("选角：%s(%s) -> %s（%s）", speaker, name, chosen.id, reason)

    return VoiceCasting(voice_map=mapping, delivery=delivery, notes=notes, genders=genders)
