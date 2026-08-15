"""ToolGroupBlock rendering tests for subagent metadata suffixes."""

from __future__ import annotations

from rich.console import Console
from rich.segment import Segment

from synapse.runtime.streaming.tool_model import ToolItem
from synapse.ui.tool_blocks import ToolGroupBlock, format_subagent_suffix


def _task_item(
    item_id: str,
    *,
    label: str = "审查修复",
    status: str = "running",
    error: bool = False,
    subagent_name: str | None = "reviewer",
    model: str | None = "gpt-5.2",
    effort: str | None = "high",
    model_inherited: bool = False,
    effort_inherited: bool = False,
) -> ToolItem:
    return ToolItem(
        id=item_id,
        name="task",
        category="task",
        label=label,
        status=status,
        error=error,
        sub=False,
        subagent_name=subagent_name,
        subagent_model=model,
        subagent_reasoning_effort=effort,
        subagent_model_inherited=model_inherited,
        subagent_reasoning_inherited=effort_inherited,
    )


def _renderable(block: ToolGroupBlock):
    """Return the Rich Group the block painted (bypasses Textual mounting)."""
    visual = block._render()
    return getattr(visual, "_renderable", visual)


def _render_text(block: ToolGroupBlock) -> str:
    console = Console(width=160, force_terminal=True)
    lines = console.render_lines(_renderable(block))
    return "\n".join("".join(seg.text for seg in line) for line in lines)


def _render_segments(block: ToolGroupBlock) -> list[list[Segment]]:
    console = Console(width=160, force_terminal=True)
    return console.render_lines(_renderable(block))


def _segment_text(segments: list[list[Segment]]) -> str:
    return "".join(seg.text for seg in segments[0]) if segments else ""


def test_format_subagent_suffix_full() -> None:
    item = _task_item("t1")
    suffix = format_subagent_suffix(item)
    assert suffix == "[reviewer · gpt-5.2 · high]"


def test_format_subagent_suffix_inherit_markers() -> None:
    item = _task_item("t1", model_inherited=True, effort_inherited=True)
    assert format_subagent_suffix(item) == "[reviewer · gpt-5.2 (inherit) · high (inherit)]"


def test_format_subagent_suffix_name_only() -> None:
    item = _task_item("t1", model=None, effort=None)
    assert format_subagent_suffix(item) == "[reviewer]"


def test_format_subagent_suffix_no_name_or_non_task() -> None:
    item = ToolItem(id="t1", name="read_file", category="read", label="Read a")
    assert format_subagent_suffix(item) == ""
    nested = _task_item("t1")
    nested.sub = True
    assert format_subagent_suffix(nested) == ""


def test_running_task_row_shows_suffix() -> None:
    block = ToolGroupBlock("Running 1 subagent")
    block.add_item(_task_item("t1"))
    text = _render_text(block)
    assert "审查修复" in text
    assert "[reviewer · gpt-5.2 · high]" in text
    assert "○" in text


def test_done_and_error_rows_keep_suffix() -> None:
    done = ToolGroupBlock("Launched 1 subagent")
    done.add_item(_task_item("t1", status="done"))
    done_text = _render_text(done)
    assert "[reviewer · gpt-5.2 · high]" in done_text
    assert "✓" in done_text

    failed = ToolGroupBlock("Launched 1 subagent")
    failed.add_item(_task_item("t1", status="error", error=True))
    failed_text = _render_text(failed)
    assert "[reviewer · gpt-5.2 · high]" in failed_text
    assert "✗" in failed_text


def test_suffix_is_muted_style() -> None:
    block = ToolGroupBlock("Running 1 subagent")
    block.add_item(_task_item("t1"))
    segments = _render_segments(block)
    found = False
    for seg in segments[1]:  # first row after the header
        if "reviewer" in seg.text:
            style = str(seg.style or "")
            # Muted renders as the theme's dim color (e.g. #5f6368) or a
            # dimmed ANSI name depending on the active theme; other tests may
            # have switched themes, so accept any dim-ish rendering.
            assert any(
                marker in style
                for marker in ("5f6368", "muted", "dim", "bright_black", "black")
            ), style
            found = True
    assert found


def test_selectable_text_matches_screen_text() -> None:
    block = ToolGroupBlock("Running 1 subagent")
    block.add_item(_task_item("t1"))
    selectable = block.selectable_text()
    assert "审查修复  [reviewer · gpt-5.2 · high] [running]" in selectable


def test_non_task_items_unchanged() -> None:
    block = ToolGroupBlock("Read 1 file")
    block.add_item(
        ToolItem(id="r1", name="read_file", category="read", label="Read README.md")
    )
    text = _render_text(block)
    assert "Read README.md" in text
    assert "[" not in text.split("Read README.md")[1].split("\n")[0]


def test_add_item_same_id_preserves_metadata() -> None:
    """A same-id replacement (streaming args update) must not drop metadata."""
    block = ToolGroupBlock("Running 1 subagent")
    first = _task_item("t1", label="审查")
    block.add_item(first)
    updated = _task_item("t1", label="审查修复")
    block.add_item(updated)
    text = _render_text(block)
    assert "审查修复" in text
    assert "[reviewer · gpt-5.2 · high]" in text


def test_folding_counts_unaffected_by_suffix() -> None:
    block = ToolGroupBlock("Running 1 subagent")
    for i in range(16):
        block.add_item(_task_item(f"t{i}", label=f"task {i}"))
    text = _render_text(block)
    assert "… and" in text
