from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from videogen import __version__
from videogen.config import get_settings
from videogen.models.bible import Character, StoryBible
from videogen.models.project import AspectRatio, InputMode, Project
from videogen.orchestrator.batch import STOP_AFTER_STAGES, BatchManager
from videogen.orchestrator.pipeline import Pipeline, PipelineError, PipelineNotFound
from videogen.providers import video as video_providers
from videogen.providers.image.base import ImageProviderError
from videogen.providers.llm.base import LLMError
from videogen.providers.tts.base import TTSProviderError
from videogen.providers.video.base import VideoProviderError
from videogen.services import (
    character_image_service,
    subtitle_service,
    voice_catalog,
)
from videogen.services.character_image_service import CharacterImageError
from videogen.services.cost_service import BudgetExceeded, PriceTable
from videogen.utils.ffmpeg import (
    ffmpeg_available,
    ffmpeg_capabilities,
    ffprobe_available,
    resolve_ffmpeg,
    resolve_ffmpeg_for,
)
from videogen.utils.file_store import FileStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("videogen starting (projects=%s)", settings.projects_path)
    # 只提示「有没有配」，绝不打印密钥本身
    logger.info(
        "LLM provider=%s model=%s key_configured=%s",
        settings.llm_provider,
        settings.openrouter_model,
        settings.has_llm_key,
    )
    if not settings.has_llm_key:
        logger.warning("未检测到 LLM API Key，生成类接口会失败（请在 .env 配置）")
    if settings.api_token:
        logger.info("API Token 校验已开启")
    logger.info(
        "视频 provider=%s concurrency=%d ffmpeg=%s",
        settings.video_provider,
        settings.video_concurrency,
        "可用" if ffmpeg_available() else "不可用（将降级为占位文件）",
    )
    logger.info(
        "配音 provider=%s 字幕=%s BGM=%s",
        settings.tts_provider,
        settings.subtitle_mode,
        "开启" if settings.bgm_enabled else "关闭",
    )
    if settings.video_provider == "openrouter":
        # 真实模型按秒计费，启动时把单价和最短时长打出来（绝不打印密钥）
        try:
            provider = video_providers.get_video_provider()
            try:
                price = await provider.price_per_second(resolution=settings.video_resolution)
                caps = await provider.capabilities()
                durations = caps.get("supported_durations") or []
                unit = (
                    f"{price:g}" if price is not None else "未知(.env VIDEO_PRICE_PER_SECOND)"
                )
                budget = (
                    f"${settings.budget_usd:g}" if settings.budget_usd > 0 else "未限制"
                )
                logger.warning(
                    "真实视频模型已启用：model=%s 单价=%s USD/秒 最短=%s秒 预算=%s",
                    provider.model,
                    unit,
                    min(durations) if durations else "?",
                    budget,
                )
            finally:
                await provider.aclose()
        except Exception:
            logger.warning("读取视频模型能力/单价失败（不影响启动）", exc_info=True)
    try:
        recovered = await pipeline.recover_interrupted_jobs()
        if recovered:
            logger.warning("已恢复 %d 个中断的项目：%s", len(recovered), ", ".join(recovered))
    except Exception:
        logger.exception("恢复中断任务失败")

    yield

    await batches.shutdown()
    await pipeline.jobs.shutdown()
    logger.info("videogen shutting down")


app = FastAPI(title="videogen", version=__version__, lifespan=lifespan)

store = FileStore()
pipeline = Pipeline(store)
batches = BatchManager()

if settings.static_path.is_dir():
    app.mount("/static", StaticFiles(directory=settings.static_path), name="static")


# ── 鉴权（可选）────────────────────────────────────────────


@app.middleware("http")
async def api_token_middleware(request: Request, call_next):
    token = settings.api_token.strip()
    path = request.url.path
    if token and path.startswith("/api") and path != "/api/health":
        supplied = request.headers.get("x-api-token") or request.query_params.get("token") or ""
        if not secrets.compare_digest(supplied, token):
            return JSONResponse({"detail": "缺少或错误的 API Token"}, status_code=401)
    return await call_next(request)


# ── 统一错误映射 ───────────────────────────────────────────


@app.exception_handler(PipelineNotFound)
async def _not_found_handler(request: Request, exc: PipelineNotFound):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(PipelineError)
async def _pipeline_error_handler(request: Request, exc: PipelineError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(LLMError)
async def _llm_error_handler(request: Request, exc: LLMError):
    # LLMError 的消息在 provider 层已脱敏，不含密钥
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(VideoProviderError)
async def _video_error_handler(request: Request, exc: VideoProviderError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(BudgetExceeded)
async def _budget_handler(request: Request, exc: BudgetExceeded):
    # 超预算是业务拒绝，不是服务端故障：用 400 让前端直接把原因展示给用户
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(TTSProviderError)
async def _tts_error_handler(request: Request, exc: TTSProviderError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


# ── Request Models ──────────────────────────────────────────


class CreateProjectRequest(BaseModel):
    title: str = "未命名项目"
    input_mode: InputMode = InputMode.THEME
    input_text: str = Field(min_length=1)
    aspect_ratio: AspectRatio = AspectRatio.VERTICAL
    target_duration_sec: int = Field(default=45, ge=5, le=600)


class UpdateProjectRequest(BaseModel):
    title: str | None = None
    input_text: str | None = None
    aspect_ratio: AspectRatio | None = None
    target_duration_sec: int | None = Field(default=None, ge=5, le=600)
    #: 为空表示不改；给空字符串表示恢复默认模型
    llm_model: str | None = None


class GenerateRequest(BaseModel):
    """生成类接口的通用参数：可临时指定 LLM 模型。"""

    model: str | None = None


class ReferenceImageRequest(BaseModel):
    """角色参考图（角色一致性）：base64 上传，避免额外依赖 multipart。"""

    filename: str = "reference.png"
    content_base64: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    """确认剧本/分镜。严格模式拦下时用 `force=true` 强制继续（跳过结构校验）。"""

    force: bool = False


class GenerateReferenceRequest(BaseModel):
    """AI 生成角色参考图（会真的调图片模型，按张计费）。"""

    #: 已经有参考图时是否重新生成（默认跳过，不花钱）
    force: bool = False
    #: 是否把已有参考图作为图生图输入（出「同一角色的另一个角度」）
    use_reference: bool = False
    #: 自定义 prompt（留空 = 用角色设定自动拼）
    prompt: str = ""


class GenerateReferencesRequest(BaseModel):
    """批量生成角色参考图：默认只补没有图的角色。"""

    force: bool = False
    only: list[str] = Field(default_factory=list)
    concurrency: int = Field(default=2, ge=1, le=4)


class BatchItemRequest(BaseModel):
    title: str = ""
    input_text: str = Field(min_length=1)


class BatchRequest(BaseModel):
    items: list[BatchItemRequest] = Field(min_length=1)
    target_duration_sec: int = Field(default=45, ge=5, le=600)
    aspect_ratio: AspectRatio = AspectRatio.VERTICAL
    input_mode: InputMode = InputMode.THEME
    #: 批量跑到哪一步就停：storyboard（只要剧本+分镜）/ video / audio / compose（默认全流程）
    stop_after: str = "compose"
    model: str = ""
    #: 配音文案模式（留空 = 用 .env 的 AUDIO_LLM_SCRIPT）
    audio_script_mode: str = Field(default="", pattern="^(|off|narration|polish|full|perform)$")
    concurrency: int = Field(default=1, ge=1, le=4)


class AudioGenerateRequest(BaseModel):
    """按角色换音色（可只给要改的说话人）+ 可选的配音文案模式。"""

    voices: dict[str, str] = Field(default_factory=dict)
    #: off = 用分镜原文案；narration = 只给没台词的镜头补旁白；
    #: polish = 只改写已有文案；full = 补旁白 + 改写 + 念法建议
    script_mode: str | None = Field(
        default=None, pattern="^(off|narration|polish|full|perform)$"
    )
    #: 是否把视频抽帧发给模型（None = 用配置）
    use_vision: bool | None = None
    #: 对已经生成过同模式文案的镜头再跑一次（None = 用配置，默认跳过不重复花钱）
    force_script: bool | None = None


class ComposeRequest(BaseModel):
    """单次合成的临时覆盖项，不写回全局配置。"""

    subtitle_mode: str | None = Field(default=None, pattern="^(soft|burn|none)$")
    bgm_id: str | None = None
    bgm_gain_db: float | None = Field(default=None, ge=-60, le=0)
    crf: int | None = Field(default=None, ge=0, le=51)
    subtitle_font_scale: float | None = Field(default=None, gt=0, le=0.2)
    #: 视频模型自带音轨：off = 丢弃；under = 压低垫在人声下面
    ambient_mode: str | None = Field(default=None, pattern="^(off|under)$")
    ambient_gain_db: float | None = Field(default=None, ge=-60, le=0)


def _model_of(project_id: str, req: GenerateRequest | None) -> str:
    """请求里显式给的模型优先，否则用项目上保存的模型（空 = 用 .env 默认）。"""
    if req and req.model is not None:
        return req.model.strip()
    try:
        return pipeline.get_project(project_id).llm_model
    except PipelineError:
        return ""


# ── 模型 ────────────────────────────────────────────────────


@app.get("/api/models")
async def list_models():
    """可选模型 + 单价 + 可选音色（前端下拉用）。"""
    prices = PriceTable.from_settings()
    return {
        "default": settings.openrouter_model,
        "candidates": settings.candidate_models,
        "prices": prices.model_dump(),
        "budget_usd": settings.budget_usd,
        "batch_max_items": settings.batch_max_items,
        "seed": settings.video_seed,
        "voices": voice_catalog.catalog_rows(),
        # 云端 TTS 有自己的音色表（名字与 Edge 完全不同，不能混用）
        "voices_cloud": voice_catalog.catalog_rows(cloud=True),
        "tts_provider": settings.tts_provider,
        "tts_cloud": voice_catalog.is_cloud_target(settings.tts_provider),
        "tts_model": settings.openrouter_tts_model,
        "voice_defaults": {
            "female": settings.tts_voice_female,
            "male": settings.tts_voice_male,
            "narrator": settings.tts_voice_narrator,
        },
        "voice_allow_accent": settings.tts_allow_accent_voices,
        "story_chapters": settings.story_chapters,
    }


@app.get("/api/projects/{project_id}/voices")
async def project_voices(project_id: str):
    """这个项目的选角：每个说话人用什么音色、为什么、有没有撞音色。

    **不调 TTS、不花钱** —— 只跑一遍确定性选角，给 UI 显示与人工调整用。
    """
    project = pipeline.get_project(project_id)
    storyboard = store.load_storyboard(project_id)
    bible = store.load_bible(project_id)
    if storyboard is None:
        raise HTTPException(400, "分镜不存在，先生成分镜再选角")
    manifest = store.load_audio_manifest(project_id)
    cloud = voice_catalog.is_cloud_target(settings.tts_provider)
    casting = voice_catalog.cast_voice_map(
        storyboard,
        bible,
        existing=(manifest.voice_map if manifest else None),
        target=settings.tts_provider,
    )
    rows = {row["id"]: row["label"] for row in voice_catalog.catalog_rows(cloud=cloud)}
    names = {c.id: c.name for c in (bible.characters if bible else [])}
    return {
        "voice_map": casting.voice_map,
        "delivery": {sp: d.model_dump() for sp, d in casting.delivery.items()},
        "genders": casting.genders,
        "notes": casting.notes,
        "characters": [
            {
                "id": speaker,
                "name": names.get(speaker, "旁白" if speaker == "narrator" else speaker),
                "voice": voice,
                "voice_label": rows.get(voice, voice),
                "gender": casting.genders.get(speaker, ""),
                "delivery": (
                    casting.delivery[speaker].model_dump()
                    if speaker in casting.delivery
                    else None
                ),
            }
            for speaker, voice in casting.voice_map.items()
        ],
        "project_status": project.status.value,
        "tts_provider": settings.tts_provider,
        "cloud": cloud,
    }


# ── Health ──────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    caps = await ffmpeg_capabilities()
    burn_exe, burn_caps = await resolve_ffmpeg_for({"subtitles"})
    can_burn = "subtitles" in burn_caps
    return {
        "status": "ok",
        "version": __version__,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.openrouter_model,
        "llm_key_configured": settings.has_llm_key,
        "video_provider": settings.video_provider,
        "video_model": (
            settings.openrouter_video_model if settings.video_provider == "openrouter" else "mock"
        ),
        "image_provider": settings.image_provider,
        "image_model": settings.openrouter_image_model,
        "image_price_usd": settings.image_price_usd,
        "ffmpeg_available": ffmpeg_available(),
        "video_concurrency": settings.video_concurrency,
        "auth_enabled": bool(settings.api_token.strip()),
        "tts_provider": settings.tts_provider,
        "tts_voice": settings.tts_voice,
        "audio_script_mode": settings.audio_script_mode,
        "audio_script_vision": settings.audio_llm_vision,
        "audio_script_model": settings.audio_llm_model or settings.openrouter_model,
        "bgm_enabled": settings.bgm_enabled,
        "bgm_dir": str(settings.bgm_path),
        "subtitle_mode": settings.subtitle_mode,
        "ffprobe_available": ffprobe_available(),
        # 只暴露可执行文件路径与能力，不涉及任何密钥
        "ffmpeg_path": resolve_ffmpeg(),
        "ffmpeg_can_burn_subtitles": can_burn,
        "ffmpeg_burn_path": burn_exe if can_burn else "",
        "ffmpeg_can_embed_subtitles": "mov_text" in caps,
        "ffmpeg_auto_upgrade": settings.ffmpeg_auto_upgrade,
        "stages_implemented": ["bible", "script", "storyboard", "video", "audio", "compose"],
        "stages_pending": [],
        "active_jobs": pipeline.jobs.active_count,
        "active_batches": batches.active_count,
        "budget_usd": settings.budget_usd,
        "llm_model_candidates": settings.candidate_models,
    }


# ── Project Routes ──────────────────────────────────────────


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest):
    project = Project(
        title=req.title.strip() or "未命名项目",
        input_mode=req.input_mode,
        input_text=req.input_text.strip(),
        aspect_ratio=req.aspect_ratio,
        target_duration_sec=req.target_duration_sec,
    )
    store.save_project(project)
    return project.model_dump()


@app.get("/api/projects")
async def list_projects():
    return [p.model_dump() for p in store.list_projects()]


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    return pipeline.get_project(project_id).model_dump()


@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, req: UpdateProjectRequest):
    project = await pipeline.update_project(project_id, req.model_dump(exclude_none=True))
    return project.model_dump()


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    if not await pipeline.delete_project(project_id):
        raise HTTPException(404, "项目不存在")
    return {"status": "ok"}


# ── Bible Routes ────────────────────────────────────────────


@app.post("/api/projects/{project_id}/bible/generate")
async def generate_bible(project_id: str, req: GenerateRequest | None = None):
    bible = await pipeline.generate_bible(project_id, model=_model_of(project_id, req))
    return bible.model_dump()


@app.get("/api/projects/{project_id}/bible")
async def get_bible(project_id: str):
    bible = pipeline.get_bible(project_id)
    if not bible:
        raise HTTPException(404, "故事圣经不存在")
    return bible.model_dump()


@app.put("/api/projects/{project_id}/bible")
async def update_bible(project_id: str, data: dict):
    """全量更新故事圣经（仅允许在剧本确认前）。"""
    bible = await pipeline.update_bible(project_id, data)
    return bible.model_dump()


# ── 长篇剧集大纲 Routes ─────────────────────────────────────


class StoryOutlineRequest(BaseModel):
    """生成长篇大纲（20+ 章骨架 + 逐章正文）。"""

    #: 章数（留空 = 用配置 STORY_CHAPTERS）
    chapters: int | None = Field(default=None, gt=0, le=200)
    #: 重出骨架（会丢弃标题对不上的旧正文）
    force: bool = False
    model: str = ""


@app.post("/api/projects/{project_id}/story-outline/generate")
async def generate_story_outline(project_id: str, req: StoryOutlineRequest | None = None):
    """生成长篇剧集大纲。只调 LLM（很便宜），不烧视频额度。"""
    body = req or StoryOutlineRequest()
    outline = await pipeline.generate_story_outline(
        project_id,
        chapters=body.chapters,
        force=body.force,
        model=_model_of(project_id, GenerateRequest(model=body.model)),
    )
    return outline.model_dump()


@app.get("/api/projects/{project_id}/story-outline")
async def get_story_outline(project_id: str):
    outline = pipeline.get_story_outline(project_id)
    if not outline:
        raise HTTPException(404, "长篇大纲不存在")
    return outline.model_dump()


# ── Script Routes ───────────────────────────────────────────


class ScriptGenerateRequest(GenerateRequest):
    """生成剧本：可选「只展开这几章」。"""

    #: 要展开的章 id（留空 = 全部章）。已经展开过的章不会重复生成
    chapters: list[str] | None = None
    #: 清空重来（会作废下游分镜与已生成的媒体）
    force: bool = False


@app.post("/api/projects/{project_id}/script/generate")
async def generate_script(project_id: str, req: ScriptGenerateRequest | None = None):
    body = req or ScriptGenerateRequest()
    script = await pipeline.generate_script(
        project_id,
        model=_model_of(project_id, body),
        chapters=body.chapters,
        force=body.force,
    )
    return script.model_dump()


@app.get("/api/projects/{project_id}/script")
async def get_script(project_id: str):
    script = pipeline.get_script(project_id)
    if not script:
        raise HTTPException(404, "剧本不存在")
    return script.model_dump()


@app.put("/api/projects/{project_id}/script")
async def update_script(project_id: str, data: dict):
    """全量更新剧本（仅允许在确认之前）。"""
    script = await pipeline.update_script(project_id, data)
    return script.model_dump()


@app.post("/api/projects/{project_id}/script/confirm")
async def confirm_script(project_id: str, req: ConfirmRequest | None = None):
    """确认剧本。严格模式下有结构 error 会被拦（body `{"force": true}` 可强制）。"""
    project = await pipeline.confirm_script(project_id, force=bool(req and req.force))
    return {"status": "ok", "project_status": project.status.value}


@app.post("/api/projects/{project_id}/script/reopen")
async def reopen_script(project_id: str):
    """把已确认的剧本退回，允许重新生成/编辑。"""
    project = await pipeline.reopen_script(project_id)
    return {"status": "ok", "project_status": project.status.value}


# ── Storyboard Routes ───────────────────────────────────────


@app.post("/api/projects/{project_id}/storyboard/generate")
async def generate_storyboard(project_id: str, req: GenerateRequest | None = None):
    storyboard = await pipeline.generate_storyboard(project_id, model=_model_of(project_id, req))
    return storyboard.model_dump()


@app.get("/api/projects/{project_id}/storyboard")
async def get_storyboard(project_id: str):
    storyboard = pipeline.get_storyboard(project_id)
    if not storyboard:
        raise HTTPException(404, "分镜不存在")
    return storyboard.model_dump()


@app.put("/api/projects/{project_id}/storyboard")
async def update_storyboard(project_id: str, data: dict):
    """全量更新分镜（仅允许在确认之前）。"""
    storyboard = await pipeline.update_storyboard(project_id, data)
    return storyboard.model_dump()


@app.post("/api/projects/{project_id}/storyboard/confirm")
async def confirm_storyboard(project_id: str, req: ConfirmRequest | None = None):
    """确认分镜。严格模式下有结构 error 会被拦（body `{"force": true}` 可强制）。"""
    project = await pipeline.confirm_storyboard(project_id, force=bool(req and req.force))
    return {"status": "ok", "project_status": project.status.value}


@app.post("/api/projects/{project_id}/audio/reopen")
async def reopen_audio(project_id: str):
    """退回配音阶段：换音色/念法后重做配音（成片会作废）。"""
    project = await pipeline.reopen_audio(project_id)
    return {"status": project.status.value}


@app.post("/api/projects/{project_id}/storyboard/reopen")
async def reopen_storyboard(project_id: str):
    project = await pipeline.reopen_storyboard(project_id)
    return {"status": "ok", "project_status": project.status.value}


class MergeShotsRequest(BaseModel):
    """合并短镜头（省钱）：默认只预览，`dry_run=false` 才真的写盘。

    真实视频模型按最短档位计费（H3 最短 5 秒），3 秒的镜头也是 5 秒的钱 ——
    把几个短镜头合成一个长镜头能直接省钱，代价是镜头语言变粗。
    """

    #: 目标最短时长（秒）；留空 = 用模型最短档位（H3 是 5）
    min_sec: float | None = Field(default=None, gt=0, le=60)
    #: 单个镜头时长上限（0 = 不限制）
    max_sec: float = Field(default=0.0, ge=0, le=60)
    dry_run: bool = True
    #: 已有可用片段时是否强制合并（会让已付费片段作废）
    force: bool = False


@app.post("/api/projects/{project_id}/storyboard/merge-shots")
async def merge_shots(project_id: str, req: MergeShotsRequest | None = None):
    """把太短的镜头合并成更少更长的片段（默认只算不写）。"""
    body = req or MergeShotsRequest()
    return await pipeline.merge_short_shots(
        project_id,
        min_sec=body.min_sec,
        max_sec=body.max_sec,
        dry_run=body.dry_run,
        force=body.force,
    )


@app.get("/api/projects/{project_id}/preflight")
async def preflight(project_id: str, action: str = "all", script_mode: str = ""):
    """动手花钱前先看账：镜头数 / 计费秒数 / 单价 / 预估 USD / 预算余额 / 省钱建议。"""
    return await pipeline.preflight(project_id, action=action, script_mode=script_mode)


# ── World State / Outline / Consistency ─────────────────────


@app.get("/api/projects/{project_id}/world-state")
async def get_world_state(project_id: str):
    world_state = pipeline.get_world_state(project_id)
    if not world_state:
        raise HTTPException(404, "世界状态不存在")
    return world_state.model_dump()


@app.get("/api/projects/{project_id}/outline")
async def get_outline(project_id: str):
    outline = pipeline.get_outline(project_id)
    if not outline:
        raise HTTPException(404, "大纲不存在")
    return outline.model_dump()


@app.get("/api/projects/{project_id}/consistency")
async def get_consistency(project_id: str):
    report = pipeline.get_report(project_id)
    if not report:
        raise HTTPException(404, "校验报告不存在")
    return report.model_dump()


@app.post("/api/projects/{project_id}/consistency/revalidate")
async def revalidate(project_id: str):
    report = await pipeline.revalidate(project_id)
    if not report:
        raise HTTPException(404, "故事圣经不存在，无法校验")
    return report.model_dump()


# ── Video / Jobs Routes ─────────────────────────────────────


@app.post("/api/projects/{project_id}/video/generate", status_code=202)
async def generate_video(project_id: str):
    """提交视频生成后台任务（异步，用 video/status 查进度）。"""
    job = await pipeline.start_video_generation(project_id)
    return {"job": job.to_public()}


@app.get("/api/projects/{project_id}/video/status")
async def video_status(project_id: str):
    return pipeline.video_status(project_id)


@app.get("/api/projects/{project_id}/video/manifest")
async def video_manifest(project_id: str):
    manifest = pipeline.get_video_manifest(project_id)
    if not manifest:
        raise HTTPException(404, "视频清单不存在")
    return manifest.model_dump()


@app.post("/api/projects/{project_id}/video/cancel")
async def cancel_video(project_id: str):
    cancelled = await pipeline.cancel_video_generation(project_id)
    return {"cancelled": cancelled}


@app.get("/api/projects/{project_id}/clips/{shot_id}")
async def get_clip(project_id: str, shot_id: str):
    """读取某个镜头的视频片段（路径经过越界校验）。"""
    manifest = pipeline.get_video_manifest(project_id)
    if not manifest:
        raise HTTPException(404, "视频清单不存在")
    clip = manifest.get(shot_id)
    if not clip or not clip.path:
        raise HTTPException(404, "该镜头还没有视频文件")
    path = store.resolve_artifact(project_id, clip.path)
    if not path:
        raise HTTPException(404, "视频文件不存在（可能已被清理）")
    media_type = "video/mp4" if path.suffix == ".mp4" else "application/json"
    return FileResponse(path, media_type=media_type, filename=path.name)


# ── Audio（配音）Routes ─────────────────────────────────────


@app.post("/api/projects/{project_id}/audio/generate", status_code=202)
async def generate_audio(project_id: str, req: AudioGenerateRequest | None = None):
    """提交配音后台任务（异步，用 audio/status 查进度）。

    `script_mode` 非 off 时会先让 LLM 看着画面写/改配音文案（额外花 LLM 额度）。
    """
    voices = (req.voices if req else {}) or {}
    job = await pipeline.start_audio_generation(
        project_id,
        voice_overrides=voices,
        script_mode=req.script_mode if req else None,
        use_vision=req.use_vision if req else None,
        force_script=req.force_script if req else None,
    )
    return {"job": job.to_public()}


@app.get("/api/projects/{project_id}/audio/status")
async def audio_status(project_id: str):
    return pipeline.audio_status(project_id)


@app.get("/api/projects/{project_id}/audio/manifest")
async def audio_manifest(project_id: str):
    manifest = pipeline.get_audio_manifest(project_id)
    if not manifest:
        raise HTTPException(404, "配音清单不存在")
    return manifest.model_dump()


@app.post("/api/projects/{project_id}/audio/cancel")
async def cancel_audio(project_id: str):
    cancelled = await pipeline.cancel_audio_generation(project_id)
    return {"cancelled": cancelled}


@app.get("/api/projects/{project_id}/audio/{clip_id}")
async def get_audio_clip(project_id: str, clip_id: str):
    """试听某条配音（路径经过越界校验）。"""
    manifest = pipeline.get_audio_manifest(project_id)
    if not manifest:
        raise HTTPException(404, "配音清单不存在")
    clip = manifest.get(clip_id)
    if not clip or not clip.path:
        raise HTTPException(404, "该条目还没有音频文件")
    path = store.resolve_artifact(project_id, clip.path)
    if not path:
        raise HTTPException(404, "音频文件不存在（可能已被清理）")
    media_type = "audio/mpeg" if path.suffix == ".mp3" else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


# ── Compose（成片）Routes ───────────────────────────────────


@app.post("/api/projects/{project_id}/compose", status_code=202)
async def compose_project(project_id: str, req: ComposeRequest | None = None):
    """提交成片合成任务（异步，用 compose/status 查进度）。"""
    overrides: dict = {}
    if req:
        if req.subtitle_mode is not None:
            overrides["compose_subtitle_mode"] = req.subtitle_mode
        if req.bgm_id is not None:
            overrides["bgm_id"] = req.bgm_id
        if req.bgm_gain_db is not None:
            overrides["bgm_gain_db"] = req.bgm_gain_db
        if req.crf is not None:
            overrides["compose_crf"] = req.crf
        if req.subtitle_font_scale is not None:
            overrides["compose_subtitle_font_scale"] = req.subtitle_font_scale
        if req.ambient_mode is not None:
            overrides["compose_ambient_mode"] = req.ambient_mode
        if req.ambient_gain_db is not None:
            overrides["compose_ambient_gain_db"] = req.ambient_gain_db
    job = await pipeline.start_compose(project_id, overrides=overrides)
    return {"job": job.to_public()}


@app.get("/api/projects/{project_id}/compose/status")
async def compose_status(project_id: str):
    return pipeline.compose_status(project_id)


@app.post("/api/projects/{project_id}/compose/cancel")
async def cancel_compose(project_id: str):
    cancelled = await pipeline.cancel_compose(project_id)
    return {"cancelled": cancelled}


@app.get("/api/projects/{project_id}/final")
async def get_final(project_id: str):
    """下载成片。"""
    report = pipeline.get_compose_report(project_id)
    if not report or not report.output_path:
        raise HTTPException(404, "成片不存在，请先合成")
    path = store.resolve_artifact(project_id, report.output_path)
    if not path:
        raise HTTPException(404, "成片文件不存在（可能已被清理）")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/projects/{project_id}/subtitles")
async def get_subtitles(project_id: str):
    """下载字幕侧挂文件（SRT）。"""
    report = pipeline.get_compose_report(project_id)
    if not report or not report.subtitles_path:
        raise HTTPException(404, "字幕文件不存在")
    path = store.resolve_artifact(project_id, report.subtitles_path)
    if not path:
        raise HTTPException(404, "字幕文件不存在（可能已被清理）")
    return FileResponse(path, media_type="application/x-subrip", filename=path.name)


@app.get("/api/projects/{project_id}/subtitles.vtt")
async def get_subtitles_vtt(project_id: str):
    """字幕的 WebVTT 版本，给浏览器 `<video><track>` 预览用。

    浏览器**不认** SRT，也**不渲染** mp4 里的 mov_text 软字幕轨 —— 所以想在页面上
    看见字幕，必须有这份 VTT。已经合成过的老项目没有 `subtitles.vtt` 文件，
    这里就地把 srt 转出来，不需要重新合成。
    """

    report = pipeline.get_compose_report(project_id)
    if not report or not report.subtitles_path:
        raise HTTPException(404, "字幕文件不存在")
    if report.subtitles_vtt_path:
        cached = store.resolve_artifact(project_id, report.subtitles_vtt_path)
        if cached and cached.is_file():
            body = cached.read_text(encoding="utf-8")
            return Response(content=body, media_type="text/vtt; charset=utf-8")
    path = store.resolve_artifact(project_id, report.subtitles_path)
    if not path or not path.is_file():
        raise HTTPException(404, "字幕文件不存在（可能已被清理）")
    body = subtitle_service.srt_to_vtt(path.read_text(encoding="utf-8"))
    return Response(content=body, media_type="text/vtt; charset=utf-8")


# ── 角色一致性（参考图）────────────────────────────────────


def _load_character(project_id: str, char_id: str) -> tuple[StoryBible, Character]:
    bible = pipeline.get_bible(project_id)
    if not bible:
        raise HTTPException(404, "故事圣经不存在")
    character = bible.get_character(char_id)
    if character is None:
        raise HTTPException(404, f"角色不存在: {char_id}")
    return bible, character


@app.post("/api/projects/{project_id}/characters/{char_id}/reference")
async def upload_reference(project_id: str, char_id: str, req: ReferenceImageRequest):
    """上传角色参考图（base64）。真实视频 provider 会用它保持角色外观一致。"""
    import base64
    import binascii

    _load_character(project_id, char_id)  # 项目/角色不存在就直接 404

    suffix = Path(req.filename).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "只支持 png / jpg / jpeg / webp")
    try:
        raw = base64.b64decode(req.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, f"base64 解码失败：{exc}") from exc

    limit = int(settings.reference_image_max_mb * 1024 * 1024)
    if len(raw) > limit:
        raise HTTPException(400, f"参考图太大（{len(raw)} 字节 > {limit} 字节）")
    if not raw:
        raise HTTPException(400, "参考图内容为空")

    target = store.refs_dir(project_id) / f"{char_id}{suffix}"
    for stale in store.refs_dir(project_id).glob(f"{char_id}.*"):
        if stale != target:
            stale.unlink(missing_ok=True)
    target.write_bytes(raw)

    bible = pipeline.get_bible(project_id)
    assert bible is not None  # _load_character 已经确认过
    for index, item in enumerate(bible.characters):
        if item.id == char_id:
            bible.characters[index] = item.model_copy(
                update={"reference_image": f"refs/{target.name}"}
            )
            break
    store.save_bible(project_id, bible)
    return {
        "status": "ok",
        "character_id": char_id,
        "path": f"refs/{target.name}",
        "bytes": len(raw),
    }


@app.get("/api/projects/{project_id}/characters/{char_id}/reference")
async def get_reference(project_id: str, char_id: str):
    character = _load_character(project_id, char_id)[1]
    if not character.reference_image:
        raise HTTPException(404, "该角色还没有参考图")
    path = store.resolve_artifact(project_id, character.reference_image)
    if not path:
        raise HTTPException(404, "参考图文件不存在")
    return FileResponse(path)


@app.delete("/api/projects/{project_id}/characters/{char_id}/reference")
async def delete_reference(project_id: str, char_id: str):
    bible, character = _load_character(project_id, char_id)
    for index, item in enumerate(bible.characters):
        if item.id == char_id:
            bible.characters[index] = item.model_copy(update={"reference_image": ""})
            break
    store.save_bible(project_id, bible)
    for stale in store.refs_dir(project_id).glob(f"{char_id}.*"):
        stale.unlink(missing_ok=True)
    return {"status": "ok", "removed": bool(character.reference_image)}


@app.get("/api/projects/{project_id}/characters/references/plan")
async def references_plan(project_id: str):
    """哪些角色还没有参考图 + 补齐一遍的预估花费（**不调 API、不花钱**）。"""
    pipeline.get_project(project_id)
    return character_image_service.reference_plan(store, project_id)


@app.post("/api/projects/{project_id}/characters/references/generate")
async def generate_references(project_id: str, req: GenerateReferencesRequest):
    """批量 AI 生成角色参考图（默认只补缺失的；单角色失败不影响其它）。"""
    pipeline.get_project(project_id)
    try:
        return await character_image_service.generate_all_references(
            store,
            project_id,
            force=req.force,
            only=req.only or None,
            concurrency=req.concurrency,
        )
    except BudgetExceeded as exc:
        raise HTTPException(402, str(exc)) from exc
    except (CharacterImageError, ImageProviderError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/projects/{project_id}/characters/{char_id}/reference/generate")
async def generate_reference(project_id: str, char_id: str, req: GenerateReferenceRequest):
    """给一个角色 AI 生成参考图（定妆照）；已有图默认跳过，force=true 才重画。"""
    _load_character(project_id, char_id)
    try:
        result = await character_image_service.generate_character_image(
            store,
            project_id,
            char_id,
            force=req.force,
            use_reference=req.use_reference,
            prompt_override=req.prompt,
        )
    except BudgetExceeded as exc:
        raise HTTPException(402, str(exc)) from exc
    except (CharacterImageError, ImageProviderError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok", **result}


# ── 成本 ────────────────────────────────────────────────────


@app.get("/api/projects/{project_id}/cost")
async def project_cost(project_id: str):
    """成本账本：按阶段的花费 / token / 秒数 / 字符 + 预算与预估。

    额外给出视频的「片段实测花费 vs 账本」：`video_unrecorded_usd > 0` 说明有漏记，
    调 `cost/reconcile` 可以按实测补齐（不调 API、不动媒体）。
    """
    return pipeline.cost_status(project_id)


@app.post("/api/projects/{project_id}/cost/reconcile")
async def reconcile_cost(project_id: str):
    """把视频片段上实测但没记账的花费补进账本（幂等，只补差额）。"""
    return await pipeline.reconcile_video_cost(project_id)


# ── 批量生产 ────────────────────────────────────────────────


@app.post("/api/batches", status_code=202)
async def create_batch(req: BatchRequest):
    """一次提交多个主题，自动跑完整条链路（跳过人工确认门控）。"""
    try:
        batch = batches.submit(
            pipeline,
            [item.model_dump() for item in req.items],
            target_duration_sec=req.target_duration_sec,
            aspect_ratio=req.aspect_ratio,
            input_mode=req.input_mode,
            stop_after=req.stop_after,
            model=req.model,
            audio_script_mode=req.audio_script_mode,
            concurrency=req.concurrency,
            max_items=settings.batch_max_items,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return batch.to_public()


@app.get("/api/batches")
async def list_batches(limit: int = 20):
    return [batch.to_public() for batch in batches.list(limit=limit)]


@app.get("/api/batches/stages")
async def batch_stages():
    return {"stop_after": list(STOP_AFTER_STAGES), "max_items": settings.batch_max_items}


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str):
    batch = batches.get(batch_id)
    if not batch:
        raise HTTPException(404, "批量任务不存在")
    return batch.to_public()


@app.post("/api/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    return {"cancelled": await batches.cancel(batch_id)}


@app.get("/api/projects/{project_id}/jobs")
async def list_project_jobs(project_id: str, limit: int = 20):
    pipeline.get_project(project_id)
    return [job.to_public() for job in pipeline.jobs.list(project_id, limit=limit)]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = pipeline.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job.to_public()


# ── Static UI ───────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = settings.static_path / "index.html"
    if html_path.exists():
        return HTMLResponse(
            content=html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return HTMLResponse(
        "<h1>videogen</h1><p>UI 未就绪（static/index.html 缺失），请访问 "
        "<a href='/docs'>/docs</a></p>"
    )
