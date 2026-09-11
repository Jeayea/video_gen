from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from videogen.utils.coerce import StrList, ensure_str_list


def _merge_ordered(existing: list[str], incoming: list[str]) -> list[str]:
    """有序去重合并，保证同样的输入产生同样的输出（可 diff）。"""
    out = list(existing)
    seen = set(existing)
    for item in incoming:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


class CharacterState(BaseModel):
    character_id: str
    location: str = ""
    physical: str = "正常"
    emotional: str = ""
    knows: StrList = Field(default_factory=list)
    possesses: StrList = Field(default_factory=list)
    active_goals: StrList = Field(default_factory=list)


class WorldState(BaseModel):
    current_scene: str = ""
    timeline: str = ""
    character_states: list[CharacterState] = Field(default_factory=list)
    open_threads: StrList = Field(default_factory=list)
    established_facts_this_session: StrList = Field(default_factory=list)

    def get_state(self, character_id: str) -> CharacterState | None:
        for s in self.character_states:
            if s.character_id == character_id:
                return s
        return None

    def apply_delta(self, delta: WorldStateDelta) -> None:
        if delta.current_scene:
            self.current_scene = delta.current_scene
        if delta.timeline:
            self.timeline = delta.timeline
        for cs_delta in delta.character_states:
            existing = self.get_state(cs_delta.character_id)
            if existing:
                if cs_delta.location:
                    existing.location = cs_delta.location
                if cs_delta.physical:
                    existing.physical = cs_delta.physical
                if cs_delta.emotional:
                    existing.emotional = cs_delta.emotional
                existing.knows = _merge_ordered(existing.knows, cs_delta.knows)
                existing.possesses = _merge_ordered(existing.possesses, cs_delta.possesses)
                existing.active_goals = cs_delta.active_goals or existing.active_goals
            else:
                self.character_states.append(cs_delta)
        if delta.open_threads:
            # 合并而不是整体替换：旧悬念不应该凭空消失
            self.open_threads = _merge_ordered(self.open_threads, delta.open_threads)
        if delta.established_facts_this_session:
            self.established_facts_this_session = _merge_ordered(
                self.established_facts_this_session,
                delta.established_facts_this_session,
            )


class WorldStateDelta(BaseModel):
    current_scene: str = ""
    timeline: str = ""
    character_states: list[CharacterState] = Field(default_factory=list)
    open_threads: StrList = Field(default_factory=list)
    established_facts_this_session: StrList = Field(default_factory=list)

    @field_validator("character_states", mode="before")
    @classmethod
    def _drop_bad_states(cls, value: object) -> object:
        # 允许 LLM 返回 null / 单对象
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value

    @field_validator("open_threads", "established_facts_this_session", mode="before")
    @classmethod
    def _normalize_lines(cls, value: object) -> list[str]:
        return ensure_str_list(value)
