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
    # Textual's ``Widget._render`` caches the visual in ``_layout_cache``;
    # clear it so a re-rendered block (e.g. after a status transition) is
    # never read from the stale cache.
    cache = getattr(block, "_layout_cache", None)
    if cache is not None:
        cache.clear()
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


def _with_status(block: ToolGroupBlock, status: str) -> str:
    block.set_subagent_phase("t1", status, render=False)
    block._render_block()
    return _render_text(block)


def test_status_sits_between_intent_and_suffix() -> None:
    for status, label in (
        ("calling_tools", "calling tools"),
        ("reasoning", "reasoning"),
        ("answering", "answering"),
    ):
        block = ToolGroupBlock("Running 1 subagent")
        block.add_item(_task_item("t1", label="审查修复"))
        text = _with_status(block, status)
        assert text.index("审查修复") < text.index(label) < text.index(
            "[reviewer · gpt-5.2 · high]"
        ), status


def test_status_colors_differ_and_are_not_muted() -> None:

    styles: dict[str, str] = {}
    for status in ("calling_tools", "reasoning", "answering"):
        block = ToolGroupBlock("Running 1 subagent")
        block.add_item(_task_item("t1"))
        block.set_subagent_phase("t1", status, render=False)
        block._render_block()
        segments: list[Segment] = [
            seg
            for line in _render_segments(block)
            for seg in line
            if seg.text.strip() in {"calling tools", "reasoning", "answering"}
        ]
        assert len(segments) == 1, status
        style = str(segments[0].style or "")
        assert style, status
        assert not any(
            marker in style for marker in ("muted", "dim", "italic")
        ), f"{status}: {style}"
        styles[status] = style
    assert len({*styles.values()}) == 3, styles


def test_status_hidden_when_done_or_error() -> None:
    done = ToolGroupBlock("Launched 1 subagent")
    done.add_item(_task_item("t1", status="done"))
    assert "reasoning" not in _with_status(done, "reasoning")

    failed = ToolGroupBlock("Launched 1 subagent")
    failed.add_item(_task_item("t1", status="error", error=True))
    assert "answering" not in _with_status(failed, "answering")


def test_status_cleared_on_update_to_finished() -> None:
    block = ToolGroupBlock("Running 1 subagent")
    block.add_item(_task_item("t1"))
    block.set_subagent_phase("t1", "answering", render=False)
    block._render_block()
    assert "answering" in _render_text(block)
    block.update_item("t1", status="ok", render=False)
    block._render_block()
    assert "answering" not in _render_text(block)


def test_status_in_selectable_text() -> None:
    block = ToolGroupBlock("Running 1 subagent")
    block.add_item(_task_item("t1"))
    block.set_subagent_phase("t1", "calling_tools", render=False)
    assert (
        "审查修复  calling tools  [reviewer · gpt-5.2 · high] [running]"
        in block.selectable_text()
    )


def test_sub_window_scrolls_forward_as_calls_arrive() -> None:
    """Oldest sub-calls fold away as new ones arrive (regression)."""
    from synapse.runtime.streaming.tool_model import ToolItem

    block = ToolGroupBlock("Running 1 subagent")
    block.add_item(_task_item("t1"))
    for i in range(1, 6):
        block.add_item(
            ToolItem(
                id=f"s{i}",
                name="search_files",
                category="search",
                label=f"Searched pattern {i}",
                status="done",
                error=False,
                sub=True,
                parent_id="t1",
            ),
            render=False,
        )

    def visible_labels() -> list[str]:
        text = _render_text(block)
        return [f"Searched pattern {i}" for i in range(1, 6) if f"Searched pattern {i}" in text]

    block._render_block()
    assert visible_labels() == ["Searched pattern 3", "Searched pattern 4", "Searched pattern 5"]
    assert "… and 2 earlier" in _render_text(block)
    # Folding is display-only: all five calls stay in the model.
    assert len([i for i in block.items if i.sub]) == 5


def test_sub_window_rolls_past_errored_calls() -> None:
    """An old errored sub-call is replaced once newer calls arrive."""
    from synapse.runtime.streaming.tool_model import ToolItem

    block = ToolGroupBlock("Running 1 subagent")
    block.add_item(_task_item("t1"))
    for i in range(1, 5):
        block.add_item(
            ToolItem(
                id=f"s{i}",
                name="search_files",
                category="search",
                label=f"Searched pattern {i}",
                status="error",
                error=True,
                sub=True,
                parent_id="t1",
            ),
            render=False,
        )
    block._render_block()
    text = _render_text(block)
    assert "Searched pattern 1" not in text
    assert "Searched pattern 2" in text and "Searched pattern 4" in text
