"""Add tests for subagent tool isolation."""
from pathlib import Path

# timeline match tests
tp = Path("tests/test_timeline.py")
tt = tp.read_text(encoding="utf-8")
if "test_match_tool_result_does_not_steal_parent_task" not in tt:
    tt = tt.rstrip() + """


def test_match_tool_result_does_not_steal_parent_task():
    \"\"\"Nested read_file must not finish a pending parent task item.\"\"\"
    task = build_tool_item(
        {\"name\": \"task\", \"args\": {\"description\": \"explore skills\"}},
        item_id=\"g1-0\",
    )
    matched = match_tool_result([task], \"read_file\")
    assert matched is None
    assert task.status == \"running\"
    done = match_tool_result([task], \"task\")
    assert done is not None
    assert done.id == \"g1-0\"
"""
    tp.write_text(tt + "\n", encoding="utf-8", newline="\n")

# tui sink tests
sp = Path("tests/test_tui_sink.py")
st = sp.read_text(encoding="utf-8")
if "test_textual_sink_ignores_nested_and_empty_legacy" not in st:
    st = st.rstrip() + """


def test_textual_sink_ignores_nested_and_empty_legacy():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    # Nested subagent noise must not create tool groups.
    sink.tool_result(\"read_file\", \"ok\", sub=True)
    assert not [c for c in app.calls if c[0] == \"write_tool_group_header\"]
    # Unmatched empty legacy must not create \"0 tools\".
    sink.tool_result(\"read_file\", \"ok\", sub=False)
    assert not [c for c in app.calls if c[0] == \"write_tool_group_header\"]


def test_textual_sink_keeps_running_task_group_open():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    item = build_tool_item(
        {\"name\": \"task\", \"args\": {\"description\": \"explore\"}},
        item_id=\"g1-0\",
        index=0,
    )
    sink.tool_calls_started([item], parallel=False)
    sink.tool_item_started(item)
    # Intermediate thought/answer while task still running must not close group.
    sink.write_reasoning(\"waiting for subagent\")
    sink.close_reasoning()
    sink.write_answer_complete(\"subagent still working…\", msg_id=\"m-mid\")
    assert len([c for c in app.calls if c[0] == \"close_tool_group\"]) == 0
    sink.tool_item_finished(item.id, status=\"ok\")
    sink.tool_group_closed(\"g1\")
    assert len([c for c in app.calls if c[0] == \"close_tool_group\"]) == 1
    headers = [c[1][0] for c in app.calls if c[0] == \"write_tool_group_header\"]
    assert headers
    assert \"subagent\" in headers[0].lower() or \"Launched\" in headers[0]
"""
    sp.write_text(st + "\n", encoding="utf-8", newline="\n")

print("tests appended")
