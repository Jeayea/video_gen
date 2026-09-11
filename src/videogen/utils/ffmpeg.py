from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from videogen.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "FFmpegError",
    "describe_media",
    "ffmpeg_available",
    "ffmpeg_capabilities",
    "ffprobe_available",
    "probe_duration",
    "reset_ffmpeg_cache",
    "resolve_ffmpeg",
    "resolve_ffprobe",
    "run_ffmpeg",
    "run_ffmpeg_capture",
]


class FFmpegError(RuntimeError):
    """ffmpeg 不可用或执行失败（消息已脱敏）。"""


_resolved: str | None = None
_resolved_once = False
_probe_resolved: str | None = None
_probe_once = False
_caps: set[str] | None = None


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


def resolve_ffprobe() -> str | None:
    """找 ffprobe（**可选**）：探测真实媒体时长用。

    注意 `imageio-ffmpeg` 只带 ffmpeg 不带 ffprobe，所以这里找不到是常态，
    调用方必须能接受 None（用 provider 返回值 / 估算代替）。
    """
    global _probe_resolved, _probe_once
    if _probe_once:
        return _probe_resolved
    _probe_once = True

    configured = (get_settings().ffprobe_path or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            _probe_resolved = str(candidate)
            return _probe_resolved
        found = shutil.which(configured)
        if found:
            _probe_resolved = found
            return _probe_resolved

    # ffmpeg 旁边通常就有 ffprobe（brew / 官方静态包都是这样）
    exe = resolve_ffmpeg()
    if exe:
        sibling = Path(exe).with_name("ffprobe")
        if sibling.is_file():
            _probe_resolved = str(sibling)
            return _probe_resolved

    _probe_resolved = shutil.which("ffprobe")
    return _probe_resolved


def reset_ffmpeg_cache() -> None:
    """测试用：清掉所有解析/能力探测缓存。"""
    global _resolved, _resolved_once, _probe_resolved, _probe_once, _caps
    _resolved = None
    _resolved_once = False
    _probe_resolved = None
    _probe_once = False
    _caps = None


def ffmpeg_available() -> bool:
    return resolve_ffmpeg() is not None


def ffprobe_available() -> bool:
    return resolve_ffprobe() is not None


async def _capture(
    exe: str, args: list[str], *, timeout_sec: float, cwd: Path | None = None
) -> tuple[int, str]:
    """跑一个短命令并返回 (returncode, 合并输出)。"""
    process = await asyncio.create_subprocess_exec(
        exe,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 1, ""
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, (stdout or b"").decode("utf-8", "replace")


async def ffmpeg_capabilities() -> set[str]:
    """探测 ffmpeg 支持的滤镜/编码器（结果缓存）。

    只关心合成链路真正会用到的几项：没有 libass 就烧不了字幕，
    没有 mov_text 就内嵌不了软字幕 —— 必须能降级而不是直接崩。
    """
    global _caps
    if _caps is not None:
        return _caps

    caps: set[str] = set()
    exe = resolve_ffmpeg()
    if not exe:
        _caps = caps
        return caps

    try:
        code, out = await _capture(exe, ["-hide_banner", "-filters"], timeout_sec=20.0)
        if code == 0:
            for token in _tokens(out):
                caps.add(token)
        code, out = await _capture(exe, ["-hide_banner", "-encoders"], timeout_sec=20.0)
        if code == 0:
            for token in _tokens(out):
                caps.add(token)
    except Exception:
        logger.warning("ffmpeg 能力探测失败，按最小能力处理", exc_info=True)

    _caps = caps
    return caps


def _tokens(output: str) -> list[str]:
    names: list[str] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("Filters:"):
            names.append(parts[1])
    return names


async def run_ffmpeg_capture(
    args: list[str], *, timeout_sec: float = 120.0, cwd: Path | None = None
) -> str:
    """异步执行 ffmpeg 并返回输出摘要（用于需要读输出的场景）。"""
    exe = resolve_ffmpeg()
    if not exe:
        raise FFmpegError(_NO_FFMPEG)
    code, out = await _capture(exe, args, timeout_sec=timeout_sec, cwd=cwd)
    if code != 0:
        raise FFmpegError(f"ffmpeg 退出码 {code}：{_tail(out)}")
    return out


async def describe_media(path: Path, *, timeout_sec: float = 30.0) -> str:
    """返回 `ffmpeg -i` 的探测输出（时长/流信息），用于诊断与验证。

    注意：只给输入不给输出时 ffmpeg 一定返回非 0，这里刻意不看返回码。
    """
    exe = resolve_ffmpeg()
    if not exe or not Path(path).is_file():
        return ""
    try:
        _, out = await _capture(exe, ["-hide_banner", "-i", str(path)], timeout_sec=timeout_sec)
    except Exception:  # noqa: BLE001 - 诊断信息拿不到就算了
        return ""
    return out


async def probe_duration(path: Path, *, timeout_sec: float = 30.0) -> float | None:
    """用 ffprobe 读媒体时长；没有 ffprobe 或读取失败时返回 None。"""
    exe = resolve_ffprobe()
    if not exe or not Path(path).is_file():
        return None
    try:
        code, out = await _capture(
            exe,
            [
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            timeout_sec=timeout_sec,
        )
    except Exception:
        logger.debug("ffprobe 读取时长失败: %s", path, exc_info=True)
        return None
    if code != 0:
        return None
    try:
        value = float(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


_NO_FFMPEG = (
    "未找到 ffmpeg，请安装（brew install ffmpeg）或在 .env 设置 FFMPEG_PATH，"
    "或 pip install imageio-ffmpeg"
)


async def run_ffmpeg(
    args: list[str], *, timeout_sec: float = 120.0, cwd: Path | None = None
) -> None:
    """异步执行 ffmpeg，失败时抛出带 stderr 摘要的 FFmpegError。

    `cwd` 用于让滤镜参数里的文件名保持相对路径（`subtitles=` 的转义很坑，
    切到产物目录里只写文件名最稳）。
    """
    exe = resolve_ffmpeg()
    if not exe:
        raise FFmpegError(_NO_FFMPEG)

    process = await asyncio.create_subprocess_exec(
        exe,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
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


def _tail(text: str, lines: int = 3) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])[:300]
