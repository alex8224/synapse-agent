"""Gitignore tool path matcher tests."""

from __future__ import annotations

from pathlib import Path
from synapse.tool_ignore import ToolIgnoreMatcher


def test_from_patterns_basic():
    m = ToolIgnoreMatcher.from_patterns([".venv", "__pycache__/"])
    assert m.is_ignored(".venv", is_dir=True)
    assert m.is_ignored("foo/.venv/bar.py")
    assert m.is_ignored("__pycache__")
    assert m.is_ignored("__pycache__/foo.pyc")
    assert m.is_ignored("src/__pycache__")


def test_from_patterns_no_match():
    m = ToolIgnoreMatcher.from_patterns([".venv", "__pycache__/"])
    assert not m.is_ignored("src/main.py")
    assert not m.is_ignored(".env.example")
    assert not m.is_ignored("")


def test_from_patterns_negate():
    m = ToolIgnoreMatcher.from_patterns([".venv", "!.venv/runtime"])
    assert m.is_ignored(".venv/lib.py")
    assert not m.is_ignored(".venv/runtime")
    assert not m.is_ignored(".venv/runtime/foo.py")


def test_anchored_pattern():
    m = ToolIgnoreMatcher.from_patterns(["/build/"])
    assert m.is_ignored("build/output")
    assert m.is_ignored("build/a/b/c")
    assert not m.is_ignored("src/build")


def test_globstar_pattern():
    m = ToolIgnoreMatcher.from_patterns(["**/*.log"])
    assert m.is_ignored("a.log")
    assert m.is_ignored("logs/server.log")
    assert not m.is_ignored("a.log.txt")


def test_comment_and_blank_lines():
    m = ToolIgnoreMatcher.from_patterns(["# comment", "", "  *.pyc  "])
    assert m.rule_count == 1
    assert m.is_ignored("foo.pyc")
    assert not m.is_ignored("foo.py")


def test_from_workspace(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".gitignore").write_text(".venv\n*.pyc\n/dist/\n")
    (root / ".venv").mkdir()
    (root / "build").mkdir()
    m = ToolIgnoreMatcher.from_workspace(root)
    assert m.rule_count == 3
    assert m.is_ignored(".venv", is_dir=True)
    assert m.is_ignored(".venv/foo.py")
    assert m.is_ignored("bar.pyc")
    assert m.is_ignored("dist")
    assert not m.is_ignored("build")


def test_empty_rules_never_ignored():
    m = ToolIgnoreMatcher([])
    assert not m.is_ignored(".venv", is_dir=True)
    assert not m.is_ignored("anything")


def test_normalize_drops_virtual_prefixes():
    assert ToolIgnoreMatcher.normalize("/src/main.py") == "src/main.py"
    assert ToolIgnoreMatcher.normalize("./foo") == "foo"
    assert ToolIgnoreMatcher.normalize("bar") == "bar"
