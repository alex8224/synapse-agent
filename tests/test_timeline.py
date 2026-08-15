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
    summarize_items,
    summarize_todos,
    tool_category,
    truncate_preview,
)


def test_tool_category_mapping():
    assert tool_category("read_file") == "read"
    assert tool_category("ls") == "list"
    assert tool_category("grep") == "search"
    assert tool_category("search_files") == "search"
    assert tool_category("find_files") == "glob"
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


def test_item_label_synapse_search_fallback():
    assert item_label("search_files", {"pattern": "TODO|FIXME"}) == "Searched TODO|FIXME"
    assert item_label("find_files", {"pattern": "**/*.py"}) == "Matched **/*.py"
    # No intent + no pattern still degrades to a phrase, not the bare tool name.
    assert item_label("search_files", {"path": "/src"}) == "Searched pattern"
    assert item_label("find_files", {"path": "/src"}) == "Matched src"


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


def test_summarize_running_subagent_has_no_duplicate_verbs():
    assert summarize_categories(["task"], running=True) == "Running 1 subagent"
    assert summarize_categories(["task", "task"], running=True) == (
        "Running 2 subagents"
    )


def test_summarize_items_uses_each_category_running_state():
    task = build_tool_item(
        {"name": "task", "args": {"description": "review"}}, item_id="task"
    )
    read = build_tool_item(
        {"name": "read_file", "args": {"file_path": "/x"}}, item_id="read"
    )
    task.status = "ok"

    assert summarize_items([task, read], running=True) == (
        "Launched 1 subagent, Running Read 1 file"
    )


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


# --------------------------------------------------------------------------- #
# subagent metadata binding (build_tool_item)
# --------------------------------------------------------------------------- #


def _task_call(**args):
    return {
        "name": "task",
        "args": {"intent": "审查修复", "subagent_type": "reviewer", **args},
    }


def test_item_label_task_prefers_intent_then_description():
    """The UI suffix change must not alter the base intent label."""
    assert item_label("task", {"intent": "审查修复"}) == "审查修复"
    assert item_label("task", {"description": "Review the fix"}) == "Review the fix"
    assert item_label("task", {}) == "Launched subagent"


def test_build_tool_item_task_binds_subagent_metadata():
    from synapse.runtime.subagent_specs import ResolvedSubagentDisplayConfig

    configs = {
        "reviewer": ResolvedSubagentDisplayConfig(
            name="reviewer",
            model="gpt-5.2",
            reasoning_effort="high",
            model_inherited=False,
            reasoning_effort_inherited=False,
        )
    }
    item = build_tool_item(
        _task_call(intent="审查修复"),
        item_id="t1",
        index=0,
        subagent_configs=configs,
    )
    assert item.name == "task"
    assert item.category == "task"
    assert item.label == "审查修复"
    assert item.subagent_name == "reviewer"
    assert item.subagent_model == "gpt-5.2"
    assert item.subagent_reasoning_effort == "high"
    assert item.subagent_model_inherited is False
    assert item.subagent_reasoning_inherited is False


def test_build_tool_item_task_inherited_metadata():
    from synapse.runtime.subagent_specs import ResolvedSubagentDisplayConfig

    configs = {
        "researcher": ResolvedSubagentDisplayConfig(
            name="researcher",
            model="main:model",
            reasoning_effort="high",
            model_inherited=True,
            reasoning_effort_inherited=True,
        )
    }
    item = build_tool_item(
        {"name": "task", "args": {"intent": "explore", "subagent_type": "researcher"}},
        item_id="t2",
        subagent_configs=configs,
    )
    assert item.subagent_name == "researcher"
    assert item.subagent_model == "main:model"
    assert item.subagent_model_inherited is True
    assert item.subagent_reasoning_inherited is True


def test_build_tool_item_task_missing_subagent_type():
    item = build_tool_item(
        {"name": "task", "args": {"intent": "explore"}},
        item_id="t3",
    )
    assert item.subagent_name is None
    assert item.subagent_model is None


def test_build_tool_item_task_unknown_name_keeps_name_only():
    item = build_tool_item(
        _task_call(intent="custom run"),
        item_id="t4",
        subagent_configs={},
    )
    # Unknown name (no config map entry): the name is still captured from the
    # call args; model/effort stay empty and are never guessed.
    assert item.subagent_name == "reviewer"
    assert item.subagent_model is None
    assert item.subagent_reasoning_effort is None


def test_build_tool_item_non_task_ignores_subagent_keys():
    item = build_tool_item(
        {"name": "read_file", "args": {"file_path": "/a", "subagent_type": "reviewer"}},
        item_id="t5",
        subagent_configs={},
    )
    assert item.subagent_name is None
    assert item.category == "read"


def test_build_tool_item_nested_sub_item_not_tagged():
    item = build_tool_item(
        _task_call(intent="nested"),
        item_id="t6",
        sub=True,
        subagent_configs={},
    )
    assert item.sub is True
    assert item.subagent_name is None


def test_build_tool_item_restore_snapshot_fallback():
    """Transcript restore path: no live config map, but persistence wrote the
    snapshot into args, so the metadata is rehydrated."""
    item = build_tool_item(
        {
            "name": "task",
            "args": {
                "label": "审查修复",
                "path": None,
                "subagent_type": "reviewer",
                "subagent_model": "gpt-5.1",
                "subagent_reasoning_effort": "medium",
                "subagent_model_inherited": True,
                "subagent_reasoning_inherited": False,
            },
        },
        item_id="hist-1",
    )
    assert item.subagent_name == "reviewer"
    assert item.subagent_model == "gpt-5.1"
    assert item.subagent_reasoning_effort == "medium"
    assert item.subagent_model_inherited is True
    assert item.subagent_reasoning_inherited is False


def test_item_label_task_restores_persisted_label():
    """Persisted transcripts store the intent under ``label``; the restored
    row must keep its original title instead of the generic fallback."""
    assert (
        item_label("task", {"label": "审查修复", "subagent_type": "reviewer"})
        == "审查修复"
    )
    # Real task calls never carry ``label``, so description still applies.
    assert (
        item_label("task", {"description": "Review the fix"}) == "Review the fix"
    )


def test_build_tool_item_config_lookup_is_case_insensitive():
    from synapse.runtime.subagent_specs import ResolvedSubagentDisplayConfig

    configs = {
        "reviewer": ResolvedSubagentDisplayConfig(
            name="reviewer",
            model="gpt-5.2",
            reasoning_effort="high",
            model_inherited=False,
            reasoning_effort_inherited=False,
        )
    }
    item = build_tool_item(
        {
            "name": "task",
            "args": {"intent": "审查", "subagent_type": "Reviewer"},
        },
        item_id="t7",
        subagent_configs=configs,
    )
    assert item.subagent_name == "Reviewer"
    assert item.subagent_model == "gpt-5.2"
    assert item.subagent_reasoning_effort == "high"
