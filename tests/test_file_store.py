"""`FileStore` 的健壮性：**一个坏文件不能拖垮整个项目列表**。

review 报的 P0：`_load` 直接把 `ValidationError` 抛出去，而 `list_projects()`
会遍历所有项目 —— 手改坏一个 `meta.json` 就让 `/api/projects` 变成 500，
其余项目也跟着看不见。现在坏文件会被隔离成 `.bad` 并返回 None。
"""

from __future__ import annotations

import json

from videogen.models.bible import Character, StoryBible
from videogen.models.project import Project
from videogen.utils.file_store import FileStore


def write_broken(store: FileStore, project_id: str, name: str, payload: str) -> None:
    directory = store.ensure_project_dir(project_id)
    (directory / f"{name}.json").write_text(payload, encoding="utf-8")


def test_corrupt_meta_does_not_break_project_listing(tmp_path):
    store = FileStore(tmp_path)
    good = Project(title="好项目")
    store.save_project(good)

    # 三种坏法：截断的 JSON、字段非法、类型错
    for index, payload in enumerate(
        [
            '{"id": "brokenjson01", ',
            '{"id": "badstatus001", "status": "NOT_A_STATUS"}',
            '{"id": "badtype0001", "target_duration_sec": "很久"}',
        ]
    ):
        project_id = ["brokenjson01", "badstatus001", "badtype0001"][index]
        write_broken(store, project_id, "meta", payload)

    listed = store.list_projects()
    assert [p.id for p in listed] == [good.id], "坏项目应被跳过，好项目必须还在"

    # 坏文件被改名隔离（不删除），可以人工修好再放回来
    assert (tmp_path / "brokenjson01" / "meta.json.bad").is_file()
    assert store.load_project("brokenjson01") is None


def test_corrupt_bible_is_isolated(tmp_path):
    """不止 meta：任何一个产物坏了都只影响它自己。"""
    store = FileStore(tmp_path)
    project = Project(title="有坏圣经的项目")
    store.save_project(project)
    write_broken(store, project.id, "story_bible", "{不是 JSON")

    assert store.load_bible(project.id) is None
    assert store.load_project(project.id).title == "有坏圣经的项目"
    assert (tmp_path / project.id / "story_bible.json.bad").is_file()


def test_valid_files_are_untouched(tmp_path):
    store = FileStore(tmp_path)
    project = Project(title="正常项目")
    store.save_project(project)
    store.save_bible(project.id, StoryBible(characters=[Character(id="c1", name="林悦")]))

    assert store.load_project(project.id).title == "正常项目"
    assert store.load_bible(project.id).characters[0].name == "林悦"
    assert not list(tmp_path.rglob("*.bad")), "正常读取不该产生隔离文件"


def test_missing_and_invalid_id_are_none(tmp_path):
    store = FileStore(tmp_path)
    assert store.load_project("不存在000") is None
    assert store.load_project("../../etc/passwd") is None


def test_save_is_atomic_and_readable(tmp_path):
    store = FileStore(tmp_path)
    project = Project(title="原子写")
    store.save_project(project)
    payload = json.loads((tmp_path / project.id / "meta.json").read_text(encoding="utf-8"))
    assert payload["title"] == "原子写"
    # 目录里不该留下临时文件
    leftovers = [p.name for p in (tmp_path / project.id).iterdir() if p.name.startswith(".meta.")]
    assert leftovers == []


def test_transient_read_error_does_not_quarantine(tmp_path, monkeypatch):
    """读文件时的 OSError（句柄用完 / 权限）不等于文件坏了：不能隔离成 .bad、让项目从列表里消失。"""
    from pathlib import Path

    store = FileStore(tmp_path / "projects")
    project = Project(title="瞬时错误")
    store.save_project(project)
    real = Path.read_text

    def flaky(self, *args, **kwargs):
        if self.name == "meta.json":
            raise OSError(24, "Too many open files")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert store.load_project(project.id) is None
    monkeypatch.setattr(Path, "read_text", real)
    assert store.load_project(project.id) is not None  # 文件还在、没被改名
