"""Live transcript rendering: stream blocks, tool groups, turn rail, copy.

Owns the mounted-transcript state and DOM writes that used to live directly on
``CodingAgentApp``. The Textual host keeps Textual event wiring and action
bindings and forwards calls here, so this controller can be tested against a
fake host surface without a running Textual app.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.events import MouseUp
from textual.widgets import Static

import synapse.ui.tui_styles as _styles
from synapse.ui.answer_divider import AnswerDivider
from synapse.ui.formatters import soften_turn_footer
from synapse.ui.timeline import ToolItem
from synapse.ui.tool_blocks import ToolGroupBlock
from synapse.ui.transcript.state import TranscriptState
from synapse.ui.transcript_blocks import AnswerBlock, ThoughtBlock, _MarkdownBlock
from synapse.ui.tui_styles import _MARK_THOUGHT, _MARKDOWN_MAX_CHARS
from synapse.ui.turn_rail import format_turn_rail_preview
from synapse.ui.turn_rail_widgets import TurnRail
from synapse.ui.user_turn_block import UserTurnBlock
from synapse.ui.welcome import WelcomeView


def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


class TranscriptController:
    """Write path for the transcript: blocks, stream, tools, rail, copy."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.state = TranscriptState()
        self._tool_batch_mode = False

    # -- TextualStreamHost ------------------------------------------------

    @property
    def transcript_generation(self) -> int:
        """Generation used to reject callbacks from an older session stream."""
        return int(getattr(self._app, "transcript_generation", 0))

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        """Forward thread dispatch without exposing the full App to the sink."""
        return self._app.call_from_thread(callback, *args, **kwargs)

    def call_after_refresh(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        """Queue a UI callback without blocking the producing runtime thread."""
        return self._app.call_after_refresh(callback, *args, **kwargs)

    def apply_turn_usage(self, **kwargs: Any) -> None:
        self._app.apply_turn_usage(**kwargs)

    def set_activity(
        self, phase: str, detail: str = "", reset_timer: bool = False
    ) -> None:
        self._app.set_activity(phase, detail, reset_timer)

    def _refresh_git_chrome(self) -> None:
        self._app._refresh_git_chrome()

    def begin_tool_batch(self) -> None:
        """Accumulate live tool-block writes without rendering.

        The replay path calls this before rendering a bounded batch so that
        every ``ToolGroupBlock`` mutation inside the batch updates data only;
        ``end_tool_batch`` then flushes the block once instead of rebuilding it
        per tool event.
        """
        self._tool_batch_mode = True

    def end_tool_batch(self) -> None:
        """Flush the accumulated tool block once and leave batch mode."""
        self._tool_batch_mode = False
        block = self.state.live_tool_block
        if block is not None:
            block.flush()

    # -- stream ----------------------------------------------------------

    def set_stream(self, kind: str, body: str, elapsed_s: float = 0.0) -> None:
        """Mount or update a live block at the end of #log (in place)."""
        text = body or ""
        kind = (kind or "answer").strip() or "answer"
        st = self.state
        if st.live_stream_kind and st.live_stream_kind != kind:
            # Never abandon a live block in #log. The sink normally seals an
            # answer before switching to reasoning; this defensive cleanup keeps
            # direct/late stream transitions from leaving a duplicate preview.
            self.clear_stream()
        if not text.strip() and st.live_stream_block is None:
            return
        if kind == "reasoning":
            block = st.live_stream_block
            if not isinstance(block, ThoughtBlock) or st.live_stream_kind != "reasoning":
                block = ThoughtBlock(
                    float(elapsed_s or 0.0),
                    text,
                    live=True,
                    expand_on_seal=bool(
                        getattr(self._app.settings, "expand_thinking", False)
                    ),
                    dim_color=lambda: _styles._C_DIM,
                    thought_mark=_MARK_THOUGHT,
                )
                st.live_stream_block = block
                st.live_stream_kind = "reasoning"
                st.thought_blocks.append(block)
                self._mount_block(block)
            else:
                block.update_live(float(elapsed_s or 0.0), text)
                self._follow_timeline_if_needed()
            st.in_tool_rail = False
            return
        block = st.live_stream_block
        if not isinstance(block, AnswerBlock) or st.live_stream_kind != "answer":
            if st.pending_answer_divider:
                self._mount_answer_divider()
                st.pending_answer_divider = False
            block = AnswerBlock(
                text,
                live=True,
                fg_color=lambda: _styles._C_FG,
                markdown_max_chars=_MARKDOWN_MAX_CHARS,
            )
            st.live_stream_block = block
            st.live_stream_kind = "answer"
            self._mount_block(block)
        else:
            block.update_live(text)
            self._follow_timeline_if_needed()

    def clear_stream(self) -> None:
        """Drop unsealed live stream row; legacy #stream stays empty."""
        try:
            stream = self._app.query_one("#stream", Static)
            stream.update("")
            stream.remove_class("active")
        except Exception:  # noqa: BLE001
            pass
        st = self.state
        block = st.live_stream_block
        if block is not None and getattr(block, "live", False):
            try:
                if block.is_attached:
                    block.remove()
            except Exception:  # noqa: BLE001
                pass
            if isinstance(block, ThoughtBlock) and block in st.thought_blocks:
                st.thought_blocks.remove(block)
        st.live_stream_block = None
        st.live_stream_kind = None

    def _follow_timeline_if_needed(self) -> None:
        try:
            timeline = self._app.query_one("#log", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        follow = timeline.max_scroll_y <= 0 or timeline.scroll_y >= timeline.max_scroll_y - 1
        if follow:
            self._app.call_after_refresh(self._scroll_timeline)

    # -- transcript writers ----------------------------------------------

    def _show_welcome(self) -> None:
        try:
            self._app.query_one("#main", Vertical).add_class("welcome")
            self._app.query_one("#welcome", WelcomeView).start_animation()
        except Exception:  # noqa: BLE001
            pass

    def _dismiss_welcome(self) -> None:
        try:
            self._app.query_one("#main", Vertical).remove_class("welcome")
            self._app.query_one("#welcome", WelcomeView).stop_animation()
        except Exception:  # noqa: BLE001
            pass

    def _mount_block(self, block: Any, *, dismiss_welcome: bool = True) -> None:
        if dismiss_welcome:
            self._dismiss_welcome()
        timeline = self._app.query_one("#log", VerticalScroll)
        follow = timeline.max_scroll_y <= 0 or timeline.scroll_y >= timeline.max_scroll_y - 1
        timeline.mount(block)
        self.state.current_turn_blocks.append(block)
        if follow:
            self._app.call_after_refresh(self._scroll_timeline)

    def _scroll_timeline(self) -> None:
        self._app.query_one("#log", VerticalScroll).scroll_end(animate=False)

    def _mount_markdown_block(self, text: str) -> None:
        """Render a Markdown string into the transcript, dismissing welcome."""
        if not isinstance(text, str) or not text.strip():
            return
        self._dismiss_welcome()
        if len(text) > _MARKDOWN_MAX_CHARS:
            renderable: Any = Text(text, style=_styles._C_FG)
            block = Static(renderable)
        else:
            block = _MarkdownBlock(text)
        self._mount_block(block, dismiss_welcome=False)

    def append_user(
        self,
        text: str,
        images: list[Any] | None = None,
        *,
        full_text: str | None = None,
    ) -> None:
        st = self.state
        if st.current_turn_blocks:
            st.live_turn_pages.append(st.current_turn_blocks)
            st.current_turn_blocks = []
            self._trim_live_turn_pages()
        imgs = list(images or [])
        history_state = getattr(getattr(self._app, "_history", None), "state", None)
        total_turns = int(getattr(history_state, "total_turns", 0) or 0)
        turn_index = max(total_turns + 1, len(st.user_turns) + 1)
        image_widgets = self._make_image_widgets(imgs)
        block = UserTurnBlock(
            text or "",
            stamp=_stamp(),
            turn_index=turn_index,
            image_count=len(imgs),
            image_widgets=image_widgets,
            full_text=full_text,
        )
        st.user_turns.append(block)
        self._mount_block(block)
        for widget in image_widgets:
            widget.display = False  # hidden until the block is expanded
            try:
                timeline = self._app.query_one("#log", VerticalScroll)
                timeline.mount(widget)
            except Exception:  # noqa: BLE001 - timeline not ready
                pass
        self._refresh_turn_rail()
        st.in_tool_rail = False
        st.pending_answer_divider = False

    def _make_image_widgets(self, imgs: list[Any]) -> list[Any]:
        """Build hidden Textual image widgets for the given attachments."""
        if not imgs:
            return []
        from synapse.ui.image_render import (
            TRANSCRIPT_MAX_ROWS,
            make_image_widget,
        )

        max_cols = 60
        try:
            timeline = self._app.query_one("#log", VerticalScroll)
            max_cols = max(20, int(getattr(timeline.size, "width", 60) or 60) - 2)
        except Exception:  # noqa: BLE001 - timeline not ready yet
            pass
        widgets = []
        for att in imgs:
            widget = make_image_widget(
                att, max_cols=max_cols, max_rows=TRANSCRIPT_MAX_ROWS
            )
            if widget is not None:
                widget.add_class("transcript-image")
                widget.image_attachment = att
                widgets.append(widget)
        return widgets

    def _trim_live_turn_pages(self) -> None:
        """Unmount completed live turns beyond the configured visible window."""
        st = self.state
        limit = max(1, int(getattr(self._app.settings, "history_tail_turns", 20) or 20))
        removed = False
        while len(st.live_turn_pages) >= limit:
            page = st.live_turn_pages.pop(0)
            self._drop_page_references(page)
            for block in page:
                try:
                    if isinstance(block, UserTurnBlock):
                        block.cleanup_images()
                    block.remove()
                except Exception:  # noqa: BLE001 - block may already be detached
                    pass
            removed = True
        if not removed:
            return
        self._renumber_user_turns()
        self._refresh_turn_rail()
        try:
            from synapse.ui.textual_lifecycle import clear_textual_style_cache_refs

            clear_textual_style_cache_refs()
        except Exception:  # noqa: BLE001
            pass

    def _drop_page_references(self, page: list[object]) -> None:
        st = self.state
        for block in page:
            if isinstance(block, UserTurnBlock) and block in st.user_turns:
                st.user_turns.remove(block)
            elif isinstance(block, ThoughtBlock) and block in st.thought_blocks:
                st.thought_blocks.remove(block)
            elif isinstance(block, ToolGroupBlock) and block in st.tool_blocks:
                st.tool_blocks.remove(block)
            elif block in st.approval_blocks:
                st.approval_blocks.remove(block)

    def _renumber_user_turns(self) -> None:
        history_state = getattr(getattr(self._app, "_history", None), "state", None)
        total_turns = int(getattr(history_state, "total_turns", 0) or 0)
        start = max(1, total_turns - len(self.state.user_turns) + 1)
        for index, block in enumerate(self.state.user_turns, start=start):
            block.turn_index = index

    def _refresh_turn_rail(self) -> None:
        """Rebuild right-side turn markers from current user anchors."""
        try:
            rail = self._app.query_one("#turn-rail", TurnRail)
        except Exception:  # noqa: BLE001
            return
        turns = [
            (format_turn_rail_preview(block.full_text), block)
            for block in self.state.user_turns
        ]
        rail.set_turns(turns)

    def jump_to_user_turn(self, target: UserTurnBlock) -> None:
        """Scroll the transcript so the selected user turn is at the top."""
        if target is None or not target.is_attached:
            return
        timeline = self._app.query_one("#log", VerticalScroll)
        try:
            timeline.scroll_to_widget(target, animate=True, top=True)
        except Exception:  # noqa: BLE001
            try:
                timeline.scroll_to_center(target, animate=True)
            except Exception:  # noqa: BLE001
                pass

    # -- copy selection / last answer -------------------------------------

    def action_copy_selection(self) -> None:
        """Copy current text selection, or fall back to the last answer body."""
        text: str | None = None
        try:
            text = self._app.screen.get_selected_text()
        except Exception:  # noqa: BLE001
            text = None
        if text and str(text).strip():
            self._copy_text_to_clipboard(str(text), label="selection")
            return
        self.action_copy_last_answer()

    def action_copy_last_answer(self) -> None:
        """Copy the most recent assistant answer body to the clipboard."""
        body = self._get_last_answer_body()
        if not body.strip():
            self.append_event("nothing to copy", "dim")
            return
        self._copy_text_to_clipboard(body, label="answer")

    def _get_last_answer_body(self) -> str:
        try:
            timeline = self._app.query_one("#log", VerticalScroll)
            for child in reversed(list(timeline.children)):
                if isinstance(child, AnswerBlock):
                    return child.body or ""
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _copy_text_to_clipboard(self, text: str, *, label: str = "text") -> None:
        body = text or ""
        if not body:
            self.append_event("nothing to copy", "dim")
            return
        try:
            self._app.copy_to_clipboard(body)
        except Exception as exc:  # noqa: BLE001
            self.append_event(f"copy failed: {exc}", "yellow")
            return
        n = len(body)
        preview = body.replace("\n", " ").strip()
        if len(preview) > 48:
            preview = preview[:47].rstrip() + "…"
        self.append_event(f"copied {label} ({n} chars): {preview}", "dim")

    def on_mouse_up(self, event: MouseUp) -> None:
        """After drag-select, auto-copy selected text to clipboard."""
        # Defer one frame so Textual's compositor has fully finalised the selection.
        self._app.call_after_refresh(self._auto_copy_selection)

    def _auto_copy_selection(self) -> None:
        if not self._app.screen.selections:
            return
        text = self._app.screen.get_selected_text()
        if text and str(text).strip():
            self._copy_text_to_clipboard(str(text), label="selection")

    # -- commit -----------------------------------------------------------

    def commit_thought(self, elapsed_s: float, body: str) -> None:
        st = self.state
        st.last_thought_body = body or ""
        st.last_thought_elapsed = elapsed_s
        st.thought_expanded = bool(
            getattr(self._app.settings, "expand_thinking", False)
        )
        live = st.live_stream_block
        if isinstance(live, ThoughtBlock) and st.live_stream_kind == "reasoning":
            live.seal(elapsed_s, body or "")
            st.live_stream_block = None
            st.live_stream_kind = None
            self._follow_timeline_if_needed()
        else:
            block = ThoughtBlock(
                elapsed_s,
                body,
                expand_on_seal=bool(
                    getattr(self._app.settings, "expand_thinking", False)
                ),
                dim_color=lambda: _styles._C_DIM,
                thought_mark=_MARK_THOUGHT,
            )
            st.thought_blocks.append(block)
            self._mount_block(block)
        st.in_tool_rail = False

    def action_toggle_last_thought(self) -> None:
        """Toggle the most recent ThoughtBlock (supports historical/frozen ones in transcript)."""
        timeline = self._app.query_one("#log", VerticalScroll)
        for child in reversed(list(timeline.children)):
            if isinstance(child, ThoughtBlock):
                child.toggle()
                self.state.thought_expanded = not child.collapsed
                return
        # Fallback to tracked list if DOM query yields nothing (e.g. cleared state)
        if self.state.thought_blocks:
            self.state.thought_blocks[-1].toggle()
            self.state.thought_expanded = not self.state.thought_blocks[-1].collapsed

    def action_toggle_last_tools(self) -> None:
        """Toggle the latest tool group (supports historical/frozen ones after commit).
        Queries live DOM so collapsed state works even for groups from prior turns.
        """
        timeline = self._app.query_one("#log", VerticalScroll)
        for child in reversed(list(timeline.children)):
            if isinstance(child, ToolGroupBlock):
                child.toggle()
                return
        # Fallback
        if self.state.tool_blocks:
            self.state.tool_blocks[-1].toggle()

    def commit_answer(self, text: str) -> None:
        body = (text or "").strip()
        if not body:
            return
        # Context-compaction summaries are for the model only.
        try:
            from synapse.runtime.context_compact import is_context_compact_text

            if is_context_compact_text(body):
                self.append_event("context compacted (hidden)", "dim")
                self.clear_stream()
                return
        except Exception:  # noqa: BLE001
            pass
        st = self.state
        st.last_answer_text = body
        self._commit_live_tools_to_log()
        live = st.live_stream_block
        if isinstance(live, AnswerBlock) and st.live_stream_kind == "answer":
            # Divider was mounted when the live answer row started (set_stream).
            live.seal(body)
            st.live_stream_block = None
            st.live_stream_kind = None
            self._follow_timeline_if_needed()
            return
        if st.pending_answer_divider:
            self._mount_answer_divider()
            st.pending_answer_divider = False
        # No live row (e.g. restore / non-stream path): mount sealed answer once.
        self._mount_block(
            AnswerBlock(
                body,
                live=False,
                fg_color=lambda: _styles._C_FG,
                markdown_max_chars=_MARKDOWN_MAX_CHARS,
            )
        )

    def _mount_answer_divider(self) -> None:
        """Insert centered ◇ rule with vertical spacing before the answer."""
        width = 0
        try:
            log = self._app.query_one("#log", VerticalScroll)
            width = int(getattr(log.size, "width", 0) or 0)
        except Exception:  # noqa: BLE001
            width = 0
        if width <= 0:
            width = int(getattr(self._app.size, "width", 0) or 0)
        # Subtract log padding (0 1) so the rule centers in the content box.
        usable = max(28, (width or 56) - 2)
        self._mount_block(
            AnswerDivider(usable, muted_color=lambda: _styles._C_MUTED)
        )

    # -- tool group rendering (live panel) --------------------------------

    def _render_live_tools(self) -> None:
        if self.state.live_tool_block is not None:
            self.state.live_tool_block.set_summary(
                self.state.live_tool_summary or "tools",
                render=not self._tool_batch_mode,
            )

    def _tool_details_expanded(self) -> bool:
        """Whether finished tool groups keep detail rows visible (config default: True)."""
        return bool(getattr(self._app.settings, "tool_details_expanded", True))

    def _commit_live_tools_to_log(self) -> None:
        st = self.state
        if st.live_tool_block is None:
            return
        st.last_tool_items = list(st.live_tool_block.items)
        st.last_tool_summary = st.live_tool_block.summary
        st.live_tool_items.clear()
        st.live_tool_summary = ""
        st.live_tool_block = None

    def write_tool_group_header(self, summary: str, collapsed: bool = True) -> None:
        st = self.state
        # Never paint empty placeholder groups ("0 tools").
        if (summary or "").strip() in {"", "0 tools", "tools", "Running 0 tools"}:
            if st.live_tool_block is None or not st.live_tool_block.items:
                return
        # A sealed previous group must leave _live_tool_block as None so the
        # next batch always creates a fresh block (never reuses a frozen one).
        if st.live_tool_block is None:
            block = ToolGroupBlock(summary)
            block.collapsed = collapsed
            block._render_block()
            st.live_tool_block = block
            st.tool_blocks.append(block)
            self._mount_block(block)
        else:
            st.live_tool_block.set_summary(
                summary, render=not self._tool_batch_mode
            )
            st.live_tool_block.set_collapsed(
                collapsed, render=not self._tool_batch_mode
            )
        st.live_tool_summary = summary
        st.last_tool_summary = summary

    def update_tool_group_header(self, summary: str) -> None:
        self.state.live_tool_summary = summary
        self.state.last_tool_summary = summary
        self._render_live_tools()

    def subagent_phase(self, parent_id: str, phase: str | None) -> None:
        """Update a subagent row's transient thinking/answering stage."""
        block = self.state.live_tool_block
        if block is not None:
            block.set_subagent_phase(parent_id, phase)

    def write_tool_item(self, item: ToolItem) -> None:
        st = self.state
        if st.live_tool_block is None:
            self.write_tool_group_header("tools", collapsed=False)
        assert st.live_tool_block is not None
        render = not self._tool_batch_mode
        # Keep live groups expanded while tools are still arriving/running,
        # even when auto-collapse-after-finish is enabled.
        if any(it.status == "running" for it in [*st.live_tool_block.items, item]):
            st.live_tool_block.set_collapsed(False, render=render)
        elif self._tool_details_expanded():
            st.live_tool_block.set_collapsed(False, render=render)
        st.live_tool_block.add_item(item, render=render)
        # Prefer the block's self-derived summary (always matches items).
        st.live_tool_summary = st.live_tool_block.summary
        st.last_tool_summary = st.live_tool_block.summary
        st.live_tool_items = list(st.live_tool_block.items)
        st.last_tool_items = list(st.live_tool_items)

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
        st = self.state
        if st.live_tool_block is None:
            return
        st.live_tool_block.update_item(
            item_id,
            status=status,
            preview=preview,
            error=error,
            label=label,
            path=path,
            name=name,
            category=category,
            render=not self._tool_batch_mode,
        )
        st.live_tool_summary = st.live_tool_block.summary
        st.last_tool_summary = st.live_tool_block.summary
        st.live_tool_items = list(st.live_tool_block.items)
        st.last_tool_items = list(st.live_tool_items)

    def write_tool_preview(
        self, item_id: str, preview: str, *, error: bool = False
    ) -> None:
        if self.state.live_tool_block is not None:
            self.state.live_tool_block.update_preview(item_id, preview, error=error)

    def close_tool_group(self) -> None:
        """Freeze the live tool block so the next batch creates a new group."""
        st = self.state
        if st.live_tool_block is not None:
            # Final header from items, not a stale early partial summary.
            st.live_tool_block._sync_summary_from_items(running=False)
            # Default: keep details expanded. Config can auto-collapse finished batches.
            # write_todos checklists always stay expanded for readability.
            has_todo = any(
                (it.name or "").lower() in {"write_todos", "todo_write", "todos"}
                or str(it.label or "").startswith("Todos ")
                for it in st.live_tool_block.items
            )
            keep_open = has_todo or self._tool_details_expanded()
            st.live_tool_block.set_collapsed(not keep_open)
            st.live_tool_summary = st.live_tool_block.summary
            st.last_tool_summary = st.live_tool_block.summary
            st.live_tool_block._render_block()
            # Tools finished → next final answer should show the ◇ rule.
            if st.live_tool_block.items:
                st.pending_answer_divider = True
        self._commit_live_tools_to_log()

    def append_meta(self, message: str) -> None:
        self._commit_live_tools_to_log()
        body = soften_turn_footer(message)
        self._mount_block(Static(Text(f"  {body}", style=_styles._C_MUTED)))

    def mount_approval(self, pending: Any) -> None:
        """Mount an interactive HITL approval block in the timeline."""
        from synapse.ui.approval import ApprovalBlock

        self._commit_live_tools_to_log()
        block = ApprovalBlock(pending, on_decide=self._on_approval_decided)
        self._mount_block(block)
        self.state.approval_blocks.append(block)

    def _on_approval_decided(self, action: str, message: str | None) -> bool:
        """Forward an approval-widget decision to the HITL resume path.

        Returns True when the resume was accepted. A False return tells the
        approval block to re-enable its buttons (the session may still be
        settling the interrupted turn into ``WAITING_APPROVAL``).
        """
        slash = getattr(self._app, "_slash", None)
        if slash is None:
            self._app.append_event("approval handler unavailable", "yellow")
            return False
        return bool(slash.resume_hitl(action, message))

    def append_event(self, message: str, style: str = "dim") -> None:
        self._mount_block(
            Static(Text(f"  {message}", style=style)),
            dismiss_welcome=(style or "dim").lower() != "dim",
        )

    # -- lifecycle ---------------------------------------------------------

    def reset_for_turn(self) -> None:
        """Reset live turn state before a new submission (keeps user turns)."""
        st = self.state
        st.last_tool_items = []
        st.live_tool_items = []
        st.live_tool_summary = ""
        st.live_tool_block = None

    def reset_all(self) -> None:
        """Drop every mounted-transcript reference (session switch / reset)."""
        st = self.state
        st.user_turns.clear()
        st.thought_blocks.clear()
        st.tool_blocks.clear()
        st.approval_blocks.clear()
        st.live_turn_pages.clear()
        st.current_turn_blocks.clear()
        st.live_stream_block = None
        st.live_stream_kind = None
        st.live_tool_block = None
        st.in_tool_rail = False
        st.pending_answer_divider = False
        st.last_tool_items = []
        st.last_tool_summary = ""
        st.live_tool_items = []
        st.live_tool_summary = ""
        st.last_answer_text = ""
        st.last_thought_body = ""
        st.last_thought_elapsed = 0.0
        st.thought_expanded = False
