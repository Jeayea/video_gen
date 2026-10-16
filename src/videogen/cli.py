"""命令行入口：`videogen serve` 启动工作台，`videogen doctor` 自检运行环境。

装好之后（`./install.sh` 或 `pip install -e .`）两种写法等价：

    .venv/bin/videogen serve
    .venv/bin/python -m videogen.cli serve

`.env`、`projects/`、`static/` 都按仓库根目录找（和 `run.sh` 一样先 cd 过去），
所以在哪个目录下敲命令都行；`--root` 可以指定别的工作目录。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _default_root() -> Path:
    """可编辑安装时是仓库根目录；装进 site-packages 时退回当前目录。"""
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").is_file() and (repo / "static").is_dir():
        return repo
    return Path.cwd()


def _enter_root(root: str | None) -> Path:
    path = Path(root).expanduser().resolve() if root else _default_root()
    os.chdir(path)
    return path


# ── serve ────────────────────────────────────────────────────


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    _enter_root(args.root)
    # main._bind_host() 读这个变量来判断「没设 API_TOKEN 却对外监听」
    os.environ["HOST"] = args.host
    print(f"videogen → http://{args.host}:{args.port}")
    uvicorn.run("videogen.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


# ── doctor ───────────────────────────────────────────────────


class _Report:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, msg: str) -> None:
        print(f"  ✓ {msg}")

    def warn(self, msg: str) -> None:
        print(f"  ! {msg}")

    def fail(self, msg: str) -> None:
        self.failed += 1
        print(f"  ✗ {msg}")


def _doctor(args: argparse.Namespace) -> int:
    root = _enter_root(args.root)

    from videogen import __version__
    from videogen.config import get_settings
    from videogen.utils.ffmpeg import resolve_ffmpeg, resolve_ffmpeg_for, resolve_ffprobe

    r = _Report()
    print(f"videogen {__version__} · 工作目录 {root}")

    # 版本不够时 pip 根本装不上（requires-python），这里只报一下用的是哪个
    r.ok(f"Python {sys.version.split()[0]}")

    env = root / ".env"
    if env.is_file():
        r.ok(".env 已存在")
    else:
        r.warn(".env 不存在，全部用默认值（cp .env.example .env 后填 key）")

    settings = get_settings()
    # 只报「有没有配」，绝不打印密钥本身
    if settings.has_llm_key:
        r.ok("OPENROUTER_API_KEY 已配置")
    else:
        r.fail("OPENROUTER_API_KEY 没配：生成圣经 / 剧本 / 分镜都会失败")

    if settings.video_provider == "openrouter":
        msg = f"视频：真实模型 {settings.openrouter_video_model}（按秒计费）"
        if settings.budget_usd > 0:
            r.ok(f"{msg}，预算上限 ${settings.budget_usd:g}")
        else:
            r.warn(f"{msg}，BUDGET_USD=0 没有花费上限")
    else:
        r.ok(f"视频：{settings.video_provider}（占位片段，不花钱）")

    ffmpeg = resolve_ffmpeg()
    if ffmpeg:
        r.ok(f"ffmpeg：{ffmpeg}")
    else:
        r.fail("找不到 ffmpeg：合成成片要用（brew install ffmpeg，或 pip install imageio-ffmpeg）")
    if resolve_ffprobe():
        r.ok("ffprobe 可用")
    else:
        r.warn("找不到 ffprobe：探测片段音轨 / 时长会降级（brew install ffmpeg 会带上）")
    if ffmpeg:
        burn_exe, caps = asyncio.run(resolve_ffmpeg_for({"subtitles"}))
        if "subtitles" in caps:
            r.ok(f"能烧字幕（libass）：{burn_exe}")
        else:
            r.warn("没有带 libass 的 ffmpeg：烧字幕会退回软字幕（pip install imageio-ffmpeg）")

    if settings.lipsync_engine == "wav2lip-onnx":
        if Path(settings.lipsync_python).is_file():
            r.ok("逐帧口型环境已装")
        else:
            r.fail(f"LIPSYNC_ENGINE=wav2lip-onnx 但 {settings.lipsync_python} 不存在"
                   "（先跑 tools/lipsync/install.sh）")

    if not settings.static_path.is_dir():
        r.fail(f"找不到前端目录 {settings.static_path}")

    print("自检通过" if not r.failed else f"{r.failed} 项需要处理")
    return 1 if r.failed else 0


# ── main ─────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videogen", description="AI 短剧自动化流水线")
    parser.add_argument("--root", help="工作目录（.env / projects/ 所在处），默认仓库根目录")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 Web 工作台")
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                       help="监听地址，默认只监听本机；对外监听前先设 API_TOKEN")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8001")))
    serve.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    serve.set_defaults(func=_serve)

    doctor = sub.add_parser("doctor", help="检查 key / ffmpeg / 字幕 / 口型环境（不联网、不花钱）")
    doctor.set_defaults(func=_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
