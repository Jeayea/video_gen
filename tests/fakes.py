from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from videogen.models.audio import BgmTrack
from videogen.models.video import MediaKind
from videogen.providers.bgm.base import BgmLibrary
from videogen.providers.image.base import (
    GeneratedImage,
    ImageProvider,
    ImageProviderError,
    ImageRequest,
)
from videogen.providers.llm.base import (
    LLMError,
    LLMMessage,
    LLMProvider,
    message_image_count,
    message_text,
)
from videogen.providers.llm.openrouter import OpenRouterProvider
from videogen.providers.tts.base import (
    GeneratedAudio,
    TTSProvider,
    TTSProviderError,
    TTSRequest,
)
from videogen.providers.video.base import (
    GeneratedClip,
    VideoProvider,
    VideoProviderError,
    VideoRequest,
)
from videogen.utils.text import clean_spoken_text

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
        voice_text: str = "补写的旁白",
        voice_empty_shots: set[str] | None = None,
        voice_rate: str = "-10%",
        voice_instruction: str = "压抑的哭腔，语速慢，句尾发颤",
        story_chapters: int = 20,
        chapter_scenes: int = 2,
    ):
        self.scene_duration_sec = scene_duration_sec
        self.scenes = scenes
        #: "bible" | "outline" | "scene:<id>" | "storyboard:<id>" | "audio_script"
        self.fail_at = fail_at
        self.include_stage_direction = include_stage_direction
        self.video_prompt_with_ids = video_prompt_with_ids
        self.calls: list[list[LLMMessage]] = []
        #: 成本记账用：假装这些调用花了 token
        self.token_estimate = 3
        #: 编排层每个阶段结束后应该调用一次 aclose（防「复用连接」变成「泄漏连接」）
        self.close_calls = 0
        self.model = "fake-model"
        self.models_used: list[str] = []
        #: 配音文案相关：返回的文案模板 / 故意留空的镜头 / 给的语速建议
        self.voice_text = voice_text
        self.voice_empty_shots = set(voice_empty_shots or ())
        self.voice_rate = voice_rate
        self.voice_instruction = voice_instruction
        #: 长篇大纲：骨架给几章 / 每章展开几场
        self.story_chapters = story_chapters
        self.chapter_scenes = chapter_scenes
        #: 配音文案调用里收到的图片总数（验证「真的把画面发过去了」）
        self.image_count = 0
        #: 收到过图片的调用次数
        self.vision_calls = 0
        #: 配音文案各模式的调用次数
        self.voice_modes: list[str] = []

    # ── LLMProvider 接口 ──────────────────────────────

    @property
    def name(self) -> str:
        return "fake"

    def with_model(self, model: str) -> FakeLLM:
        self.model = model or self.model
        self.models_used.append(model)
        return self

    async def aclose(self) -> None:
        self.close_calls += 1

    async def chat(self, messages: list[LLMMessage], **kwargs: Any) -> str:
        self.calls.append(messages)
        self._maybe_fail("chat")
        reply = "假 logline：主角绝地翻盘"
        self._record(messages, reply)
        return reply

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
        self._record(messages, json.dumps(payload, ensure_ascii=False))
        if schema is None:
            return payload
        return schema.model_validate(payload)

    def _record(self, messages: list[LLMMessage], reply: str) -> None:
        """按字符数粗算 token，让成本链路在离线测试里也有数可记。"""
        self.record_usage(
            input_tokens=sum(len(message_text(m)) for m in messages) // self.token_estimate,
            output_tokens=len(reply) // self.token_estimate + 1,
        )

    # ── 内部 ──────────────────────────────────────────

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_at == stage:
            raise LLMError(f"fake failure at {stage}")

    def _respond(self, messages: list[LLMMessage]) -> dict:
        system = message_text(messages[0])
        user = message_text(messages[-1])

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
        if "长篇剧集的总编剧" in system:
            return self._story_outline(system, user)
        if "分场编剧" in system:
            return self._chapter_scenes(user)
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
        if "配音导演" in system:
            self._maybe_fail("audio_script")
            return self._voice_script(messages)
        return {}

    def _story_outline(self, system: str, user: str) -> dict:
        """长篇大纲：骨架一次给全，正文按批扩写（id 从用户提示里照抄）。"""
        if "你只写「骨架」" in system:
            self._maybe_fail("story_spine")
            # 真实模型会按提示里的章数写；假模型也照做，否则测不出「换了章数」
            match = re.search(r"写 (\d+) 章的长篇剧集骨架", user)
            count = int(match.group(1)) if match else self.story_chapters
            return {
                "title": "假长篇剧名",
                "logline": "一个被裁掉的人，把整座城市裁回去",
                "synopsis": "假梗概" * 40,
                "theme": "不甘",
                "chapters": [
                    {
                        "title": f"第 {i} 章",
                        "brief": f"第 {i} 章的转折：局势变了第 {i} 次",
                    }
                    for i in range(1, count + 1)
                ],
            }
        self._maybe_fail("story_expand")
        ids = re.findall(r"^- \[(ch_\d+)\]", user, flags=re.MULTILINE)
        if not ids:
            ids = re.findall(r"\[(ch_\d+)\]", user)
        return {
            "chapters": [
                {
                    "id": chapter_id,
                    "title": f"第 {chapter_id.split('_')[-1]} 章",
                    "summary": f"{chapter_id} 的正文："
                    + "谁想要什么被什么挡住，做了选择，付出代价。" * 3,
                    "beats": [f"{chapter_id} 情节点 1", f"{chapter_id} 情节点 2"],
                    "hook": f"{chapter_id} 章末钩子",
                    "characters": ["char_1"],
                    "locations": ["loc_1"],
                    "scene_budget": self.chapter_scenes,
                    "duration_budget_sec": self.chapter_scenes * 12,
                }
                for chapter_id in ids
            ]
        }

    def _chapter_scenes(self, user: str) -> dict:
        """把选中的章展开成场次（每章按配置给若干场）。"""
        ids = re.findall(r"^## (ch_\d+)｜", user, flags=re.MULTILINE)
        scenes = []
        for chapter_id in ids:
            for index in range(1, self.chapter_scenes + 1):
                scenes.append(
                    {
                        "chapter_id": chapter_id,
                        "title": f"{chapter_id} 第 {index} 场",
                        "brief": f"{chapter_id} 第 {index} 场：地点+环境细节，人物目标与阻力",
                        "environment": "冷白顶灯 / 玻璃隔断 / 桌上摊开的杂志",
                        "duration_sec": 12,
                    }
                )
        return {"scenes": scenes}

    def _voice_script(self, messages: list[LLMMessage]) -> dict:
        """按用户提示里列出的镜头 id 逐条给文案（可配置留空 / 语速建议）。"""
        user = message_text(messages[-1])
        images = message_image_count(messages[-1])
        self.image_count += images
        if images:
            self.vision_calls += 1
        mode = (
            "full"
            if "完整配音文案" in user
            else "perform"
            if "只给念法" in user
            else "polish"
            if "改写已有文案" in user
            else "narration"
        )
        self.voice_modes.append(mode)

        lines = []
        for shot_id in re.findall(r"### 镜头 (\S+?)（", user):
            if shot_id in self.voice_empty_shots:
                lines.append({"shot_id": shot_id, "text": "", "note": "刻意留白"})
                continue
            lines.append(
                {
                    # perform 模式一个字都不改文案（只给念法）
                    "text": "" if mode == "perform" else f"{self.voice_text}（{shot_id}）",
                    "shot_id": shot_id,
                    "rate": self.voice_rate if mode == "full" else "",
                    "instruction": self.voice_instruction if mode in {"full", "perform"} else "",
                    "note": f"{mode} 模式生成",
                }
            )
        return {"lines": lines}

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
        supports_reference_image: bool = False,
        price_usd_per_sec: float | None = None,
        duration_options: list[float] | None = None,
        actual_cost_usd: float | None = None,
    ):
        self.supports_reference_image = supports_reference_image
        self.requests: list[VideoRequest] = []
        self.fail_shots = set(fail_shots or ())
        self.fail_times = fail_times
        self.delay = delay
        self.media_kind = media_kind
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.closed = 0
        #: 成本前置用例用：模拟真实模型的「最短档位 + 按秒单价」
        self.price_usd_per_sec = price_usd_per_sec
        self.duration_options_sec = sorted(float(x) for x in (duration_options or []))
        #: 每个片段「实际扣费」，用来测记账（真实 provider 会回传这个值）
        self.actual_cost_usd = actual_cost_usd

    async def planned_seconds(self, requested_sec: float) -> float:
        """有档位就抬到最近的一档（与 OpenRouter 的 `_clamp_duration` 同语义）。"""
        if not self.duration_options_sec:
            return max(0.0, float(requested_sec))
        for option in self.duration_options_sec:
            if option >= requested_sec:
                return option
        return self.duration_options_sec[-1]

    async def duration_options(self) -> list[float]:
        return list(self.duration_options_sec)

    async def price_per_second(self, *, resolution: str = "") -> float | None:
        return self.price_usd_per_sec

    def attempts_for(self, shot_id: str) -> int:
        return self.calls.count(shot_id)

    async def generate(self, request: VideoRequest) -> GeneratedClip:
        self.calls.append(request.shot_id)
        self.requests.append(request)
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
                actual_cost_usd=self.actual_cost_usd,
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


class FakeTTSProvider(TTSProvider):
    """离线假配音 provider：写一个小文件代替真实音频。

    时长按字数算，方便断言「音画对齐」的结果；可配置失败与延时。
    """

    name = "fake"
    offline = True

    def __init__(
        self,
        *,
        fail_clips: set[str] | None = None,
        fail_times: int = 0,
        delay: float = 0.0,
        sec_per_char: float = 0.2,
        media_kind: MediaKind = MediaKind.AUDIO,
    ):
        self.fail_clips = set(fail_clips or ())
        self.fail_times = fail_times
        self.delay = delay
        self.sec_per_char = sec_per_char
        self.media_kind = media_kind
        self.calls: list[str] = []
        self.voices: dict[str, str] = {}
        self.active = 0
        self.max_active = 0
        self.closed = 0

    def attempts_for(self, clip_id: str) -> int:
        return self.calls.count(clip_id)

    async def synthesize(self, request: TTSRequest) -> GeneratedAudio:
        self.calls.append(request.clip_id)
        self.voices[request.clip_id] = request.voice
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            should_fail = (
                request.clip_id in self.fail_clips
                and self.attempts_for(request.clip_id) <= self.fail_times
            )
            if should_fail:
                raise TTSProviderError(f"fake 配音失败 clip={request.clip_id}")
            target = Path(request.output_path)
            if self.media_kind == MediaKind.PLACEHOLDER:
                target = target.with_suffix(".placeholder.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-audio")
            duration = round(len(clean_spoken_text(request.text)) * self.sec_per_char, 3)
            return GeneratedAudio(
                path=target,
                media_kind=self.media_kind,
                duration_sec=max(0.6, duration),
                voice=request.voice,
            )
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        self.closed += 1


class AlwaysFailTTSProvider(FakeTTSProvider):
    """所有条目都失败，用来验证「零可用配音」时的收尾。"""

    async def synthesize(self, request: TTSRequest) -> GeneratedAudio:
        self.calls.append(request.clip_id)
        raise TTSProviderError("全量配音失败")


class FakeBgmLibrary(BgmLibrary):
    """固定返回一首（可指定路径）的假曲库。"""

    name = "fake"

    def __init__(self, *, path: str = "/nonexistent/fake_bgm.mp3", moods=("紧张", "压抑")):
        self._tracks = [
            BgmTrack(id="fake_bgm", title="假BGM", path=path, moods=list(moods), duration_sec=30)
        ]

    def tracks(self) -> list[BgmTrack]:
        return list(self._tracks)


class FakeImageProvider(ImageProvider):
    """离线假图片 provider：写一张极小的真 PNG，可指定单价/失败。"""

    name = "fake"
    model = "fake-image"
    supports_reference_image = True

    def __init__(
        self,
        *,
        actual_cost_usd: float | None = 0.04,
        fail_subjects: set[str] | None = None,
        suffix: str = ".png",
        delay_sec: float = 0.0,
    ) -> None:
        self.actual_cost_usd = actual_cost_usd
        self.fail_subjects = set(fail_subjects or set())
        self.suffix = suffix
        #: 制造真实 provider 那样的「等待」：并发批量时如果没有锁，就会互相覆盖圣经
        self.delay_sec = delay_sec
        self.requests: list[ImageRequest] = []
        self.closed = 0

    async def generate(self, request: ImageRequest) -> GeneratedImage:
        self.requests.append(request)
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if request.subject_id in self.fail_subjects:
            raise ImageProviderError(f"fake 图片失败 subject={request.subject_id}")
        target = Path(request.output_path)
        if self.suffix and target.suffix != self.suffix:
            target = target.with_suffix(self.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_PNG_1PX)
        return GeneratedImage(
            path=target, media_type="image/png", actual_cost_usd=self.actual_cost_usd
        )

    async def aclose(self) -> None:
        self.closed += 1


#: 1x1 透明 PNG（合法的真 PNG，够用来验证「落盘 + 预览接口」）
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
