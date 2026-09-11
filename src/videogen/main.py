from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from videogen import __version__
from videogen.config import get_settings
from videogen.models.project import AspectRatio, InputMode, Project
from videogen.orchestrator.pipeline import Pipeline, PipelineError, PipelineNotFound
from videogen.providers.llm.base import LLMError
from videogen.providers.video.base import VideoProviderError
from videogen.utils.ffmpeg import ffmpeg_available
from videogen.utils.file_store import FileStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("videogen starting (projects=%s)", settings.projects_path)
    # 只提示「有没有配」，绝不打印密钥本身
    logger.info("LLM provider=%s model=%s key_configured=%s",
                settings.llm_provider, settings.openrouter_model, settings.has_llm_key)
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
    try:
        recovered = await pipeline.recover_interrupted_jobs()
        if recovered:
            logger.warning("已恢复 %d 个中断的项目：%s", len(recovered), ", ".join(recovered))
    except Exception:
        logger.exception("恢复中断任务失败")

    yield

    await pipeline.jobs.shutdown()
    logger.info("videogen shutting down")


app = FastAPI(title="videogen", version=__version__, lifespan=lifespan)

store = FileStore()
pipeline = Pipeline(store)

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


# ── Health ──────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.openrouter_model,
        "llm_key_configured": settings.has_llm_key,
        "video_provider": settings.video_provider,
        "ffmpeg_available": ffmpeg_available(),
        "video_concurrency": settings.video_concurrency,
        "auth_enabled": bool(settings.api_token.strip()),
        "stages_implemented": ["bible", "script", "storyboard", "video"],
        "stages_pending": ["audio", "compose"],
        "active_jobs": pipeline.jobs.active_count,
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
async def generate_bible(project_id: str):
    bible = await pipeline.generate_bible(project_id)
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


# ── Script Routes ───────────────────────────────────────────


@app.post("/api/projects/{project_id}/script/generate")
async def generate_script(project_id: str):
    script = await pipeline.generate_script(project_id)
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
async def confirm_script(project_id: str):
    project = await pipeline.confirm_script(project_id)
    return {"status": "ok", "project_status": project.status.value}


@app.post("/api/projects/{project_id}/script/reopen")
async def reopen_script(project_id: str):
    """把已确认的剧本退回，允许重新生成/编辑。"""
    project = await pipeline.reopen_script(project_id)
    return {"status": "ok", "project_status": project.status.value}


# ── Storyboard Routes ───────────────────────────────────────


@app.post("/api/projects/{project_id}/storyboard/generate")
async def generate_storyboard(project_id: str):
    storyboard = await pipeline.generate_storyboard(project_id)
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
async def confirm_storyboard(project_id: str):
    project = await pipeline.confirm_storyboard(project_id)
    return {"status": "ok", "project_status": project.status.value}


@app.post("/api/projects/{project_id}/storyboard/reopen")
async def reopen_storyboard(project_id: str):
    project = await pipeline.reopen_storyboard(project_id)
    return {"status": "ok", "project_status": project.status.value}


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
