"""Unit tests for Cursor-style Textual StreamSink (item API)."""

from __future__ import annotations

from synapse.ui.timeline import ToolItem, build_tool_item, summarize_categories
from synapse.ui.tui import (
    AnswerBlock,
    TextualStreamSink,
    ThoughtBlock,
    ToolGroupBlock,
    format_answer_divider,
    format_token_count,
    format_turn_rail_preview,
    short_model_name,
    short_workspace_label,
    soften_turn_footer,
    stream_tail_preview,
)


class _FakeApp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def call_from_thread(self, fn, *args, **kwargs):  # noqa: ANN001
        fn(*args, **kwargs)

    def set_activity(self, phase: str, detail: str = "", reset_timer: bool = False) -> None:
        self.calls.append(("set_activity", (phase, detail, reset_timer), {}))

    def set_stream(self, kind: str, body: str, elapsed_s: float = 0.0) -> None:
        self.calls.append(("set_stream", (kind, body), {"elapsed_s": elapsed_s}))

    def clear_stream(self) -> None:
        self.calls.append(("clear_stream", (), {}))

    def commit_thought(self, elapsed_s: float, body: str) -> None:
        self.calls.append(("commit_thought", (elapsed_s, body), {}))

    def commit_answer(self, text: str) -> None:
        self.calls.append(("commit_answer", (text,), {}))

    def write_tool_group_header(self, summary: str, collapsed: bool = False) -> None:
        self.calls.append(("write_tool_group_header", (summary, collapsed), {}))

    def update_tool_group_header(self, summary: str) -> None:
        self.calls.append(("update_tool_group_header", (summary,), {}))

    def write_tool_item(self, item: ToolItem) -> None:
        self.calls.append(("write_tool_item", (item.label,), {}))

    def update_tool_item(
        self,
        item_id: str,
        *,
        status: str | None = None,
        preview: str | None = None,
        error: bool | None = None,
        label: str | None = None,
        path: str | None = None,
        name: str | None = None,
        category: str | None = None,
    ) -> None:
        self.calls.append(
            (
                "update_tool_item",
                (item_id,),
                {
                    "status": status,
                    "preview": preview,
                    "error": error,
                    "label": label,
                    "path": path,
                    "name": name,
                    "category": category,
                },
            )
        )

    def write_tool_preview(self, item_id: str, preview: str, *, error: bool = False) -> None:
        self.calls.append(("write_tool_preview", (item_id, preview), {"error": error}))

    def close_tool_group(self) -> None:
        self.calls.append(("close_tool_group", (), {}))


    def append_meta(self, message: str) -> None:
        self.calls.append(("append_meta", (message,), {}))

    def append_event(self, message: str, style: str = "dim") -> None:
        self.calls.append(("append_event", (message, style), {}))


def test_summarize_categories_cursor_style():
    s = summarize_categories(
        ["ls", "read_file", "read_file", "grep", "read_file"],
        running=False,
    )
    assert s.startswith("Listed 1 dir")
    assert "Read 3 files" in s
    assert "Searched 1 pattern" in s


def test_textual_sink_streams_answer_in_main_pane():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    sink._min_stream_interval = 0
    sink.write_answer_token("hello", msg_id="m1")
    sink.write_answer_token(" world", msg_id="m1")
    assert sink.streamed_answer is True
    streams = [c for c in app.calls if c[0] == "set_stream"]
    assert streams[-1][1] == ("answer", "hello world")


def test_textual_sink_answer_complete_commits():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    sink._min_stream_interval = 0
    sink.write_answer_token("partial", msg_id="m1")
    sink.write_answer_complete("final answer", msg_id="m1")
    commits = [c for c in app.calls if c[0] == "commit_answer"]
    assert len(commits) == 1
    assert commits[0][1] == ("final answer",)


def test_textual_sink_reasoning_thought_line():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    sink._min_stream_interval = 0
    sink.write_reasoning("think hard")
    sink.close_reasoning()
    assert sink.streamed_reasoning is True
    thoughts = [c for c in app.calls if c[0] == "commit_thought"]
    assert len(thoughts) == 1
    assert thoughts[0][1][1] == "think hard"


def test_textual_sink_streams_reasoning_live():
    """Reasoning tokens should push a live stream preview before commit."""
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    sink._min_stream_interval = 0
    sink.write_reasoning("step ")
    sink.write_reasoning("one")
    streams = [c for c in app.calls if c[0] == "set_stream"]
    assert streams
    assert streams[-1][1] == ("reasoning", "step one")
    assert streams[-1][2].get("elapsed_s", 0) >= 0
    sink.close_reasoning()
    # Full body seals via commit_thought (clear_stream would drop the live row).
    assert not any(c[0] == "clear_stream" for c in app.calls)
    thoughts = [c for c in app.calls if c[0] == "commit_thought"]
    assert len(thoughts) == 1
    assert thoughts[0][1][1] == "step one"


def test_thought_block_live_then_seal():
    # Avoid Textual App: only exercise state transitions.
    block = ThoughtBlock.__new__(ThoughtBlock)
    block.elapsed_s = 0.0
    block.body = "abc"
    block.live = True
    block.collapsed = False
    block._started_at = None
    block._render_block = lambda: None  # type: ignore[method-assign]
    block.update_live(1.5, "abc def")
    assert block.body == "abc def"
    assert block.elapsed_s >= 1.5
    block.seal(2.0, "abc def final")
    assert block.live is False
    assert block.collapsed is True
    assert block.elapsed_s >= 2.0


def test_thought_block_tick_live_advances_clock():
    """Live thought header seconds move even without new tokens."""
    import time

    block = ThoughtBlock.__new__(ThoughtBlock)
    block.elapsed_s = 0.0
    block.body = "thinking"
    block.live = True
    block.collapsed = False
    block._started_at = time.monotonic() - 0.3
    rendered: list[float] = []

    def _capture() -> None:
        rendered.append(block.elapsed_s)

    block._render_block = _capture  # type: ignore[method-assign]
    block.tick_live()
    assert block.elapsed_s >= 0.25
    assert rendered  # header re-rendered


def test_sink_thought_clock_starts_at_activity_start():
    """Thought duration includes wait-for-model, not only token arrival."""
    import time

    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    sink._min_stream_interval = 0
    sink.activity_start("thinking", "waiting for model")
    armed = sink._reasoning_started
    assert armed > 0
    time.sleep(0.05)
    sink.write_reasoning("plan")
    streams = [c for c in app.calls if c[0] == "set_stream"]
    assert streams
    assert streams[-1][2].get("elapsed_s", 0) >= 0.04
    time.sleep(0.05)
    sink.close_reasoning()
    thoughts = [c for c in app.calls if c[0] == "commit_thought"]
    assert len(thoughts) == 1
    assert thoughts[0][1][0] >= 0.08
    assert thoughts[0][1][1] == "plan"
    # Clock resets after seal so the next round starts clean.
    assert sink._reasoning_started == 0.0


def test_answer_block_live_then_seal():
    block = AnswerBlock.__new__(AnswerBlock)
    block.body = "hi"
    block.live = True
    block._render_block = lambda: None  # type: ignore[method-assign]
    block.update_live("hi there")
    assert block.body == "hi there"
    block.seal("hi there final")
    assert block.live is False
    assert block.body == "hi there final"


def test_textual_sink_tool_items_detail():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    sink.activity_start("tools", "working")
    calls = [
        {"name": "read_file", "args": {"file_path": "/a.md"}},
        {"name": "read_file", "args": {"file_path": "/b.md"}},
        {"name": "ls", "args": {"path": "/"}},
    ]
    sink.tool_calls_started(calls, parallel=True)
    items = [
        build_tool_item(c, item_id=f"g1-{i}", index=i) for i, c in enumerate(calls)
    ]
    for it in items:
        sink.tool_item_started(it)
    # Finish with one short preview.
    sink.tool_item_finished(items[0].id, status="ok", preview="# title\nline2", error=False)
    sink.tool_item_finished(items[1].id, status="ok", preview=None, error=False)
    sink.tool_item_finished(items[2].id, status="ok", preview=None, error=False)
    sink.tool_group_closed("g1")
    sink.turn_finished()
    sink.info("finished in 1.0s")
    sink.activity_stop()

    headers = [c for c in app.calls if c[0] == "write_tool_group_header"]
    assert headers
    # Running groups start expanded for realtime visibility.
    assert headers[0][1][1] is False
    items_written = [c for c in app.calls if c[0] == "write_tool_item"]
    assert len(items_written) == 3
    labels = [c[1][0] for c in items_written]
    assert "Read a.md" in labels
    assert "Read b.md" in labels
    # Status flips are pushed immediately; payloads stay out of transcript.
    updates = [c for c in app.calls if c[0] == "update_tool_item"]
    assert len(updates) == 3
    previews = [c for c in app.calls if c[0] == "write_tool_preview"]
    assert not previews
    assert any(c[0] == "close_tool_group" for c in app.calls)


def test_textual_sink_opens_new_group_per_tool_batch():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]

    first = build_tool_item(
        {"name": "read_file", "args": {"file_path": "/a.md"}},
        item_id="g1-0",
        index=0,
    )
    second = build_tool_item(
        {"name": "read_file", "args": {"file_path": "/b.md"}},
        item_id="g2-0",
        index=0,
    )

    sink.tool_calls_started([first], parallel=False)
    sink.tool_item_started(first)
    sink.tool_item_finished(first.id, status="ok")
    sink.tool_group_closed("g1")

    sink.tool_calls_started([second], parallel=False)
    sink.tool_item_started(second)
    sink.tool_item_finished(second.id, status="ok")
    sink.tool_group_closed("g2")

    # Each stream batch is its own visual group (not merged for the whole turn).
    assert len([c for c in app.calls if c[0] == "write_tool_group_header"]) == 2
    assert len([c for c in app.calls if c[0] == "write_tool_item"]) == 2
    assert len([c for c in app.calls if c[0] == "close_tool_group"]) == 2
    sink.turn_finished()
    # Already closed by tool_group_closed; turn end is a no-op.
    assert len([c for c in app.calls if c[0] == "close_tool_group"]) == 2


def test_textual_sink_multi_round_interleave_order():
    """Agent loop: thought → answer → tools → thought → tools → final answer."""
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    sink._min_stream_interval = 0

    # Round 1
    sink.write_reasoning("plan step 1")
    sink.close_reasoning()
    sink.write_answer_complete("I will inspect the file first.", msg_id="m1")
    t1 = build_tool_item(
        {"name": "read_file", "args": {"file_path": "/a.md"}},
        item_id="g1-0",
        index=0,
    )
    sink.tool_calls_started([t1], parallel=False)
    sink.tool_item_started(t1)
    sink.tool_item_finished(t1.id, status="ok")
    sink.tool_group_closed("g1")
    sink.streamed_reasoning = False  # stream resets this after each tool batch

    # Round 2
    sink.write_reasoning("plan step 2")
    sink.close_reasoning()
    t2 = build_tool_item(
        {"name": "grep", "args": {"pattern": "foo"}},
        item_id="g2-0",
        index=0,
    )
    sink.tool_calls_started([t2], parallel=False)
    sink.tool_item_started(t2)
    sink.tool_item_finished(t2.id, status="ok")
    sink.tool_group_closed("g2")
    sink.streamed_reasoning = False

    # Final answer
    sink.write_answer_complete("Here is the conclusion.", msg_id="m2")
    sink.turn_finished()

    kinds = [c[0] for c in app.calls if c[0] in {
        "commit_thought",
        "commit_answer",
        "write_tool_group_header",
        "close_tool_group",
    }]
    assert kinds == [
        "commit_thought",
        "commit_answer",
        "write_tool_group_header",
        "close_tool_group",
        "commit_thought",
        "write_tool_group_header",
        "close_tool_group",
        "commit_answer",
    ]
    thoughts = [c[1][1] for c in app.calls if c[0] == "commit_thought"]
    assert thoughts == ["plan step 1", "plan step 2"]
    answers = [c[1][0] for c in app.calls if c[0] == "commit_answer"]
    assert answers == [
        "I will inspect the file first.",
        "Here is the conclusion.",
    ]


def test_tool_group_block_summary_tracks_items():
    block = ToolGroupBlock("tools")
    block.add_item(
        build_tool_item(
            {"name": "ls", "args": {"path": "/"}},
            item_id="g1-0",
            index=0,
        )
    )
    block.add_item(
        build_tool_item(
            {"name": "read_file", "args": {"file_path": "/a.md"}},
            item_id="g1-1",
            index=1,
        )
    )
    block.add_item(
        build_tool_item(
            {"name": "read_file", "args": {"file_path": "/b.md"}},
            item_id="g1-2",
            index=2,
        )
    )
    # Header must reflect ALL items, not the first write only.
    assert "Listed 1 dir" in block.summary
    assert "Read 2 files" in block.summary
    # Latest partial title must not overwrite the aggregate summary.
    block.set_summary("Read 4 files")
    assert "Listed 1 dir" in block.summary
    assert "Read 2 files" in block.summary
    assert "Read 4 files" not in block.summary


def test_tool_group_block_caps_expanded_rows():
    block = ToolGroupBlock("tools")
    block.collapsed = False
    for i in range(20):
        item = build_tool_item(
            {"name": "read_file", "args": {"file_path": f"/f{i}.md"}},
            item_id=f"g1-{i}",
            index=i,
        )
        item.status = "ok"
        block.add_item(item)
    # Force a render pass after expanding many rows.
    block._sync_summary_from_items(running=False)
    block._render_block()
    assert block.summary == "Read 20 files"
    assert len(block.items) == 20


def test_tool_group_detail_rows_are_indented_under_summary():
    """Detail rows nest under the group header for visual hierarchy."""
    captured: list[object] = []
    block = ToolGroupBlock("tools")
    block.collapsed = False
    original_update = block.update

    def _capture(renderable):  # noqa: ANN001
        captured.append(renderable)
        return original_update(renderable)

    block.update = _capture  # type: ignore[method-assign]
    item = build_tool_item(
        {
            "name": "execute",
            "args": {"command": "git config --list", "intent": "查看当前 git 用户配置"},
        },
        item_id="g1-0",
        index=0,
    )
    item.status = "ok"
    block.add_item(item)

    assert captured
    group = captured[-1]
    lines = [str(r) for r in getattr(group, "renderables", [])]
    assert any(s.startswith(f"{block._HEADER_INDENT}▾  ") for s in lines)
    assert any("✓" in s and "查看当前 git 用户配置" in s for s in lines)
    assert len(block._ITEM_INDENT) > len(block._HEADER_INDENT)
    header = next(s for s in lines if "Ran" in s)
    detail = next(s for s in lines if "查看当前 git 用户配置" in s)
    assert detail.index("✓") > header.index("▾")


def test_close_tool_group_respects_tool_details_expanded_setting():
    """Default keeps finished groups open; config can collapse them."""
    from types import SimpleNamespace

    from synapse.ui.tui import CodingAgentApp

    app = object.__new__(CodingAgentApp)
    app.settings = SimpleNamespace(tool_details_expanded=True)
    app._live_tool_items = []
    app._last_tool_items = []
    app._live_tool_summary = ""
    app._last_tool_summary = ""
    block = ToolGroupBlock("tools")
    app._live_tool_block = block
    item = build_tool_item(
        {"name": "execute", "args": {"command": "echo hi", "intent": "run echo"}},
        item_id="g1-0",
        index=0,
    )
    item.status = "ok"
    block.add_item(item)
    block.collapsed = False

    app.close_tool_group()
    assert block.collapsed is False

    # Re-attach for second pass with config off.
    app.settings.tool_details_expanded = False
    app._live_tool_block = block
    app.close_tool_group()
    assert block.collapsed is True


def test_tool_group_block_places_nested_items_after_owning_task():
    block = ToolGroupBlock("Launched 2 subagents")
    task_a = build_tool_item(
        {"name": "task", "args": {"description": "agent A"}},
        item_id="g1-0",
    )
    task_b = build_tool_item(
        {"name": "task", "args": {"description": "agent B"}},
        item_id="g1-1",
    )
    block.add_item(task_a)
    block.add_item(task_b)

    nested_b = build_tool_item(
        {"name": "grep", "args": {"intent": "search B"}},
        item_id="g1-1-sub-1-grep-b",
        sub=True,
    )
    nested_b.parent_id = task_b.id
    nested_a = build_tool_item(
        {"name": "read_file", "args": {"intent": "read A"}},
        item_id="g1-0-sub-1-read-a",
        sub=True,
    )
    nested_a.parent_id = task_a.id
    block.add_item(nested_b)
    block.add_item(nested_a)

    assert [item.id for item in block.items] == [
        task_a.id,
        nested_a.id,
        task_b.id,
        nested_b.id,
    ]


def test_timeline_blocks_toggle_in_place():
    thought = ThoughtBlock(1.2, "first line\nsecond line")
    assert thought.collapsed is True
    thought.toggle()
    assert thought.collapsed is False
    thought.toggle()
    assert thought.collapsed is True

    # Live groups start expanded for realtime visibility.
    tool = ToolGroupBlock("Read 1 file")
    assert tool.collapsed is False
    item = build_tool_item(
        {"name": "read_file", "args": {"file_path": "/a.md"}},
        item_id="g1-0",
        index=0,
    )
    tool.add_item(item)
    assert len(tool.items) == 1
    tool.toggle()
    assert tool.collapsed is True
    tool.toggle()
    assert tool.collapsed is False


def test_tool_group_block_hover_class_marks_group():
    """Pointer hover adds -hover so CSS can paint a faint left edge."""
    from types import SimpleNamespace

    tool = ToolGroupBlock("Read 1 file")
    assert not tool.has_class("-hover")
    tool.on_enter(SimpleNamespace(stop=lambda: None))
    assert tool.has_class("-hover")
    tool.on_leave(SimpleNamespace(stop=lambda: None))
    assert not tool.has_class("-hover")


def test_stream_tail_preview_keeps_only_recent_lines():
    body = "\n".join(f"line-{i}" for i in range(80))
    preview = stream_tail_preview(body, max_lines=10, max_chars=10_000)
    assert preview.startswith("…\n")
    assert "line-79" in preview
    assert "line-0" not in preview
    assert preview.count("\n") <= 11


def test_stream_tail_preview_hard_char_cap():
    body = "x" * 8000
    preview = stream_tail_preview(body, max_lines=100, max_chars=500)
    assert preview.startswith("…")
    assert len(preview) < 600


def test_textual_sink_live_stream_uses_tail_preview():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    sink._min_stream_interval = 0
    # Build a body larger than the live tail window.
    chunk = "alpha\n" * 40
    sink.write_answer_token(chunk, msg_id="m-big")
    streams = [c for c in app.calls if c[0] == "set_stream"]
    assert streams
    kind, body = streams[-1][1]
    assert kind == "answer"
    assert body.startswith("…")
    assert len(body) < len(chunk)
    # Full answer is still committed later.
    sink.write_answer_complete(chunk, msg_id="m-big")
    commits = [c for c in app.calls if c[0] == "commit_answer"]
    assert commits[-1][1] == (chunk.strip(),)


def test_chrome_helpers_match_grok_style_labels():
    from synapse.ui.tui import format_mcp_status_label, format_usage_label

    assert format_token_count(14_000) == "14K"
    assert format_token_count(392_832) == "393K"
    assert short_model_name("openai:gpt-4.1") == "gpt-4.1"
    assert short_workspace_label(r"F:\project\agent\autoagents\py-agent") == (
        "autoagents/py-agent"
    )
    raw = (
        "finished in 38.0s | tools=6 | token_stream=on | "
        "tokens: 394106 (in=392832 out=1274)"
    )
    assert soften_turn_footer(raw) == "Worked for 38.0s."
    assert format_usage_label(
        input_tokens=12_000, cache_tokens=3_000, output_tokens=1_200
    ) == "12K/3K/1.2K"
    assert format_mcp_status_label(enabled=False) == "mcp off"
    assert format_mcp_status_label(
        enabled=True, servers=["a", "b"], tools=["t1", "t2", "t3"]
    ) == "mcp on"
    assert format_mcp_status_label(
        enabled=True, servers=[], tools=[], warnings=["x"]
    ) == "mcp err"


def test_textual_sink_ignores_nested_and_empty_legacy():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    # Nested subagent noise must not create tool groups.
    sink.tool_result("read_file", "ok", sub=True)
    assert not [c for c in app.calls if c[0] == "write_tool_group_header"]
    # Unmatched empty legacy must not create "0 tools".
    sink.tool_result("read_file", "ok", sub=False)
    assert not [c for c in app.calls if c[0] == "write_tool_group_header"]


def test_textual_sink_keeps_running_task_group_open():
    app = _FakeApp()
    sink = TextualStreamSink(app)  # type: ignore[arg-type]
    item = build_tool_item(
        {"name": "task", "args": {"description": "explore"}},
        item_id="g1-0",
        index=0,
    )
    sink.tool_calls_started([item], parallel=False)
    sink.tool_item_started(item)
    # Intermediate thought/answer while task still running must not close group.
    sink.write_reasoning("waiting for subagent")
    sink.close_reasoning()
    sink.write_answer_complete("subagent still working…", msg_id="m-mid")
    assert len([c for c in app.calls if c[0] == "close_tool_group"]) == 0
    sink.tool_item_finished(item.id, status="ok")
    sink.tool_group_closed("g1")
    assert len([c for c in app.calls if c[0] == "close_tool_group"]) == 1
    headers = [c[1][0] for c in app.calls if c[0] == "write_tool_group_header"]
    assert headers
    assert "subagent" in headers[0].lower() or "Launched" in headers[0]


def test_format_answer_divider_centered_with_spacing():
    rows = format_answer_divider(80)
    assert rows[0] == ""
    assert rows[2] == ""
    line = rows[1]
    assert "◇" in line
    assert "─" in line
    # Diamond should be near the horizontal center of the panel width.
    idx = line.index("◇")
    center = len(line) // 2
    assert abs(idx - center) <= 2
    # Leading pads present (rule is shorter than panel and left-padded).
    assert line.lstrip(" ").startswith("─")
    # ~80% rule → roughly 10% pad each side on width=80.
    lead = len(line) - len(line.lstrip(" "))
    assert 4 <= lead <= 12


def test_format_turn_rail_preview_truncates_and_normalizes():
    assert format_turn_rail_preview("") == "(empty)"
    assert format_turn_rail_preview("  hi\nthere  ") == "hi there"
    long = "a" * 80
    out = format_turn_rail_preview(long, max_len=20)
    assert out.endswith("…")
    assert len(out) == 20

