"""Unit tests for Cursor-style timeline pure model."""

from __future__ import annotations

from synapse.ui.timeline import (
    TODO_MARK_ACTIVE,
    TODO_MARK_DONE,
    TODO_MARK_PENDING,
    build_tool_item,
    format_preview_with_lines,
    format_todos_preview,
    item_label,
    match_tool_result,
    parse_todo_preview_lines,
    summarize_categories,
    summarize_todos,
    tool_category,
    truncate_preview,
)


def test_tool_category_mapping():
    assert tool_category("read_file") == "read"
    assert tool_category("ls") == "list"
    assert tool_category("grep") == "search"
    assert tool_category("execute") == "run"


def test_item_label_read_basename():
    assert item_label("read_file", {"file_path": "/docs/README.md"}) == "Read README.md"
    assert item_label("read_file", {"path": "src/agent.py"}) == "Read agent.py"


def test_item_label_search_and_run():
    assert "pattern" in item_label("grep", {"pattern": "StreamSink"}).lower() or (
        item_label("grep", {"pattern": "StreamSink"}) == "Search StreamSink"
        or item_label("grep", {"pattern": "StreamSink"}) == "Searched StreamSink"
    )
    assert item_label("execute", {"command": "pytest -q"}).startswith("Run ")


def test_item_label_prefers_intent():
    assert (
        item_label(
            "grep",
            {"intent": "搜索 StreamSink 定义", "pattern": "StreamSink"},
        )
        == "搜索 StreamSink 定义"
    )


def test_todos_label_and_preview_statuses():
    todos = [
        {"content": "explore repo", "status": "completed"},
        {"content": "deep dive agent", "status": "in_progress"},
        {"content": "write tests", "status": "pending"},
    ]
    label = item_label("write_todos", {"todos": todos})
    assert label.startswith("Todos 1/3")
    assert "deep dive agent" in label
    preview = format_todos_preview(todos)
    assert preview is not None
    assert "✓ explore repo" in preview
    assert "● deep dive agent" in preview
    assert "○ write tests" in preview
    assert "done 1 · doing 1 · todo 1" in preview
    item = build_tool_item(
        {"name": "write_todos", "args": {"todos": todos}},
        item_id="t-todo",
    )
    assert item.preview == preview
    assert summarize_todos(todos) is not None


def test_todo_marks_and_legacy_parse():
    assert TODO_MARK_DONE == "✓"
    assert TODO_MARK_ACTIVE == "●"
    assert TODO_MARK_PENDING == "○"
    rows = parse_todo_preview_lines("✓ a\n● b\n○ c\n— done 1 · doing 1 · todo 1")
    assert [r.kind for r in rows] == ["done", "active", "pending"]
    # Legacy ASCII marks still parse for old sessions.
    legacy = parse_todo_preview_lines("[x] old done\n[~] old active\n[ ] old pending")
    assert [r.kind for r in legacy] == ["done", "active", "pending"]
    assert legacy[0].mark == TODO_MARK_DONE


def test_summarize_categories_cursor_style():
    s = summarize_categories(
        ["ls", "read_file", "read_file", "grep"] + ["read_file"] * 20,
        running=False,
    )
    assert s.startswith("Listed 1 dir")
    assert "Read 22 files" in s
    assert "Searched 1 pattern" in s


def test_summarize_running_prefix():
    s = summarize_categories(["read_file", "read_file"], running=True)
    assert s.startswith("Running ")
    assert "Read 2 files" in s


def test_truncate_preview_limits():
    body = "\n".join(f"line {i}" for i in range(100))
    preview = truncate_preview(body, max_chars=500, max_lines=10)
    assert preview is not None
    assert preview.count("\n") <= 10
    assert "…" in preview


def test_truncate_preview_empty():
    assert truncate_preview("") is None
    assert truncate_preview(None) is None


def test_format_preview_with_lines():
    text = format_preview_with_lines("a\nb\nc", max_lines=2)
    assert "1" in text and "a" in text
    assert "…" in text


def test_build_tool_item_from_dict():
    item = build_tool_item(
        {"name": "read_file", "args": {"file_path": "/README.md"}},
        item_id="t1",
        index=0,
    )
    assert item.id == "t1"
    assert item.category == "read"
    assert item.label == "Read README.md"
    assert item.path == "/README.md"
    assert item.status == "running"


def test_match_tool_result_first_running():
    a = build_tool_item(
        {"name": "read_file", "args": {"path": "/a"}},
        item_id="1",
    )
    b = build_tool_item(
        {"name": "read_file", "args": {"path": "/b"}},
        item_id="2",
    )
    a.status = "ok"
    matched = match_tool_result([a, b], "read_file")
    assert matched is not None
    assert matched.id == "2"


def test_match_tool_result_does_not_steal_parent_task():
    """Nested read_file must not finish a pending parent task item."""
    task = build_tool_item(
        {"name": "task", "args": {"description": "explore"}},
        item_id="task-1",
    )
    matched = match_tool_result([task], "read_file")
    assert matched is None
