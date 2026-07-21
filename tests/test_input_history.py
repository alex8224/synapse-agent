"""Tests for project input history (TUI up/down)."""

from __future__ import annotations

from pathlib import Path

from synapse.input_history import InputHistory


def test_input_history_add_up_down(tmp_path: Path):
    path = tmp_path / "history"
    h = InputHistory(path, max_entries=10)
    h.add("first")
    h.add("second")
    h.add("third")
    assert h.entries == ["first", "second", "third"]

    # Start browsing from a draft
    assert h.up("draft-now") == "third"
    assert h.up("third") == "second"
    assert h.up("second") == "first"
    # Clamp at oldest
    assert h.up("first") == "first"

    assert h.down("") == "second"
    assert h.down("") == "third"
    # Leave history → restore draft
    assert h.down("") == "draft-now"
    # Further down does nothing
    assert h.down("draft-now") is None


def test_input_history_persists_and_dedupes(tmp_path: Path):
    path = tmp_path / ".synapse" / "history"
    h = InputHistory.for_project(tmp_path)
    assert h.path == path.resolve() or h.path == path
    h.add("hello")
    h.add("hello")  # consecutive dedupe
    h.add("world")
    h2 = InputHistory(path)
    assert h2.entries == ["hello", "world"]


def test_input_history_skips_readline_header(tmp_path: Path):
    path = tmp_path / "history"
    path.write_text("_HiStOrY_V2_\none\ntwo\n", encoding="utf-8")
    h = InputHistory(path)
    assert h.entries == ["one", "two"]


def test_input_history_reads_gbk_legacy(tmp_path: Path):
    path = tmp_path / "history"
    # Simulate Windows console history saved as GBK (common on zh-CN).
    path.write_bytes("hello\n向下滚动\n".encode("gbk"))
    h = InputHistory(path)
    assert h.entries[0] == "hello"
    assert "向下" in h.entries[1]
    h.add("new-utf8")
    # Subsequent saves are UTF-8.
    assert "new-utf8" in path.read_text(encoding="utf-8")
