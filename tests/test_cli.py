"""命令行入口与配置模板：`videogen doctor` 不联网、不泄露 key；两份 .env 模板不和代码漂移。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from videogen import cli
from videogen.config import Settings, get_settings

REPO = Path(__file__).resolve().parents[1]


def _template(name: str) -> dict[str, str]:
    pairs = {}
    for line in (REPO / name).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            pairs[m.group(1)] = m.group(2)
    return pairs


@pytest.mark.parametrize("name", [".env.example", ".env.full.example"])
def test_env_templates_only_use_real_settings_and_defaults(name):
    """模板里每一项都是真实配置项，且值就是代码默认值 —— 照抄模板不会悄悄改变行为。"""
    fields = {k.upper(): k for k in Settings.model_fields}
    defaults = Settings(_env_file=None)
    for key, raw in _template(name).items():
        assert key in fields, f"{name}: {key} 不是配置项"
        field = fields[key]
        expected = getattr(defaults, field)
        if raw == "":
            empty = ("", None, 0, 0.0, [], {})
            assert expected in empty, f"{name}: {key} 留空但默认值是 {expected!r}"
        else:
            parsed = getattr(Settings(_env_file=None, **{field: raw}), field)
            assert parsed == expected, f"{name}: {key}={raw} 和默认值 {expected!r} 不一致"


def test_minimal_template_is_a_subset_of_the_full_one():
    assert set(_template(".env.example")) <= set(_template(".env.full.example"))


def test_serve_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    args = cli.build_parser().parse_args(["serve"])
    assert (args.host, args.port, args.reload) == ("127.0.0.1", 8001, False)


def test_doctor_reports_missing_key_and_never_prints_it(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # 测完把 cwd 还原（doctor 会 chdir 到 --root）
    settings = get_settings()
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    assert cli.main(["--root", str(tmp_path), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY 没配" in out
    assert ".env 不存在" in out

    secret = "sk-or-v1-" + "0123456789abcdef" * 4
    monkeypatch.setattr(settings, "openrouter_api_key", secret)
    cli.main(["--root", str(tmp_path), "doctor"])
    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY 已配置" in out
    assert secret not in out


def test_doctor_warns_real_video_model_without_budget(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "video_provider", "openrouter")
    monkeypatch.setattr(settings, "budget_usd", 0.0)
    cli.main(["--root", str(tmp_path), "doctor"])
    assert "没有花费上限" in capsys.readouterr().out
