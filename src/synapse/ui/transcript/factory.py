"""Factories that convert persisted transcript events into Textual blocks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from textual.containers import VerticalScroll

import synapse.ui.tui_styles as _styles
from synapse.ui.tool_blocks import ToolGroupBlock
from synapse.ui.transcript_blocks import AnswerBlock, ThoughtBlock
from synapse.ui.tui_styles import _MARK_THOUGHT, _MARKDOWN_MAX_CHARS
from synapse.ui.user_turn_block import UserTurnBlock


def build_restored_tool_group(
    app: Any,
    tool_calls: list[dict],
    tool_results: list[dict],
) -> tuple[ToolGroupBlock | None, bool]:
    """Build one unmounted historical tool batch and its answer-divider flag."""
    from synapse.ui.timeline import (
        build_tool_item,
        extract_todos,
        format_todos_preview,
        is_todo_tool,
        summarize_items,
        truncate_preview,
    )

    if not tool_calls and not tool_results:
        return None, False
    items: list[Any] = []
    result_by_id = {
        str(result.get("id") or ""): result
        for result in (tool_results or [])
        if isinstance(result, dict)
    }
    result_by_name: dict[str, list[dict]] = {}
    for result in tool_results or []:
        if isinstance(result, dict):
            result_by_name.setdefault(str(result.get("name") or ""), []).append(result)

    for index, call in enumerate(tool_calls or []):
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or f"hist-{index}")
        item = build_tool_item(call, item_id=call_id, index=index)
        result = result_by_id.get(call_id)
        if result is None:
            bucket = result_by_name.get(str(call.get("name") or ""), [])
            if bucket:
                result = bucket.pop(0)
        if result is not None:
            content = str(result.get("content") or "")
            item.status = "error" if str(result.get("status") or "ok") == "error" else "done"
            item.error = item.status == "error"
            if is_todo_tool(item.name):
                args = call.get("args") if isinstance(call, dict) else {}
                item.preview = format_todos_preview(extract_todos(args)) or (
                    truncate_preview(content) if content else None
                )
            else:
                item.preview = truncate_preview(content) if content else None
        else:
            item.status = "done"
        items.append(item)

    used_ids = {item.id for item in items}
    for result in tool_results or []:
        if not isinstance(result, dict):
            continue
        result_id = str(result.get("id") or "")
        if result_id and result_id in used_ids:
            continue
        fake_call = {
            "name": result.get("name") or "tool",
            "args": {},
            "id": result_id or f"orphan-{len(items)}",
        }
        item = build_tool_item(fake_call, item_id=str(fake_call["id"]), index=len(items))
        content = str(result.get("content") or "")
        item.status = "error" if str(result.get("status") or "ok") == "error" else "done"
        item.error = item.status == "error"
        item.preview = truncate_preview(content) if content else None
        items.append(item)

    summary = summarize_items(items, running=False)
    if not items or (summary or "").strip() in {"", "0 tools", "tools", "Running 0 tools"}:
        return None, False
    block = ToolGroupBlock(summary)
    block.collapsed = True
    for item in items:
        block.add_item(item)
    block._sync_summary_from_items(running=False)
    has_todo = any(
        (item.name or "").lower() in {"write_todos", "todo_write", "todos"}
        or str(item.label or "").startswith("Todos ")
        for item in items
    )
    block.set_collapsed(not (has_todo or app._transcript._tool_details_expanded()))
    block._render_block()
    return block, True


def build_answer_divider(app: Any) -> Any:
    """Create an unmounted answer divider sized for the current transcript."""
    from synapse.ui.answer_divider import AnswerDivider

    width = 0
    try:
        log = app.query_one("#log", VerticalScroll)
        width = int(getattr(log.size, "width", 0) or 0)
    except Exception:  # noqa: BLE001 - widget may not yet be mounted
        width = 0
    if width <= 0:
        width = int(getattr(app.size, "width", 0) or 0)
    return AnswerDivider(max(28, (width or 56) - 2), muted_color=lambda: _styles._C_MUTED)


def build_restored_blocks(app: Any, events: list[Any]) -> list[Any]:
    """Build unmounted widgets and synchronize transcript bookkeeping."""
    state = app._transcript.state
    blocks: list[Any] = []
    pending_divider = False
    turn_count = len(state.user_turns)
    for event in events:
        kind = event.kind
        if kind == "user":
            pending_divider = False
            turn_count += 1
            block = UserTurnBlock(
                event.text or "",
                stamp=datetime.now().strftime("%I:%M %p").lstrip("0"),
                turn_index=turn_count,
                image_count=len(getattr(event, "images", None) or []),
            )
            state.user_turns.append(block)
            blocks.append(block)
        elif kind == "thought":
            block = ThoughtBlock(
                0.0,
                event.text,
                expand_on_seal=bool(getattr(app.settings, "expand_thinking", False)),
                dim_color=lambda: _styles._C_DIM,
                thought_mark=_MARK_THOUGHT,
            )
            state.thought_blocks.append(block)
            blocks.append(block)
        elif kind == "tools":
            group, divider = build_restored_tool_group(app, event.tool_calls, event.tool_results)
            if group is not None:
                state.tool_blocks.append(group)
                blocks.append(group)
                pending_divider = divider
        elif kind == "answer":
            try:
                from synapse.runtime.context_compact import is_context_compact_text

                if is_context_compact_text(event.text):
                    continue
            except Exception:  # noqa: BLE001 - preserve legacy transcript text on fallback
                pass
            if pending_divider:
                blocks.append(build_answer_divider(app))
                pending_divider = False
            blocks.append(
                AnswerBlock(
                    event.text,
                    live=False,
                    fg_color=lambda: _styles._C_FG,
                    markdown_max_chars=_MARKDOWN_MAX_CHARS,
                )
            )
    app._transcript._refresh_turn_rail()
    return blocks
