"""ffmpeg 候选解析：按能力挑 exe（烧字幕要 libass 的关键路径）。"""

from __future__ import annotations

import pytest

from videogen.config import get_settings
from videogen.utils import ffmpeg as ffmpeg_utils


@pytest.fixture(autouse=True)
def _clear_cache():
    ffmpeg_utils.reset_ffmpeg_cache()
    yield
    ffmpeg_utils.reset_ffmpeg_cache()


def test_candidates_are_ordered_and_deduplicated(monkeypatch, tmp_path):
    first = tmp_path / "ffmpeg-first"
    first.write_text("#!/bin/sh\n")
    second = tmp_path / "ffmpeg-second"
    second.write_text("#!/bin/sh\n")

    monkeypatch.setattr(get_settings(), "ffmpeg_path", str(first))
    monkeypatch.setattr(
        ffmpeg_utils.shutil, "which", lambda name: str(second) if name == "ffmpeg" else None
    )
    monkeypatch.setattr(
        ffmpeg_utils, "ffmpeg_candidates", ffmpeg_utils.ffmpeg_candidates
    )  # 明确用真实现

    candidates = ffmpeg_utils.ffmpeg_candidates()
    assert candidates[0] == str(first.resolve())
    assert str(second) in candidates
    assert len(candidates) == len(set(candidates))


@pytest.mark.asyncio
async def test_resolve_ffmpeg_for_upgrades_to_capable_candidate(monkeypatch):
    brew, portable = "/opt/brew/ffmpeg", "/opt/portable/ffmpeg"
    monkeypatch.setattr(ffmpeg_utils, "resolve_ffmpeg", lambda: brew)
    monkeypatch.setattr(ffmpeg_utils, "ffmpeg_candidates", lambda: [brew, portable])

    async def fake_caps() -> set[str]:
        return {"mov_text", "libx264"}

    async def fake_caps_for(exe: str) -> set[str]:
        return {"mov_text", "libx264"} if exe == brew else {"mov_text", "libx264", "subtitles"}

    monkeypatch.setattr(ffmpeg_utils, "ffmpeg_capabilities", fake_caps)
    monkeypatch.setattr(ffmpeg_utils, "ffmpeg_capabilities_for", fake_caps_for)

    exe, caps = await ffmpeg_utils.resolve_ffmpeg_for({"subtitles"})
    assert exe == portable and "subtitles" in caps

    # 不需要特殊能力时不要乱换 exe
    exe, _ = await ffmpeg_utils.resolve_ffmpeg_for(set())
    assert exe == brew


@pytest.mark.asyncio
async def test_auto_upgrade_can_be_disabled(monkeypatch):
    brew, portable = "/opt/brew/ffmpeg", "/opt/portable/ffmpeg"
    monkeypatch.setattr(get_settings(), "ffmpeg_auto_upgrade", False)
    monkeypatch.setattr(ffmpeg_utils, "resolve_ffmpeg", lambda: brew)
    monkeypatch.setattr(ffmpeg_utils, "ffmpeg_candidates", lambda: [brew, portable])

    async def fake_caps() -> set[str]:
        return {"mov_text"}

    async def fake_caps_for(exe: str) -> set[str]:
        return {"mov_text", "subtitles"}

    monkeypatch.setattr(ffmpeg_utils, "ffmpeg_capabilities", fake_caps)
    monkeypatch.setattr(ffmpeg_utils, "ffmpeg_capabilities_for", fake_caps_for)

    exe, caps = await ffmpeg_utils.resolve_ffmpeg_for({"subtitles"})
    assert exe == brew and "subtitles" not in caps  # 保持主 ffmpeg，由上层降级并告警


@pytest.mark.asyncio
async def test_no_candidate_satisfies_returns_primary(monkeypatch):
    brew = "/opt/brew/ffmpeg"
    monkeypatch.setattr(ffmpeg_utils, "resolve_ffmpeg", lambda: brew)
    monkeypatch.setattr(ffmpeg_utils, "ffmpeg_candidates", lambda: [brew])

    async def fake_caps() -> set[str]:
        return {"mov_text"}

    monkeypatch.setattr(ffmpeg_utils, "ffmpeg_capabilities", fake_caps)
    exe, caps = await ffmpeg_utils.resolve_ffmpeg_for({"subtitles"})
    assert exe == brew and "subtitles" not in caps


@pytest.mark.asyncio
async def test_run_ffmpeg_honours_explicit_exe():
    """`exe=` 参数必须真的换二进制（用便携版读一次滤镜列表验证）。"""
    import subprocess

    portable = pytest.importorskip("imageio_ffmpeg").get_ffmpeg_exe()
    out = await ffmpeg_utils.run_ffmpeg_capture(
        ["-hide_banner", "-filters"], exe=portable, timeout_sec=30.0
    )
    assert "subtitles" in out
    # 顺带确认这个 exe 真的被当作可执行文件跑起来了
    assert subprocess.run([portable, "-version"], capture_output=True).returncode == 0
