"""云端 TTS（OpenRouter `openai/gpt-audio-mini`）。

全部用 `httpx.MockTransport` 打桩：不发真实请求、不花钱，但请求体（念法写进系统提示、
`modalities`/`audio.format`）、base64 落盘、`usage.cost` 记账、错误映射都按真实形状验证。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from videogen.providers.tts.base import TTSProviderError, TTSRequest
from videogen.providers.tts.openrouter_audio import OpenRouterTTSProvider
from videogen.services import voice_catalog

#: 假 pcm16 字节（provider 会把它封成 WAV）：时长由打桩的 probe_duration 提供
MP3_BYTES = bytes(range(20)) * 4
MP3_B64 = base64.b64encode(MP3_BYTES).decode()


@pytest.fixture(autouse=True)
def stub_probe(monkeypatch):
    """provider 用 ffprobe 读时长：测试里打桩，免得依赖本机 ffmpeg。"""
    from videogen.providers.tts import openrouter_audio

    async def fake_probe(path):
        return 1.5

    monkeypatch.setattr(openrouter_audio, "probe_duration", fake_probe)


def make_provider(handler, **kwargs) -> OpenRouterTTSProvider:
    return OpenRouterTTSProvider(
        api_key="test-key",
        model="openai/gpt-audio-mini",
        base_url="https://openrouter.test/api/v1",
        max_http_retries=kwargs.pop("max_http_retries", 1),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def sse(*events: dict) -> bytes:
    """把若干事件拼成 SSE 流（真实接口要求 stream=true）。"""
    lines = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return (lines + "data: [DONE]\n\n").encode()


def ok_response(cost: float | None = 0.00042, transcript: str = "薇儿，为什么？") -> httpx.Response:
    """真实形状：音频按 base64 分块流式返回，最后一块带 usage。"""
    half = len(MP3_B64) // 2
    events: list[dict] = [
        {"choices": [{"delta": {"audio": {"data": MP3_B64[:half]}}}]},
        {"choices": [{"delta": {"audio": {"data": MP3_B64[half:]}}}]},
        {"choices": [{"delta": {"audio": {"transcript": transcript}}}]},
    ]
    if cost is not None:
        events.append(
            {
                "choices": [{"delta": {}}],
                "usage": {"prompt_tokens": 31, "completion_tokens": 120, "cost": cost},
            }
        )
    return httpx.Response(200, content=sse(*events))


def test_requires_api_key(monkeypatch):
    from videogen.config import get_settings

    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")
    with pytest.raises(TTSProviderError):
        OpenRouterTTSProvider(api_key=" ")


@pytest.mark.asyncio
async def test_synthesize_sends_instruction_and_writes_mp3(tmp_path):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return ok_response()

    provider = make_provider(handler)
    output = tmp_path / "scene_1_shot_3.wav"
    result = await provider.synthesize(
        TTSRequest(
            clip_id="scene_1_shot_3",
            text="薇儿，为什么？",
            voice="nova",
            instruction="压抑的哭腔，语速慢，句尾发颤",
            output_path=output,
        )
    )

    payload = seen["json"]
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    assert payload["modalities"] == ["text", "audio"]
    # 流式只支持 pcm16（真实 400 反馈），容器由我们自己封 WAV
    assert payload["audio"] == {"voice": "nova", "format": "pcm16"}
    assert payload["usage"] == {"include": True}
    # 念法必须写进系统提示（云端 TTS 的情绪表达力全靠这句）
    system = payload["messages"][0]["content"]
    assert "压抑的哭腔，语速慢，句尾发颤" in system
    assert "原样朗读" in system
    assert "一个字都不许增加" in system  # 真实翻车：换个音色就自己加戏
    # 台词只在 user 消息里，且没有被改写
    assert payload["messages"][1]["content"] == "薇儿，为什么？"

    assert result.path == output
    assert output.read_bytes().endswith(MP3_BYTES)  # 前面是 WAV 头
    assert result.usd == pytest.approx(0.00042)
    assert result.duration_sec == 1.5  # 时长由 ffprobe 补齐
    assert result.transcript == "薇儿，为什么？"


@pytest.mark.asyncio
async def test_missing_instruction_falls_back_to_natural_reading(tmp_path):
    """没有逐条念法时用兜底（去播音腔），而不是留空。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return ok_response()

    provider = make_provider(handler)
    await provider.synthesize(
        TTSRequest(clip_id="c", text="你好。", voice="nova", output_path=tmp_path / "c.mp3")
    )
    system = seen["json"]["messages"][0]["content"]
    assert "自然" in system and "不要播音腔" in system


@pytest.mark.asyncio
async def test_pcm_is_wrapped_into_a_playable_wav(tmp_path):
    """流式接口只给 pcm16：provider 自己封 WAV 头，落盘必须是可播放/可探测的 wav。"""
    import wave

    provider = make_provider(lambda request: ok_response())
    result = await provider.synthesize(
        TTSRequest(clip_id="c", text="你好。", voice="nova", output_path=tmp_path / "c.mp3")
    )
    assert result.path.suffix == ".wav"
    with wave.open(str(result.path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 24000
        assert handle.readframes(10) == MP3_BYTES[:20]


@pytest.mark.asyncio
async def test_non_wav_container_falls_back_with_a_warning(tmp_path, caplog):
    provider = make_provider(lambda request: ok_response(), audio_format="mp3")
    result = await provider.synthesize(
        TTSRequest(clip_id="c", text="你好。", voice="nova", output_path=tmp_path / "c.mp3")
    )
    assert result.path.suffix == ".wav"


@pytest.mark.asyncio
async def test_request_is_streaming(tmp_path):
    """真实 400 反馈：`Audio output requires stream: true`。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return ok_response()

    provider = make_provider(handler)
    await provider.synthesize(
        TTSRequest(clip_id="c", text="你好。", voice="nova", output_path=tmp_path / "c.mp3")
    )
    assert seen["json"]["stream"] is True


@pytest.mark.asyncio
async def test_stream_error_event_is_reported(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse({"error": {"message": "voice not supported", "code": 400}}),
        )

    provider = make_provider(handler)
    with pytest.raises(TTSProviderError) as exc:
        await provider.synthesize(
            TTSRequest(clip_id="c", text="你好。", voice="nova", output_path=tmp_path / "c.mp3")
        )
    assert "voice not supported" in str(exc.value)


@pytest.mark.asyncio
async def test_text_only_response_is_an_error(tmp_path):
    """模型只回文本不回音频：必须报错，不能产出空文件假装成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "好的"}}]}))

    provider = make_provider(handler)
    with pytest.raises(TTSProviderError) as exc:
        await provider.synthesize(
            TTSRequest(clip_id="c", text="你好。", voice="nova", output_path=tmp_path / "c.mp3")
        )
    assert "没有返回音频" in str(exc.value)
    assert not (tmp_path / "c.mp3").exists()


@pytest.mark.asyncio
async def test_http_error_is_reported_without_leaking_key(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized: bad key sk-or-v1-abcdefghijklmnop")

    provider = make_provider(handler)
    with pytest.raises(TTSProviderError) as exc:
        await provider.synthesize(
            TTSRequest(clip_id="c", text="你好。", voice="nova", output_path=tmp_path / "c.mp3")
        )
    message = str(exc.value)
    assert "401" in message
    assert "sk-or-v1-abcdefghijklmnop" not in message


@pytest.mark.asyncio
async def test_empty_text_is_rejected_before_any_request(tmp_path):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return ok_response()

    provider = make_provider(handler)
    with pytest.raises(TTSProviderError):
        await provider.synthesize(
            TTSRequest(
                clip_id="c",
                text="（转身离开）",
                voice="nova",
                output_path=tmp_path / "c.mp3",
            )
        )
    assert called["n"] == 0


# ── 云端选角 ──────────────────────────────────────────


def test_cloud_casting_replaces_edge_voice_names():
    """换引擎后音色名必须换成云端音色（Edge 名字发给云端接口会 400）。"""
    from tests.conftest import _casting_storyboard

    bible, storyboard = _casting_storyboard()
    cloud = voice_catalog.cast_voice_map(storyboard, bible, target="openrouter")
    assert all(voice_catalog.is_cloud_voice(v) for v in cloud.voice_map.values())
    # 4 个说话人拿到的云端音色互不相同（云端池足够大）
    assert len(set(cloud.voice_map.values())) == len(cloud.voice_map)

    edge = voice_catalog.cast_voice_map(
        storyboard, bible, target="edge", existing=cloud.voice_map
    )
    assert all(not voice_catalog.is_cloud_voice(v) for v in edge.voice_map.values())
    assert any("不适用于当前 TTS 引擎" in note for note in edge.notes)


def test_cloud_voice_for_unknown_name_falls_back_by_gender():
    assert voice_catalog.cloud_voice_for("zh-CN-XiaoxiaoNeural") == "nova"
    assert voice_catalog.cloud_voice_for("nova") == "nova"
    assert voice_catalog.cloud_voice_for("some-custom-voice", gender="male") == "ballad"


def test_cloud_catalog_rows_are_offered_to_ui():
    rows = voice_catalog.catalog_rows(cloud=True)
    ids = {row["id"] for row in rows}
    assert "nova" in ids and "onyx" in ids
    assert all(row["locale"] == "cloud" for row in rows)
    # Edge 表里带上云端对照，方便前端展示「换引擎后是谁的声音」
    edge_rows = {row["id"]: row for row in voice_catalog.catalog_rows()}
    assert edge_rows["zh-CN-XiaoxiaoNeural"]["cloud"] == "nova"


# ── 加戏检测与时长城守 ─────────────────────────────────────


def test_transcript_check_detects_added_lines():
    from videogen.providers.tts.openrouter_audio import _transcript_is_verbatim

    assert _transcript_is_verbatim("薇儿，为什么？", "薇儿，为什么？")
    assert _transcript_is_verbatim("薇儿……为什么……", "薇儿，为什么？")
    assert _transcript_is_verbatim("", "薇儿，为什么？")  # 没回转写 = 不误杀
    # 真实样本：把台词演成了一段独白（还念出了括号提示）
    assert not _transcript_is_verbatim(
        "薇儿……为什么……难道就这样吗？我……我真的……很……无法理解……"
        "（语气颤抖，声音渐弱）……为什么……你要离开……（深呼吸）",
        "薇儿，为什么？",
    )


@pytest.mark.asyncio
async def test_improvised_audio_triggers_a_stricter_retry(tmp_path):
    """第一次转写明显加戏 → 带更严格的提示重试一次，用第二次的结果。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return ok_response(transcript="薇儿……为什么……难道就这样吗？我真的很无法理解……")
        return ok_response(transcript="薇儿，为什么？")

    provider = make_provider(handler)
    result = await provider.synthesize(
        TTSRequest(clip_id="c", text="薇儿，为什么？", voice="nova", output_path=tmp_path / "c.wav")
    )
    assert len(calls) == 2
    assert "只念下面这一句" in calls[1]["messages"][0]["content"]
    assert result.transcript == "薇儿，为什么？"


@pytest.mark.asyncio
async def test_absurdly_long_line_fails_instead_of_shipping(tmp_path, monkeypatch):
    """两分钟独白（一次只说 5 个字）不能悄悄进成片 —— 直接失败让人看见。"""
    from videogen.providers.tts import openrouter_audio

    async def fake_probe(path):
        return 60.0

    monkeypatch.setattr(openrouter_audio, "probe_duration", fake_probe)
    provider = make_provider(
        lambda request: ok_response(transcript="薇儿……为什么……难道就这样吗？" * 20)
    )
    with pytest.raises(TTSProviderError) as exc:
        await provider.synthesize(
            TTSRequest(
                clip_id="c", text="薇儿，为什么？", voice="shimmer", output_path=tmp_path / "c.wav"
            )
        )
    assert "时长异常" in str(exc.value)
