from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from videogen.config import get_settings

logger = logging.getLogger(__name__)

__all__ = ["FFmpegError", "ffmpeg_available", "resolve_ffmpeg", "run_ffmpeg"]


class FFmpegError(RuntimeError):
    """ffmpeg 不可用或执行失败（消息已脱敏）。"""


_resolved: str | None = None
_resolved_once = False


def resolve_ffmpeg() -> str | None:
    """按优先级找 ffmpeg 可执行文件。

    1. `.env` 里的 `FFMPEG_PATH`（若显式配置且存在）
    2. `PATH` 里的 ffmpeg
    3. `imageio-ffmpeg` 附带的便携版（`pip install imageio-ffmpeg`）

    找不到返回 None —— 调用方必须显式处理，不允许假装成功。
    """
    global _resolved, _resolved_once
    if _resolved_once:
        return _resolved

    _resolved_once = True
    configured = get_settings().ffmpeg_path

    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            _resolved = str(candidate)
            return _resolved
        found = shutil.which(configured)
        if found:
            _resolved = found
            return _resolved

    found = shutil.which("ffmpeg")
    if found:
        _resolved = found
        return _resolved

    try:  # 可选依赖：pip install imageio-ffmpeg
        import imageio_ffmpeg  # type: ignore[import-not-found]

        _resolved = imageio_ffmpeg.get_ffmpeg_exe()
        logger.info("使用 imageio-ffmpeg 自带的 ffmpeg")
    except Exception:  # noqa: BLE001 - 没有就是没有，交给调用方报错
        _resolved = None

    if not _resolved:
        logger.warning(
            "未找到 ffmpeg：视频生成会不可用。可执行 `brew install ffmpeg` 或 "
            "`pip install imageio-ffmpeg`，也可在 .env 设置 FFMPEG_PATH"
        )
    return _resolved


def reset_ffmpeg_cache() -> None:
    """测试用：清掉解析缓存。"""
    global _resolved, _resolved_once
    _resolved = None
    _resolved_once = False


def ffmpeg_available() -> bool:
    return resolve_ffmpeg() is not None


async def run_ffmpeg(args: list[str], *, timeout_sec: float = 120.0) -> None:
    """异步执行 ffmpeg，失败时抛出带 stderr 摘要的 FFmpegError。"""
    exe = resolve_ffmpeg()
    if not exe:
        raise FFmpegError(
            "未找到 ffmpeg，请安装（brew install ffmpeg）或在 .env 设置 FFMPEG_PATH，"
            "或 pip install imageio-ffmpeg"
        )

    process = await asyncio.create_subprocess_exec(
        exe,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise FFmpegError(f"ffmpeg 执行超时（{timeout_sec:.0f}s）") from exc
    except asyncio.CancelledError:
        # 任务被取消时不能留下后台 ffmpeg 进程和半截文件
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise FFmpegError(f"ffmpeg 退出码 {process.returncode}：{' | '.join(tail)[:300]}")
