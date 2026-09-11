from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from videogen.models.video import MediaKind
from videogen.providers.llm.base import LLMError, LLMMessage, LLMProvider
from videogen.providers.llm.openrouter import OpenRouterProvider
from videogen.providers.video.base import (
    GeneratedClip,
    VideoProvider,
    VideoProviderError,
    VideoRequest,
)

SCENE_ID_RE = re.compile(r"场次(?:\s*ID)?[:：]\s*(\S+)")


class FakeLLM(LLMProvider):
    """离线假 LLM：按 system prompt 判断当前阶段，返回结构化的假数据。

    不联网、不确定、不花钱，用来把整条流水线跑成确定性行为。
    """

    def __init__(
        self,
        *,
        scene_duration_sec: int | str = 45,
        scenes: int = 5,
        fail_at: str | None = None,
        include_stage_direction: bool = True,
        video_prompt_with_ids: bool = False,
    ):
        self.scene_duration_sec = scene_duration_sec
        self.scenes = scenes
        self.fail_at = fail_at  # "bible" | "outline" | "scene:<id>" | "storyboard:<id>"
        self.include_stage_direction = include_stage_direction
        self.video_prompt_with_ids = video_prompt_with_ids
        self.calls: list[list[LLMMessage]] = []

    # ── LLMProvider 接口 ──────────────────────────────

    async def chat(self, messages: list[LLMMessage], **kwargs: Any) -> str:
        self.calls.append(messages)
        self._maybe_fail("chat")
        return "假 logline：主角绝地翻盘"

    async def chat_json(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel] | None = None,
        temperature: float = 0.5,
        max_tokens: int = 8192,
        retries: int = 2,
    ) -> Any:
        self.calls.append(messages)
        payload = self._respond(messages)
        if schema is None:
            return payload
        return schema.model_validate(payload)

    # ── 内部 ──────────────────────────────────────────

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_at == stage:
            raise LLMError(f"fake failure at {stage}")

    def _respond(self, messages: list[LLMMessage]) -> dict:
        system = messages[0].content
        user = messages[-1].content

        # 用各阶段 system prompt 里独一无二的锚点判断，避免误匹配
        if "资深短剧策划" in system:
            self._maybe_fail("bible")
            return self._bible()
        if "大纲策划" in system:
            self._maybe_fail("outline")
            return {
                "scenes": [
                    {"title": f"第 {i} 场", "brief": f"第 {i} 场概述"}
                    for i in range(1, self.scenes + 1)
                ]
            }
        if "专业分镜师" in system:
            scene_id = SCENE_ID_RE.search(user)
            sid = scene_id.group(1) if scene_id else "scene_1"
            self._maybe_fail(f"storyboard:{sid}")
            return self._shots(sid)
        if "逐场编写短剧剧本" in system:
            scene_id = SCENE_ID_RE.search(user)
            sid = scene_id.group(1) if scene_id else "scene_1"
            self._maybe_fail(f"scene:{sid}")
            return self._scene(sid)
        return {}

    def _bible(self) -> dict:
        return {
            "characters": [
                {
                    "id": "char_1",
                    "name": "林晚",
                    "aliases": ["晚晚"],
                    "age": "24岁",  # 故意给脏数据，验证容错
                    "role": "主角",
                    "description": "女白领",
                    "visual_prompt": "a young Chinese woman, long black hair, office attire",
                    "personality": "坚韧",  # 故意给字符串而非数组
                    "relationships": [{"target": "char_2", "type": "敌对", "note": "抢项目"}],
                },
                {
                    "id": "char_2",
                    "name": "赵明远",
                    "role": "反派",
                    "visual_prompt": "a middle-aged Chinese man in a suit",
                },
            ],
            "settings": [
                {
                    "id": "loc_1",
                    "name": "星辰科技办公室",
                    "visual_prompt": "modern open-plan office",
                }
            ],
            "facts": [{"id": "fact_1", "content": "母亲的医药费是 30万", "type": "number"}],
            "style": {"genre": "都市复仇", "tone": "压抑中带爽感", "pacing": "快节奏"},
        }

    def _scene(self, scene_id: str) -> dict:
        line = "这个项目是我做的。"
        if self.include_stage_direction:
            line = "（咬牙）这个项目是我做的。"  # 舞台提示应被清洗掉
        return {
            "heading": f"INT. 办公室 - 日（第{scene_id[-1]}天）",
            "summary": f"{scene_id} 的剧情概述",
            "dialogue": [
                {"speaker": "char_1", "line": line},
                {"speaker": "char_2", "line": "你？别开玩笑了。"},
            ],
            "duration_sec": self.scene_duration_sec,
            "emotion": "压抑",
            "world_state_delta": {
                "current_scene": scene_id,
                "timeline": "故事第 1 天",
                "character_states": [
                    {
                        "character_id": "char_1",
                        "location": "loc_1",
                        "emotional": "愤怒",
                        "knows": ["赵明远抢了她的方案"],
                        "active_goals": ["证明方案是自己的"],
                    }
                ],
                "open_threads": ["林晚还没找到证据"],
                "established_facts_this_session": [f"{scene_id} 中林晚当众说出了工号 A-2071"],
            },
        }

    def _shots(self, scene_id: str) -> dict:
        prompt = "medium shot, a young Chinese woman in a modern office, cinematic lighting"
        if self.video_prompt_with_ids:
            prompt = "medium shot of char_1 in loc_1, cinematic lighting"
        return {
            "shots": [
                {
                    "shot_no": 1,
                    "duration_sec": "约4秒",  # 脏数据
                    "camera": "中景 · 固定",
                    "visual_description": "林晚站在会议室中央",
                    "character_in_frame": "char_1",  # 故意给字符串
                    "mood": "紧张",
                    "video_prompt": prompt,
                    "dialogue_text": "这个项目是我做的。",
                    "dialogue_speaker": "char_1",
                },
                {
                    "shot_no": 2,
                    "duration_sec": 4.5,  # 故意给小数
                    "camera": "近景 · 推",
                    "visual_description": "赵明远冷笑",
                    "character_in_frame": ["char_2"],
                    "mood": "轻蔑",
                    "video_prompt": "close up, a middle-aged Chinese man smirking",
                    "narration": None,
                },
            ]
        }


class FlakyJSONProvider(OpenRouterProvider):
    """前 n 次返回坏输出，之后返回好 JSON —— 用来验证真实的 chat_json 重试闭环。

    刻意不调用 super().__init__：这条路径不需要 API key，也不该联网。
    """

    def __init__(self, bad_responses: list[str], good_payload: dict):
        self.bad_responses = list(bad_responses)
        self.good_payload = good_payload
        self.attempts = 0

    async def chat(self, messages: list[LLMMessage], **kwargs: Any) -> str:
        self.attempts += 1
        if self.bad_responses:
            return self.bad_responses.pop(0)
        return json.dumps(self.good_payload, ensure_ascii=False)


class FakeVideoProvider(VideoProvider):
    """离线假视频 provider：写一个小文件代替真实转码。

    可以配置：指定镜头始终失败、前 N 次失败、人为延时（用来验证并发/取消）。
    """

    name = "fake"
    requires_ffmpeg = False

    def __init__(
        self,
        *,
        fail_shots: set[str] | None = None,
        fail_times: int = 0,
        delay: float = 0.0,
        media_kind: MediaKind = MediaKind.VIDEO,
    ):
        self.fail_shots = set(fail_shots or ())
        self.fail_times = fail_times
        self.delay = delay
        self.media_kind = media_kind
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.closed = 0

    def attempts_for(self, shot_id: str) -> int:
        return self.calls.count(shot_id)

    async def generate(self, request: VideoRequest) -> GeneratedClip:
        self.calls.append(request.shot_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            should_fail = (
                request.shot_id in self.fail_shots
                and self.attempts_for(request.shot_id) <= self.fail_times
            )
            if should_fail:
                raise VideoProviderError(f"fake 生成失败 shot={request.shot_id}")
            target = Path(request.output_path)
            if self.media_kind == MediaKind.PLACEHOLDER:
                target = target.with_suffix(".placeholder.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-clip")
            return GeneratedClip(
                path=target,
                media_kind=self.media_kind,
                duration_sec=request.duration_sec,
                task_id=f"fake-{request.shot_id}",
            )
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        self.closed += 1


class AlwaysFailVideoProvider(FakeVideoProvider):
    """所有镜头都失败，用来验证「零可用片段」时的收尾。"""

    async def generate(self, request: VideoRequest) -> GeneratedClip:
        self.calls.append(request.shot_id)
        raise VideoProviderError("全量失败")
