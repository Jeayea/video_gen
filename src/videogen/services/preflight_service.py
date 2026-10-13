"""成本前置（preflight）：动手花钱**之前**把账算清楚。

真实视频模型按秒计费、还有最短档位（H3 最短 5 秒），所以「分镜 45 秒」很可能
实际要买 75 秒。这个服务回答四个问题：

1. 这次点击会生成几个片段？实际被计费多少秒？大概多少钱？
2. 会不会把已经生成好的片段全部重跑（换模型 / 换 provider / 上游文本变了）？
3. 预算够不够？不够还差多少？
4. 如果先合并短镜头，能省多少？（只算不写，合并要用户确认）

金额一律用 provider 的真实单价（`price_per_second`）与真实档位（`planned_seconds`），
问不到才退回配置单价 —— 和真正扣费时的口径保持一致。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from videogen.config import Settings, get_settings
from videogen.models.audio import AudioManifest
from videogen.models.cost import CostReport
from videogen.models.project import Project
from videogen.models.storyboard import Storyboard
from videogen.models.video import ClipStatus, MediaKind, VideoManifest
from videogen.providers.video.base import VideoProvider, VideoProviderError
from videogen.services import (
    audio_script_service,
    cost_service,
    storyboard_service,
    video_service,
)
from videogen.services.storyboard_service import Segment
from videogen.utils.text import count_speech_chars

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIONS",
    "UnavailableVideoProvider",
    "audio_plan",
    "budget_projection",
    "compose_plan",
    "preflight",
    "video_plan",
]


class UnavailableVideoProvider(VideoProvider):
    """provider 初始化失败（没装依赖 / 没配 key）时的空实现。

    只用于**估算**：老实说自己不可用、单价问不到、档位未知，让 preflight 退回配置单价
    并给出告警 —— 绝不假装自己是 mock 或 openrouter，免得账单口径骗人。
    """

    name = "unavailable"
    model = ""

    async def generate(self, request):  # pragma: no cover - 只用于估算，不会被调用
        raise VideoProviderError("视频 provider 不可用，无法生成片段")

#: 可以做成本前置的动作
ACTIONS = ("all", "video", "audio", "compose")


#: 「这几段要画几张关键帧、多少钱」——由 pipeline 注入（它知道项目、圣经与目录）
KeyframeEstimator = Callable[[list[Segment]], Awaitable[dict]]

def budget_projection(report, upcoming_usd: float) -> dict:
    """预算投影：已花 + 本次预估 vs 预算，差多少。"""
    total = report.total_usd
    budget = report.budget_usd
    remaining = None if budget <= 0 else round(budget - total, 6)
    projected = round(total + max(0.0, upcoming_usd), 6)
    shortfall = 0.0 if budget <= 0 else round(max(0.0, projected - budget), 6)
    return {
        "budget_usd": budget,
        "spent_usd": total,
        "remaining_usd": remaining,
        "upcoming_usd": round(max(0.0, upcoming_usd), 6),
        "projected_usd": projected,
        "shortfall_usd": shortfall,
        "over_budget": shortfall > 0,
        # 预算为 0 = 不限制
        "limited": budget > 0,
    }


async def video_plan(
    storyboard: Storyboard | None,
    video: VideoManifest | None,
    provider: VideoProvider,
    settings: Settings | None = None,
    *,
    price_per_second: float | None = None,
    keyframe_estimator: KeyframeEstimator | None = None,
) -> dict:
    """算清楚这次「生成视频片段」会买多少秒、花多少钱，以及合并镜头能省多少。

    `keyframe_estimator`：只认首帧的模型要先画开场关键帧（按张计费），这笔钱算进
    `estimated_usd`，明细在 `keyframes`。
    """
    settings = settings or get_settings()
    prices = cost_service.PriceTable.from_settings(settings)
    shots = storyboard.shots if storyboard else []
    ready = video.ready_clips if video else []
    failed = video.failed_clips if video else []
    placeholders = video.placeholder_clips if video else []

    provider_model = getattr(provider, "model", "") or provider.name
    unit_price = price_per_second
    if unit_price is None:
        try:
            unit_price = await provider.price_per_second()
        except Exception:  # 问不到单价就退回配置，不能因此拒绝出预算
            logger.warning("视频单价查询失败，退回配置单价", exc_info=True)
    price = prices.video_per_second if unit_price is None else max(0.0, unit_price)
    price_source = "provider" if unit_price is not None else "config"

    options: list[float] = []
    try:
        options = list(await provider.duration_options())
    except Exception:
        logger.warning("视频时长档位查询失败，按无档位处理", exc_info=True)
    min_sec = options[0] if options else 1.0
    max_sec = options[-1] if options else 0.0

    # 已经就绪的片段要不要重跑：provider / 模型对不上就必须全部重生成。
    # 用 video_service 里那一份判断，保证「预估」与「真正跑」永远是同一个口径。
    switched = bool(video) and video_service.provider_switched(video, provider)
    pending = [] if switched else [
        shot for shot in shots if _needs_clip(shot, video)
    ]

    storyboard_seconds = sum(int(s.duration_sec) for s in shots)
    # 计费单位是「段」：段落分镜下一段只买一次档位；没编段的镜头各自成段（= 单镜计费）
    segments = storyboard_service.segments_of(storyboard) if storyboard else []
    pending_ids = {shot.id for shot in pending}
    billed_all = 0.0
    billed_pending = 0.0
    pending_segments: list[Segment] = []
    for segment in segments:
        seconds = await _planned(provider, segment.duration_sec)
        billed_all += seconds
        if any(sid in pending_ids for sid in segment.shot_ids):
            billed_pending += seconds
            pending_segments.append(segment)
    if switched:
        billed_pending = billed_all
        pending_segments = list(segments)
    segments_pending = len(pending_segments)
    segment_mode = any(len(seg.shots) > 1 for seg in segments)
    if not switched:
        # 段里缺一个镜头，整段都要重生成：待生成镜头按「整段」展开
        pending = [
            shot
            for seg in segments
            if any(sid in pending_ids for sid in seg.shot_ids)
            for shot in seg.shots
        ]

    to_generate = len(shots) if switched else len(pending)

    plan: dict = {
        "action": "video",
        "provider": provider.name,
        "model": provider_model,
        "price_per_second": round(price, 6),
        "price_source": price_source,
        "unit_kind": "second",
        "min_sec": round(min_sec, 3),
        "max_sec": round(max_sec, 3),
        "duration_options": [round(value, 3) for value in options],
        "shots_total": len(shots),
        "to_generate": to_generate,
        "segment_mode": segment_mode,
        "segments_total": len(segments),
        "segments_to_generate": segments_pending,
        "existing_ready": len(ready),
        "existing_failed": len(failed),
        "existing_placeholder": len(placeholders),
        "regenerate_all": switched,
        "regenerate_reason": (
            _switch_reason(video, provider.name, provider_model) if switched else ""
        ),
        "storyboard_seconds": storyboard_seconds,
        "billed_seconds": round(billed_pending, 3),
        "billed_seconds_if_all": round(billed_all, 3),
        "estimated_usd": round(billed_pending * price, 6),
        "estimated_usd_if_all": round(billed_all * price, 6),
        "inflation_seconds": round(max(0.0, billed_pending - _pending_seconds(pending)), 3),
        "warnings": [],
    }

    if options and to_generate:
        storyboard_secs = _pending_seconds(pending) if not switched else float(storyboard_seconds)
        extra = round(plan["billed_seconds"] - storyboard_secs, 3)
        if extra > 0.05:
            plan["warnings"].append(
                f"模型最短 {min_sec:g} 秒/镜头：分镜 {storyboard_secs:g} 秒实际要买 "
                f"{plan['billed_seconds']:g} 秒，多付 {extra:g} 秒 ≈ "
                f"${extra * price:.4f}"
            )
    if switched:
        plan["warnings"].append(
            f"provider / 模型变了（{plan['regenerate_reason']}），"
            f"已就绪的 {len(ready)} 个片段会全部重新生成"
        )

    plan["keyframes"] = {}
    if keyframe_estimator is not None and pending_segments:
        try:
            keyframes = dict(await keyframe_estimator(pending_segments))
        except Exception:  # 估不出关键帧不该让「看一眼要花多少」失败
            logger.warning("关键帧预估失败", exc_info=True)
            keyframes = {}
        plan["keyframes"] = keyframes
        extra_usd = float(keyframes.get("estimated_usd") or 0.0)
        if keyframes.get("to_generate"):
            plan["estimated_usd"] = round(plan["estimated_usd"] + extra_usd, 6)
            plan["warnings"].append(
                f"模型只认首帧：先画 {keyframes['to_generate']} 张开场关键帧（定妆照 + 场景图）"
                f" ≈ ${extra_usd:.2f}，计入本次预估"
            )

    # 段落分镜已经把短镜头按段计费了，「合并短镜头」不再省钱，也不该再劝人合并
    plan["merge_plan"] = {} if segment_mode else _merge_plan(
        storyboard,
        provider,
        price_per_second=price,
        min_sec=min_sec,
        max_sec=max_sec,
        baseline_usd=plan["estimated_usd"],
    )
    return plan


def _switch_reason(video: VideoManifest | None, provider_name: str, model: str) -> str:
    if not video:
        return "无历史清单"
    parts = []
    if video.provider and video.provider != provider_name:
        parts.append(f"{video.provider} → {provider_name}")
    if video.model and model and video.model != model:
        parts.append(f"{video.model} → {model}")
    return "，".join(parts) or "配置变化"


def _needs_clip(shot, video: VideoManifest | None) -> bool:
    if video is None:
        return True
    clip = video.get(shot.id)
    if clip is None:
        return True
    return clip.status != ClipStatus.READY or clip.media_kind == MediaKind.PLACEHOLDER


def _pending_seconds(pending: list) -> float:
    return float(sum(int(shot.duration_sec) for shot in pending))


async def _planned(provider: VideoProvider, seconds: float) -> float:
    try:
        return float(await provider.planned_seconds(float(seconds)))
    except Exception:  # 档位问不到就按原时长估
        logger.warning("视频计费秒数查询失败，按原时长估算", exc_info=True)
        return float(seconds)


def _merge_plan(
    storyboard: Storyboard | None,
    provider: VideoProvider,
    *,
    price_per_second: float,
    min_sec: float,
    max_sec: float,
    baseline_usd: float,
) -> dict:
    """合并短镜头能省多少（只算不写；真的合并要用户确认）。"""
    if not storyboard or not storyboard.shots:
        return {}
    buckets = storyboard_service.plan_merge_buckets(
        storyboard, min_sec=min_sec, max_sec=max_sec
    )
    after_seconds = 0.0
    for bucket in buckets:
        after_seconds += min(
            max(float(sum(int(s.duration_sec) for s in bucket)), min_sec),
            max_sec if max_sec > 0 else float("inf"),
        )
    after_usd = round(after_seconds * price_per_second, 6)
    saving = round(max(0.0, baseline_usd - after_usd), 6)
    return {
        "min_sec": round(min_sec, 3),
        "max_sec": round(max_sec, 3),
        "from_shots": len(storyboard.shots),
        "to_shots": len(buckets),
        "after_seconds": round(after_seconds, 3),
        "after_usd": after_usd,
        "saving_usd": saving,
        "worth_it": len(buckets) < len(storyboard.shots) and saving > 0.0001,
    }


def audio_plan(
    storyboard: Storyboard | None,
    audio: AudioManifest | None,
    settings: Settings | None = None,
    *,
    script_mode: str = "",
    tts_provider: str = "",
    native_narration: set[str] | None = None,
    dub_mode: str = "tts",
) -> dict:
    """配音花多少：TTS 按字符（edge-tts 免费）+ 可选的 LLM 文案 token。

    `native_narration` 里的镜头旁白由视频原声直出，不走 TTS，不计字符。
    `dub_mode="native"`：整片用视频原声，配音阶段不调 TTS、不写文案，$0。
    """
    settings = settings or get_settings()
    prices = cost_service.PriceTable.from_settings(settings)
    native_mode = dub_mode == "native"
    shots = [] if native_mode else (storyboard.shots if storyboard else [])
    mode = (
        "off"
        if native_mode
        else audio_script_service.normalize_mode(script_mode or settings.audio_script_mode)
    )

    native_narration = native_narration or set()
    chars = 0
    elapsed = 0.0
    for shot in shots:
        if shot.id in native_narration:
            continue
        text = count_speech_chars(shot.dialogue_text or "") or count_speech_chars(
            shot.narration or ""
        )
        chars += text
        if text <= 0:
            continue
        # 只是给个「大概几秒」的直觉，不参与计费
        elapsed += max(0.6, text / 4.5)

    script_shots, _skipped = audio_script_service.script_targets(
        storyboard, mode, force=settings.audio_llm_force
    ) if storyboard and mode != "off" else ([], 0)
    script_count = sum(len(group) for _, group in script_shots)
    frames = audio_script_service.planned_frames(script_count, settings)
    script_usd = (
        audio_script_service.estimate_voice_script_usd(script_count, frames=frames)
        if mode != "off"
        else 0.0
    )
    tts_usd = prices.tts_usd(chars)
    plan = {
        "action": "audio",
        "tts_provider": tts_provider or settings.tts_provider,
        "tts_price_per_mchar": prices.tts_per_mchar,
        "tts_chars": chars,
        "tts_usd": round(tts_usd, 6),
        "estimated_seconds": round(elapsed, 3),
        "script_mode": mode,
        "script_shots": script_count,
        "script_frames": frames,
        "script_usd": round(script_usd, 6),
        "estimated_usd": round(tts_usd + script_usd, 6),
        "existing_ready": len(audio.ready_clips) if audio else 0,
        "warnings": [],
    }
    if mode != "off":
        plan["warnings"].append(
            f"配音文案（{mode}）：预计 {script_count} 个镜头、抽 {frames} 帧 ≈ "
            f"${script_usd:.4f}（LLM token）"
        )
    if native_narration:
        plan["warnings"].append(
            f"{len(native_narration)} 个镜头的旁白由视频原声直出，不另配音、不计 TTS 费用"
        )
    plan["dub_mode"] = "native" if native_mode else "tts"
    if native_mode:
        plan["warnings"].append("原声模式：对白 / 旁白都用视频原声，不做 TTS 配音，$0")
    elif chars == 0 and not shots:
        plan["warnings"].append("还没有分镜，无法预估配音")
    return plan


def compose_plan(settings: Settings | None = None) -> dict:
    """合成：本地 ffmpeg，不额外花钱，但把口径写清楚（避免用户以为要钱）。"""
    settings = settings or get_settings()
    return {
        "action": "compose",
        "provider": "ffmpeg(local)",
        "subtitle_mode": settings.subtitle_mode,
        "ambient_mode": settings.ambient_mode,
        "estimated_usd": 0.0,
        "unit_kind": "second",
        "warnings": ["合成在本机跑，不产生 API 费用（只花 CPU 时间）"],
    }


async def preflight(
    *,
    project: Project,
    storyboard: Storyboard | None,
    video: VideoManifest | None,
    audio: AudioManifest | None,
    provider: VideoProvider,
    report: CostReport,
    settings: Settings | None = None,
    action: str = "all",
    script_mode: str = "",
    keyframe_estimator: KeyframeEstimator | None = None,
) -> dict:
    """把各阶段的预估拼成一份「动手前的账单」。"""
    settings = settings or get_settings()
    action = (action or "all").strip().lower()
    if action not in ACTIONS:
        action = "all"

    video_part = None
    audio_part = None
    compose_part = None
    if action in {"all", "video"}:
        video_part = await video_plan(
            storyboard, video, provider, settings, keyframe_estimator=keyframe_estimator
        )
    if action in {"all", "audio"}:
        audio_part = audio_plan(
            storyboard,
            audio,
            settings,
            script_mode=script_mode,
            native_narration=video_service.native_narration_shots(storyboard, video)
            if storyboard
            else None,
            dub_mode=video_service.dub_mode_for(project, settings),
        )
    if action in {"all", "compose"}:
        compose_part = compose_plan(settings)

    upcoming = sum(
        part.get("estimated_usd", 0.0)
        for part in (video_part, audio_part, compose_part)
        if part
    )
    budget = budget_projection(report, upcoming)
    return {
        "project_id": project.id,
        "project_status": project.status.value,
        "action": action,
        "video": video_part,
        "audio": audio_part,
        "compose": compose_part,
        "upcoming_usd": round(upcoming, 6),
        "budget": budget,
        "warnings": sorted(
            {
                warning
                for part in (video_part, audio_part, compose_part)
                if part
                for warning in part.get("warnings", [])
            }
        ),
    }
