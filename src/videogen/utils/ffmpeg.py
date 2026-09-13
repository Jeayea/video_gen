from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Iterable
from pathlib import Path

from videogen.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "FFmpegError",
    "describe_media",
    "ffmpeg_available",
    "ffmpeg_candidates",
    "ffmpeg_capabilities",
    "ffmpeg_capabilities_for",
    "ffprobe_available",
    "probe_duration",
    "probe_stream_durations",
    "probe_streams",
    "reset_ffmpeg_cache",
    "resolve_ffmpeg",
    "resolve_ffmpeg_for",
    "resolve_ffprobe",
    "run_ffmpeg",
    "run_ffmpeg_bytes",
    "run_ffmpeg_capture",
]


class FFmpegError(RuntimeError):
    """ffmpeg 不可用或执行失败（消息已脱敏）。"""


_resolved: str | None = None
_resolved_once = False
_probe_resolved: str | None = None
_probe_once = False
_caps: set[str] | None = None
#: 非主 ffmpeg 的能力缓存（key = 可执行文件绝对路径）
_caps_by_exe: dict[str, set[str]] = {}


def ffmpeg_candidates() -> list[str]:
    """所有能找到的 ffmpeg，按优先级去重。

    顺序：`FFMPEG_PATH` → `PATH` → `imageio-ffmpeg` 便携版。

    做成「候选列表」而不是「只挑一个」是有原因的：不同来源的 ffmpeg 编译选项
    差别很大（brew 的 9.0.1 就没有 libass，烧不了字幕，而 imageio-ffmpeg 的
    便携版带 libass），所以要能按**需要的能力**换一个来用。
    """
    out: list[str] = []
    configured = (get_settings().ffmpeg_path or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            out.append(str(candidate))
        else:
            found = shutil.which(configured)
            if found:
                out.append(found)

    found = shutil.which("ffmpeg")
    if found:
        out.append(found)

    try:  # 可选依赖：pip install imageio-ffmpeg
        import imageio_ffmpeg  # type: ignore[import-not-found]

        out.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 - 没有就是没有
        pass

    seen: set[str] = set()
    result: list[str] = []
    for exe in out:
        key = str(Path(exe).resolve()) if Path(exe).is_file() else exe
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def resolve_ffmpeg() -> str | None:
    """按优先级找 ffmpeg 可执行文件。

    1. `.env` 里的 `FFMPEG_PATH`（若显式配置且存在）
    2. `PATH` 里的 ffmpeg
    3. `imageio-ffmpeg` 附带的便携版（`pip install imageio-ffmpeg`）

    找不到返回 None —— 调用方必须显式处理，不允许假装成功。

    需要**特定能力**（例如烧字幕要 libass）时用 `resolve_ffmpeg_for()`，
    它会在这个顺序之外自动换用能满足要求的候选。
    """
    global _resolved, _resolved_once
    if _resolved_once:
        return _resolved

    _resolved_once = True
    candidates = ffmpeg_candidates()
    _resolved = candidates[0] if candidates else None

    if _resolved and "imageio-ffmpeg" in _resolved:
        logger.info("使用 imageio-ffmpeg 自带的 ffmpeg")

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
    _caps_by_exe.clear()


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


async def ffmpeg_capabilities_for(exe: str) -> set[str]:
    """探测**指定** ffmpeg 的滤镜/编码器能力（按可执行文件缓存）。"""
    if not exe:
        return set()
    if exe in _caps_by_exe:
        return _caps_by_exe[exe]

    caps: set[str] = set()
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
        logger.warning("ffmpeg 能力探测失败（%s），按最小能力处理", exe, exc_info=True)

    _caps_by_exe[exe] = caps
    return caps


async def ffmpeg_capabilities() -> set[str]:
    """探测主 ffmpeg 支持的滤镜/编码器（结果缓存）。

    只关心合成链路真正会用到的几项：没有 libass 就烧不了字幕，
    没有 mov_text 就内嵌不了软字幕 —— 必须能降级而不是直接崩。
    """
    global _caps
    if _caps is not None:
        return _caps

    exe = resolve_ffmpeg()
    if not exe:
        _caps = set()
        return _caps

    _caps = await ffmpeg_capabilities_for(exe)
    return _caps


async def resolve_ffmpeg_for(required: Iterable[str]) -> tuple[str | None, set[str]]:
    """挑一个满足 `required` 能力的 ffmpeg，返回 `(exe, 它的能力集合)`。

    先试主 ffmpeg；它缺能力时，若 `FFMPEG_AUTO_UPGRADE=true`（默认），
    就按候选顺序换用第一个满足要求的 —— 典型场景是 `.env` 里
    `FFMPEG_PATH=ffmpeg` 指到了没有 libass 的 brew 版，而
    `imageio-ffmpeg` 的便携版能烧字幕。

    找不到满足要求的就返回主 ffmpeg（**让调用方降级**，绝不假装能烧）。
    """
    needed = {item for item in required if item}
    primary = resolve_ffmpeg()
    primary_caps = await ffmpeg_capabilities() if primary else set()

    if not needed or (primary and needed <= primary_caps):
        return primary, primary_caps

    if not get_settings().ffmpeg_auto_upgrade:
        return primary, primary_caps

    for exe in ffmpeg_candidates():
        if exe == primary:
            continue
        caps = await ffmpeg_capabilities_for(exe)
        if needed <= caps:
            logger.info(
                "主 ffmpeg 缺少 %s，本次改用 %s", ",".join(sorted(needed)), exe
            )
            return exe, caps

    return primary, primary_caps


def _tokens(output: str) -> list[str]:
    names: list[str] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("Filters:"):
            names.append(parts[1])
    return names


async def run_ffmpeg_capture(
    args: list[str],
    *,
    timeout_sec: float = 120.0,
    cwd: Path | None = None,
    exe: str | None = None,
) -> str:
    """异步执行 ffmpeg 并返回输出摘要（用于需要读输出的场景）。"""
    executable = exe or resolve_ffmpeg()
    if not executable:
        raise FFmpegError(_NO_FFMPEG)
    code, out = await _capture(executable, args, timeout_sec=timeout_sec, cwd=cwd)
    if code != 0:
        raise FFmpegError(f"ffmpeg 退出码 {code}：{_tail(out)}")
    return out


async def run_ffmpeg_bytes(
    args: list[str],
    *,
    timeout_sec: float = 120.0,
    cwd: Path | None = None,
    exe: str | None = None,
) -> bytes:
    """异步执行 ffmpeg 并返回**原始 stdout 字节**（例如 `-f rawvideo -`）。

    单独开一个函数而不是复用 `run_ffmpeg_capture()`：那个把 stdout/stderr 合并后
    再按 utf-8 解码，像素数据会被彻底破坏。这里 stdout 只收字节，stderr 单独收。
    """
    executable = exe or resolve_ffmpeg()
    if not executable:
        raise FFmpegError(_NO_FFMPEG)

    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise FFmpegError(f"ffmpeg 执行超时（{timeout_sec:.0f}s）") from exc
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise FFmpegError(f"ffmpeg 退出码 {process.returncode}：{' | '.join(tail)[:300]}")
    return stdout or b""


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


async def probe_streams(path: Path, *, timeout_sec: float = 30.0) -> set[str] | None:
    """列出媒体里的流类型（`{"video", "audio"}`）。

    返回 `None` 表示**探测不了**（没有 ffprobe）—— 调用方必须显式处理，
    不能猜「有没有音轨」，否则会拼出一条 ffmpeg 直接报错的滤镜图。
    """
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
                "stream=codec_type",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            timeout_sec=timeout_sec,
        )
    except Exception:
        logger.debug("ffprobe 读取流信息失败: %s", path, exc_info=True)
        return None
    if code != 0:
        return None
    kinds = {line.strip().lower() for line in out.splitlines() if line.strip()}
    return kinds or None


async def probe_stream_durations(
    path: Path, *, timeout_sec: float = 30.0
) -> dict[str, float] | None:
    """按流类型读时长，返回 `{"video": 41.83, "audio": 34.68}`。

    同类流有多条时取**最长**的那条（音频多轨时以主音轨为准）。

    返回 `None` 表示探测不了（没有 ffprobe）；某一类缺失就是字典里没这个 key ——
    两者都要求调用方显式区分「没有音轨」和「测不出来」。
    """
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
                "stream=codec_type,duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            timeout_sec=timeout_sec,
        )
    except Exception:
        logger.debug("ffprobe 读取流时长失败: %s", path, exc_info=True)
        return None
    if code != 0:
        return None

    durations: dict[str, float] = {}
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        kind = parts[0].strip().lower()
        try:
            value = float(parts[1])
        except ValueError:
            continue  # 有些容器写 N/A，交给调用方当「测不出来」
        if value > 0:
            durations[kind] = max(durations.get(kind, 0.0), value)
    return durations or None


_NO_FFMPEG = (
    "未找到 ffmpeg，请安装（brew install ffmpeg）或在 .env 设置 FFMPEG_PATH，"
    "或 pip install imageio-ffmpeg"
)


async def run_ffmpeg(
    args: list[str],
    *,
    timeout_sec: float = 120.0,
    cwd: Path | None = None,
    exe: str | None = None,
) -> None:
    """异步执行 ffmpeg，失败时抛出带 stderr 摘要的 FFmpegError。

    `cwd` 用于让滤镜参数里的文件名保持相对路径（`subtitles=` 的转义很坑，
    切到产物目录里只写文件名最稳）。

    `exe` 用于指定**非主** ffmpeg（例如烧字幕要用带 libass 的那个候选），
    留空就用主 ffmpeg。
    """
    executable = exe or resolve_ffmpeg()
    if not executable:
        raise FFmpegError(_NO_FFMPEG)

    process = await asyncio.create_subprocess_exec(
        executable,
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
