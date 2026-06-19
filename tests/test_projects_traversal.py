"""GL-R1-007: asserting test for the artifact-read path-traversal guard (GL-R1-001).

Before the fix the guard validated only ``filename`` — ``slug`` and ``subdir`` flowed
unguarded into ``PROJECTS_ROOT/slug/subdir/filename``, so a ``..`` climb could read
files outside the projects root. These tests would fail if the guard regressed.
"""
from __future__ import annotations

from selran_canvas import projects


def test_read_artifact_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path)
    art = tmp_path / "proj" / "manuscript"
    art.mkdir(parents=True)
    (art / "page.md").write_text("hello", encoding="utf-8")
    assert projects.read_artifact("proj", "manuscript", "page.md") == "hello"


def test_read_artifact_blocks_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path)
    art = tmp_path / "proj" / "manuscript"
    art.mkdir(parents=True)
    (art / "page.md").write_text("hello", encoding="utf-8")
    # a file outside the projects root that an unguarded climb would reach
    (tmp_path.parent / "outside.txt").write_text("SECRET", encoding="utf-8")

    for slug, subdir, fn in [
        ("..", "..", "outside.txt"),
        ("proj", "..", "outside.txt"),
        ("proj", "manuscript", "../../../outside.txt"),
        ("proj/..", "manuscript", "page.md"),
        ("proj", "manuscript/..", "page.md"),
    ]:
        assert projects.read_artifact(slug, subdir, fn) is None, (slug, subdir, fn)
