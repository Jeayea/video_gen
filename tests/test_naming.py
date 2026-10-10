"""项目目录名：按用户输入起名，重名加序号，不能路径穿越。"""

from __future__ import annotations

import pytest

from videogen.models.project import InputMode, Project
from videogen.utils.naming import DEFAULT_TITLE, derive_title, project_slug, slugify


def test_slug_keeps_chinese_and_drops_path_characters():
    assert slugify("孔乙己 · 第一场试拍（Wan 3.0）") == "孔乙己-第一场试拍-wan-3-0"
    assert slugify("../../etc/passwd") == "etc-passwd"
    assert slugify("   ") == ""


def test_title_comes_from_input_when_missing():
    novel = "孔乙己\n\n鲁镇的酒店的格局，是和别处不同的"
    assert derive_title("", novel, "novel") == "孔乙己"
    assert derive_title(DEFAULT_TITLE, "《变形记》\n作者：卡夫卡", "novel") == "变形记"
    theme = "【本片为虚构作品】一个跳水少女的逆袭，十米台之上。"
    assert derive_title("", theme, "theme") == "一个跳水少女的逆袭"
    assert derive_title("我的片子", novel, "novel") == "我的片子"
    assert derive_title("", "", "theme") == DEFAULT_TITLE


def test_slug_is_dated():
    assert project_slug("孔乙己", "2026-10-10T03:07:51+00:00") == "20261010-孔乙己"


def test_create_project_names_directory_and_dedupes(store):
    first = store.create_project(
        Project(title="", input_mode=InputMode.NOVEL, input_text="孔乙己\n\n鲁镇的酒店",
                created_at="2026-10-10T00:00:00+00:00")
    )
    second = store.create_project(
        Project(title="孔乙己", created_at="2026-10-10T08:00:00+00:00")
    )
    assert first.id == "20261010-孔乙己"
    assert first.title == "孔乙己"
    assert second.id == "20261010-孔乙己-2"
    assert store.load_project(first.id).title == "孔乙己"
    assert {p.id for p in store.list_projects()} >= {first.id, second.id}


@pytest.mark.parametrize("bad", ["..", "a/b", "a.b", "-x", "", "a\n", "x" * 65])
def test_invalid_ids_rejected(store, bad):
    assert not store.is_valid_id(bad)

