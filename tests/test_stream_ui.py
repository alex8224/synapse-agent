"""Unit tests for streaming UI helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from synapse.ui.stream import (
    StreamResult,
    _ActivityLine,
    _extract_reasoning,
    _extract_usage,
    _is_ai_message,
    _is_tool_message,
    _normalize_content,
    _reasoning_token_count,
    _StreamPrinter,
    extract_last_ai_text,
    render_math_in_text,
)


def test_normalize_content_list_blocks():
    text = _normalize_content(
        [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]
    )
    assert text == "hello world"


def test_normalize_skips_reasoning_blocks_in_content():
    text = _normalize_content(
        [
            {"type": "reasoning", "text": "secret thought"},
            {"type": "text", "text": "visible"},
        ]
    )
    assert text == "visible"


def test_extract_reasoning_from_additional_kwargs():
    class Msg:
        content = "answer"
        additional_kwargs = {"reasoning_content": "step by step"}
        response_metadata = {}

    assert _extract_reasoning(Msg()) == "step by step"


def test_is_tool_message_accepts_langchain_type_tool():
    class Msg:
        type = "tool"
        content = "ok"
        name = "ls"

    assert _is_tool_message(Msg()) is True


def test_is_tool_message_accepts_class_name():
    class ToolMessage:
        content = "ok"

    assert _is_tool_message(ToolMessage()) is True


def test_is_ai_message():
    class Msg:
        type = "ai"

    assert _is_ai_message(Msg()) is True


def test_reasoning_token_count_from_usage_metadata():
    class Msg:
        usage_metadata = {"output_token_details": {"reasoning": 16}}

    assert _reasoning_token_count(Msg()) == 16


def test_extract_usage_includes_cache_read_tokens():
    class Msg:
        usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 40},
        }
        response_metadata = {}

    u = _extract_usage(Msg())
    assert u["input_tokens"] == 100
    assert u["output_tokens"] == 20
    assert u["cache_tokens"] == 40


def test_stream_result_defaults():
    result = StreamResult(final_text="pong", streamed_answer=True, tool_calls=2)
    assert result.streamed_answer is True
    assert result.tool_calls == 2
    assert result.final_text == "pong"
    assert result.cache_tokens == 0


def test_extract_last_ai_text_from_messages():
    class Msg:
        type = "ai"
        content = "final answer"

    text = extract_last_ai_text({"messages": [Msg()]})
    assert text == "final answer"


def test_extract_last_ai_text_ignores_middleware_jump_map():
    from synapse.ui.stream import _looks_like_middleware_update

    junk = {
        "SkillsMiddleware.before_agent": None,
        "PatchToolCallsMiddleware.before_agent": None,
        "MemoryMiddleware.before_agent": None,
        "inject_steer_queue.before_model": None,
    }
    assert _looks_like_middleware_update(junk) is True
    assert extract_last_ai_text(junk) == ""
    assert extract_last_ai_text({}) == ""
    assert extract_last_ai_text({"messages": []}) == ""
    assert extract_last_ai_text("not a dict") == ""


def test_checkpointer_supports_async_detects_sqlite_saver():
    from synapse.ui.stream import (
        _is_sync_only_checkpointer_error,
        checkpointer_supports_async,
    )

    class SqliteSaver:
        pass

    class Memoryish:
        def aget_tuple(self):  # noqa: ANN001
            return None

    assert checkpointer_supports_async(None) is True
    assert checkpointer_supports_async(SqliteSaver()) is False
    assert checkpointer_supports_async(Memoryish()) is True
    err = RuntimeError(
        "The SqliteSaver does not support async methods. "
        "Consider using AsyncSqliteSaver instead."
    )
    assert _is_sync_only_checkpointer_error(err) is True
    assert _is_sync_only_checkpointer_error(RuntimeError("rate limit")) is False


def test_write_reasoning_buffers_without_live_render():
    activity = MagicMock(spec=_ActivityLine)
    printer = _StreamPrinter(activity)
    with patch("synapse.ui.stream.console.print") as mock_print:
        printer.write_reasoning("## 分析\n")
        printer.write_reasoning("- 步骤 1")
        # No permanent print while still streaming.
        mock_print.assert_not_called()
    assert printer.streamed_reasoning is True
    assert printer.reasoning_open is True
    assert "".join(printer._open_reasoning_parts) == "## 分析\n- 步骤 1"


def test_close_reasoning_commits_once():
    activity = MagicMock(spec=_ActivityLine)
    printer = _StreamPrinter(activity)
    printer.reasoning_open = True
    printer._open_reasoning_parts = ["## done"]
    with patch("synapse.ui.stream.console.print") as mock_print:
        printer.close_reasoning()
        printer.close_reasoning()
        # blank line + reasoning group, only on first close
        assert mock_print.call_count == 2
    assert printer.reasoning_open is False
    assert printer._open_reasoning_parts == []


def test_write_answer_token_buffers_only():
    activity = MagicMock(spec=_ActivityLine)
    printer = _StreamPrinter(activity)
    with patch("synapse.ui.stream.console.print") as mock_print:
        printer.write_answer_token("hello", msg_id="m1")
        printer.write_answer_token(" world", msg_id="m1")
        mock_print.assert_not_called()
    assert printer.streamed_answer is True
    assert "".join(printer._open_answer_parts) == "hello world"


def test_answer_complete_prints_once_and_dedupes():
    activity = MagicMock(spec=_ActivityLine)
    printer = _StreamPrinter(activity)

    with patch("synapse.ui.stream.console.print") as mock_print:
        printer.write_answer_complete("## 结论\n\n完成", msg_id="m1")
        printer.write_answer_complete("## 结论\n\n完成", msg_id="m1")
        printer.write_answer_complete("## 结论\n完成", msg_id="m2")
        # first commit: blank + group; later commits suppressed
        assert mock_print.call_count == 2
        assert printer.streamed_answer is True
        assert "m1" in printer._markdown_rendered_ids
        assert printer._norm_text("## 结论\n\n完成") in printer._printed_complete_texts
        assert printer.answer_buf == ["## 结论\n\n完成"]


def test_write_answer_token_ignored_after_complete():
    activity = MagicMock(spec=_ActivityLine)
    printer = _StreamPrinter(activity)
    with patch("synapse.ui.stream.console.print"):
        printer.write_answer_complete("final text", msg_id="m1")
    printer.write_answer_token("late", msg_id="m1")
    assert printer._open_answer_parts == []


def test_render_math_preserves_source_on_texicode_error_string():
    error = "\n```\nTeXicode: parsing error: unexpected token\n```\n"
    with patch("texicode.pipeline.render_tex", return_value=error):
        assert render_math_in_text("结论：$x +$") == "结论：$x +$"


def test_render_math_preserves_source_on_texicode_exception_or_empty_result():
    with patch("texicode.pipeline.render_tex", side_effect=ValueError("bad formula")):
        assert render_math_in_text("$$broken$$") == "$$broken$$"
    with patch("texicode.pipeline.render_tex", return_value=""):
        assert render_math_in_text(r"\(broken\)") == r"\(broken\)"


def test_render_math_uses_valid_texicode_result():
    with patch("texicode.pipeline.render_tex", return_value="x + y"):
        assert render_math_in_text("value: $x+y$") == "value: x + y"


def test_render_markdown_tables_use_full_rounded_borders():
    """Markdown tables should render full ROUNDED grid, not SIMPLE header line."""
    from rich.console import Console

    from synapse.ui.stream import _FullBorderMarkdown, _FullTableElement, render_markdown

    md = render_markdown(
        "| file | change |\n| --- | --- |\n| a.py | hello |\n| b.py | world |"
    )
    assert isinstance(md, _FullBorderMarkdown)
    assert md.elements.get("table_open") is _FullTableElement

    c = Console(width=40, force_terminal=True, highlight=False, emoji=False, record=True)
    c.print(md)
    out = c.export_text()
    # outer corners + verticals + row/header junctions (show_lines)
    assert "┌" in out and "└" in out and "│" in out
    assert "├" in out  # ├ between rows/header
    assert "┼" in out  # ┼ grid cross when show_lines=True
    assert "file" in out and "a.py" in out and "b.py" in out

