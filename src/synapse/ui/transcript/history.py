"""Transcript history: projection restore, paging, and generation guards.

Owns the paginated transcript-restore state (projection tail pages, history
generation, cursor) and the restore/migration/paging methods that used to live
directly on ``CodingAgentApp``.

Two independent generations are intentionally kept:
- ``app._transcript_generation`` rejects stream callbacks from an older session;
- ``history_state.generation`` rejects paging/migration worker results from an
  older session.

The Textual host keeps ``@work`` wrappers and event wiring and forwards here.
"""

from __future__ import annotations

from typing import Any

from textual.containers import VerticalScroll

import synapse.ui.tui_styles as _styles
from synapse.ui.tool_blocks import ToolGroupBlock
from synapse.ui.transcript_blocks import AnswerBlock, ThoughtBlock
from synapse.ui.tui_styles import _MARK_THOUGHT, _MARKDOWN_MAX_CHARS
from synapse.ui.turn_rail_widgets import TurnRail
from synapse.ui.user_turn_block import UserTurnBlock


class TranscriptHistoryState:
    """Paginated history-restore state (projection cursor and pages)."""

    def __init__(self, tail_turns: int = 20, max_pages: int = 5) -> None:
        self.tail_turns = max(1, int(tail_turns or 20))
        self.max_pages = max(1, int(max_pages or 5))
        self.before_turn = 0
        self.total_turns = 0
        self.total_events = 0
        self.pages: list[list[Any]] = []
        self.has_more = False
        self.loading = False
        self.thread_id = ""
        self.generation = 0

    def reset(self) -> None:
        """Drop the paging cursor and DOM page references (keep config knobs)."""
        self.before_turn = 0
        self.total_turns = 0
        self.total_events = 0
        self.pages = []
        self.has_more = False
        self.loading = False
        self.thread_id = ""


class TranscriptHistoryController:
    """Restore, migrate and page the compact transcript projection."""

    def __init__(self, app: Any) -> None:
        self._app = app
        settings = getattr(app, "settings", None)
        self.state = TranscriptHistoryState(
            tail_turns=(
                int(getattr(settings, "history_tail_turns", 20) or 20)
                if settings is not None
                else 20
            ),
            max_pages=5,
        )

    # -- reset / switch ---------------------------------------------------

    async def reset_transcript_async(
        self,
        *,
        reload_transcript: bool = False,
        announce: bool = False,
        generation: int | None = None,
    ) -> None:
        """Unmount the old session completely before painting another one."""
        app = self._app
        st = self.state
        if generation is None:
            st.generation += 1
            app._transcript_generation += 1
            generation = app._transcript_generation
        elif generation != app._transcript_generation:
            return
        st.loading = False

        log = app.query_one("#log", VerticalScroll)
        rail = app.query_one("#turn-rail", TurnRail)
        await log.remove_children()
        rail.clear_turns()

        # A newer switch may supersede this worker while it waits for Textual
        # message pumps to close. Never restore the superseded session.
        if generation != app._transcript_generation:
            return

        app._clear_transcript_state()
        try:
            from synapse.ui.textual_lifecycle import clear_textual_style_cache_refs

            clear_textual_style_cache_refs()
        except Exception:  # noqa: BLE001
            pass
        # Detached nodes may still be referenced by the screen compositor's
        # latest WidgetPlacement set until a full layout invalidation runs.
        app.refresh(layout=True)

        if reload_transcript:
            # Route through the host method so subclasses/test doubles can
            # override the restore step while production forwards back here.
            app._restore_session_transcript(announce=announce)
        else:
            app._transcript._show_welcome()

    def schedule_transcript_reset(
        self,
        *,
        reload_transcript: bool = False,
        announce: bool = False,
    ) -> None:
        """Serialize transcript replacement in one exclusive Textual worker."""
        app = self._app
        # Invalidate old stream callbacks synchronously; waiting until the worker
        # starts leaves a race where a queued callback can enter the new session.
        self.state.generation += 1
        app._transcript_generation += 1
        generation = app._transcript_generation
        app.run_worker(
            self.reset_transcript_async(
                reload_transcript=reload_transcript,
                announce=announce,
                generation=generation,
            ),
            exclusive=True,
            group="session-transcript",
        )

    # -- restore ----------------------------------------------------------

    def restore_session_transcript(self, *, announce: bool = True) -> None:
        """Load a compact tail page for current thread and paint the timeline.

        LLM context is restored by reusing the same ``thread_id`` with the
        LangGraph checkpointer; this method only rebuilds the visual history.
        The first open of a legacy thread builds a compact SQLite projection in
        a disposable child process, so full-history allocations never raise the
        long-lived TUI process memory high-water mark.
        """
        app = self._app
        if app.agent is None:
            return

        # Invalidate an in-flight page load before attempting to read the new
        # transcript.  This also covers restore failures, where the state
        # reset below cannot be reached.
        self.state.generation += 1
        self.state.loading = False
        projection = app._transcript_projection
        if not projection.contains_thread(app.thread_id):
            self.state.loading = True
            thread_id = app.thread_id
            generation = self.state.generation
            if announce:
                app.append_event("preparing legacy transcript…", "dim")
            # Delegate to the host's @work wrapper so migration runs off-thread.
            app._migrate_transcript_projection_bg(thread_id, generation, announce)
            return

        page = projection.load_tail(app.thread_id, turns=self.state.tail_turns)
        self.paint_restored_transcript(page, announce=announce)

    def migrate_transcript_projection_bg(
        self,
        thread_id: str,
        generation: int,
        announce: bool,
    ) -> None:
        """Migrate a legacy checkpoint outside the long-lived TUI process."""
        from synapse.sessions.transcript_migration import migrate_transcript_projection

        app = self._app
        result = migrate_transcript_projection(
            checkpoint_path=app.settings.checkpoint_path,
            projection_path=app._transcript_projection.path,
            thread_id=thread_id,
        )
        app.call_from_thread(
            self.transcript_migration_done,
            thread_id,
            generation,
            announce,
            result.success,
            result.error,
        )

    def transcript_migration_done(
        self,
        thread_id: str,
        generation: int,
        announce: bool,
        success: bool,
        error: str | None,
    ) -> None:
        """Paint migrated history only if the requesting session is still active."""
        app = self._app
        if self.state.generation != generation or app.thread_id != thread_id:
            return
        self.state.loading = False
        if not success:
            if announce:
                app.append_event(
                    f"restore transcript failed: {error or 'migration failed'}",
                    "yellow",
                )
            return
        page = app._transcript_projection.load_tail(
            thread_id, turns=self.state.tail_turns
        )
        self.paint_restored_transcript(page, announce=announce)

    def paint_restored_transcript(self, page: Any, *, announce: bool) -> None:
        """Apply one compact transcript tail page on the Textual UI thread."""
        app = self._app
        st = self.state

        # Reset paging state for the current thread before painting. Never keep
        # the full LangChain message list in the TUI.
        st.before_turn = page.start_turn
        st.total_turns = page.total_turns
        st.total_events = page.total_events
        st.has_more = page.has_more
        st.loading = False
        st.thread_id = app.thread_id

        if not page.events:
            if announce:
                # Only announce emptiness on explicit /switch restore.
                app.append_event("(empty session transcript)", "dim")
            return

        blocks = self.build_restored_blocks(page.events)
        self.mount_blocks(blocks)
        st.pages = [blocks]

        usage = app._transcript_projection.load_usage(app.thread_id)
        if usage is not None:
            app._apply_projected_usage(usage)

        if announce:
            loaded_turns = sum(1 for b in blocks if isinstance(b, UserTurnBlock))
            app.append_event(
                f"restored transcript: {loaded_turns} / {page.total_turns} user turns"
                f"  ({page.total_events} events)",
                "dim",
            )
            if page.has_more:
                app.append_event("scroll to top to load earlier history", "dim")
        # Jump to bottom after paint.
        app.call_after_refresh(app._transcript._scroll_timeline)

    def check_history_edge(self) -> None:
        """Poll: when the transcript is at the top and older turns remain,
        kick off an async page load."""
        st = self.state
        if st.loading or not st.has_more:
            return
        if st.before_turn <= 1:
            return
        try:
            timeline = self._app.query_one("#log", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        if timeline.scroll_y > 0:
            return
        self.request_earlier_history()

    def request_earlier_history(self) -> None:
        """Freeze the paging cursor and fold the next page off-thread."""
        st = self.state
        before_turn = st.before_turn
        if before_turn <= 1:
            return
        st.loading = True
        generation = st.generation
        # Delegate to the host's @work wrapper so folding runs off-thread.
        self._app._load_earlier_history_bg(
            before_turn,
            st.tail_turns,
            st.thread_id,
            generation,
        )

    def load_earlier_history_bg(
        self,
        before_turn: int,
        tail_turns: int,
        thread_id: str,
        generation: int,
    ) -> None:
        """Read an earlier projected page off-thread; insert on the UI thread."""
        app = self._app
        try:
            page = app._transcript_projection.load_before(
                thread_id,
                before_turn=before_turn,
                turns=tail_turns,
            )
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(
                self.history_load_done,
                None,
                before_turn,
                thread_id,
                generation,
                str(exc),
            )
            return
        app.call_from_thread(
            self.history_load_done,
            page,
            before_turn,
            thread_id,
            generation,
            None,
        )

    def history_load_done(
        self,
        page: Any,
        expected_turn: int,
        thread_id: str,
        generation: int,
        error: str | None,
    ) -> None:
        """Apply a folded page on the UI thread (stale results are dropped)."""
        st = self.state
        if (
            st.generation != generation
            or st.thread_id != thread_id
            or st.before_turn != expected_turn
        ):
            return
        st.loading = False
        if error:
            self._app.append_event(f"load earlier history failed: {error}", "yellow")
            return
        if page is None:
            return
        if not page.events:
            st.has_more = False
            return
        blocks = self.build_restored_blocks(page.events)
        self.insert_earlier_blocks(blocks)
        if not self.prepend_blocks(blocks):
            # Anchor disappeared (transcript rebuilt); drop this page, roll back
            # bookkeeping side effects and keep the cursor for a later retry.
            self.discard_earlier_blocks(blocks)
            return
        st.pages.insert(0, blocks)
        self.trim_mounted_history_pages()
        st.before_turn = page.start_turn
        st.has_more = page.has_more
        if not page.has_more:
            self._app.append_event("earliest history loaded", "dim")

    # -- page bookkeeping ---------------------------------------------------

    def trim_mounted_history_pages(self) -> None:
        """Keep a bounded history DOM and release all business-list references."""
        st = self.state
        ts = self._app._transcript.state
        while len(st.pages) > st.max_pages:
            page = st.pages.pop()
            users = [block for block in page if isinstance(block, UserTurnBlock)]
            thoughts = [block for block in page if isinstance(block, ThoughtBlock)]
            tools = [block for block in page if isinstance(block, ToolGroupBlock)]
            for block in users:
                if block in ts.user_turns:
                    ts.user_turns.remove(block)
            for block in thoughts:
                if block in ts.thought_blocks:
                    ts.thought_blocks.remove(block)
            for block in tools:
                if block in ts.tool_blocks:
                    ts.tool_blocks.remove(block)
            for block in page:
                try:
                    block.remove()
                except Exception:  # noqa: BLE001 - page may already be detached
                    pass
        for index, block in enumerate(ts.user_turns, start=1):
            block.turn_index = index
        self._app._transcript._refresh_turn_rail()

    def insert_earlier_blocks(self, blocks: list[Any]) -> None:
        """Insert older pages at the front of bookkeeping lists and renumber
        turn indexes so the rail stays globally consistent."""
        ts = self._app._transcript.state
        user_blocks = [b for b in blocks if isinstance(b, UserTurnBlock)]
        thought_blocks = [b for b in blocks if isinstance(b, ThoughtBlock)]
        tool_blocks = [b for b in blocks if isinstance(b, ToolGroupBlock)]
        for b in user_blocks:
            if b in ts.user_turns:
                ts.user_turns.remove(b)
        for b in thought_blocks:
            if b in ts.thought_blocks:
                ts.thought_blocks.remove(b)
        for b in tool_blocks:
            if b in ts.tool_blocks:
                ts.tool_blocks.remove(b)
        ts.user_turns[0:0] = user_blocks
        ts.thought_blocks[0:0] = thought_blocks
        ts.tool_blocks[0:0] = tool_blocks
        if user_blocks:
            k = len(user_blocks)
            for i, b in enumerate(user_blocks):
                b.turn_index = i + 1
            for b in ts.user_turns[k:]:
                if b.turn_index is not None:
                    b.turn_index = int(b.turn_index) + k
                    try:
                        b._render_block()
                    except Exception:  # noqa: BLE001
                        pass
        self._app._transcript._refresh_turn_rail()

    def discard_earlier_blocks(self, blocks: list[Any]) -> None:
        """Roll back ``insert_earlier_blocks`` when a page could not be mounted."""
        ts = self._app._transcript.state
        user_blocks = [b for b in blocks if isinstance(b, UserTurnBlock)]
        thought_blocks = [b for b in blocks if isinstance(b, ThoughtBlock)]
        tool_blocks = [b for b in blocks if isinstance(b, ToolGroupBlock)]
        for b in user_blocks:
            if b in ts.user_turns:
                ts.user_turns.remove(b)
        for b in thought_blocks:
            if b in ts.thought_blocks:
                ts.thought_blocks.remove(b)
        for b in tool_blocks:
            if b in ts.tool_blocks:
                ts.tool_blocks.remove(b)
        if user_blocks:
            k = len(user_blocks)
            for b in ts.user_turns:
                if b.turn_index is not None:
                    b.turn_index = max(1, int(b.turn_index) - k)
                    try:
                        b._render_block()
                    except Exception:  # noqa: BLE001
                        pass
        self._app._transcript._refresh_turn_rail()

    def prepend_blocks(self, blocks: list[Any]) -> bool:
        """Insert blocks at the top of the timeline, keeping the viewport.

        Returns False when the anchor disappeared (page dropped, cursor
        untouched so the next scroll retries).
        """
        if not blocks:
            return False
        timeline = self._app.query_one("#log", VerticalScroll)
        try:
            anchor = next(iter(timeline.children))
        except StopIteration:
            self.mount_blocks(blocks)
            return True
        old_max = timeline.max_scroll_y
        old_y = timeline.scroll_y
        try:
            # Textual 8.x has no Widget.before(); mount with before= anchor.
            timeline.mount(*blocks, before=anchor)
        except Exception:  # noqa: BLE001
            return False
        self._app.call_after_refresh(self.keep_scroll_after_prepend, old_max, old_y)
        return True

    def keep_scroll_after_prepend(self, old_max: int, old_y: float) -> None:
        """Offset the scroll position by the height added above the viewport."""
        try:
            timeline = self._app.query_one("#log", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        delta = max(0, timeline.max_scroll_y - old_max)
        target = min(old_y + delta, timeline.max_scroll_y)
        timeline.scroll_to(y=max(0, target), animate=False)

    # -- event -> widget factories -----------------------------------------

    def build_restored_tool_group(
        self,
        tool_calls: list[dict],
        tool_results: list[dict],
    ) -> tuple[ToolGroupBlock | None, bool]:
        """Build a historical tool batch as a group without mounting it.

        Returns ``(block, divider_needed)``; ``divider_needed`` mirrors the
        live path where a finished tool batch marks the next answer with a ◇.
        """
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
            str(r.get("id") or ""): r
            for r in (tool_results or [])
            if isinstance(r, dict)
        }
        result_by_name: dict[str, list[dict]] = {}
        for r in tool_results or []:
            if not isinstance(r, dict):
                continue
            result_by_name.setdefault(str(r.get("name") or ""), []).append(r)

        for i, call in enumerate(tool_calls or []):
            if not isinstance(call, dict):
                continue
            cid = str(call.get("id") or f"hist-{i}")
            item = build_tool_item(call, item_id=cid, index=i)
            res = result_by_id.get(cid)
            if res is None:
                bucket = result_by_name.get(str(call.get("name") or ""), [])
                if bucket:
                    res = bucket.pop(0)
            if res is not None:
                content = str(res.get("content") or "")
                status = str(res.get("status") or "ok")
                item.status = "error" if status == "error" else "done"
                item.error = item.status == "error"
                # Prefer checklist from tool args over dumping tool-result JSON.
                if is_todo_tool(item.name):
                    args = call.get("args") if isinstance(call, dict) else {}
                    checklist = format_todos_preview(extract_todos(args))
                    item.preview = checklist or (
                        truncate_preview(content) if content else None
                    )
                else:
                    item.preview = truncate_preview(content) if content else None
            else:
                item.status = "done"
            items.append(item)

        # Orphan results (no matching call) as plain items.
        used_ids = {it.id for it in items}
        for r in tool_results or []:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "")
            if rid and rid in used_ids:
                continue
            if rid and any(it.id == rid for it in items):
                continue
            fake = {
                "name": r.get("name") or "tool",
                "args": {},
                "id": rid or f"orphan-{len(items)}",
            }
            item = build_tool_item(fake, item_id=str(fake["id"]), index=len(items))
            content = str(r.get("content") or "")
            status = str(r.get("status") or "ok")
            item.status = "error" if status == "error" else "done"
            item.error = item.status == "error"
            item.preview = truncate_preview(content) if content else None
            items.append(item)

        if not items:
            return None, False
        summary = summarize_items(items, running=False)
        if (summary or "").strip() in {"", "0 tools", "tools", "Running 0 tools"}:
            return None, False
        block = ToolGroupBlock(summary)
        block.collapsed = True
        for it in items:
            block.add_item(it)
        block._sync_summary_from_items(running=False)
        has_todo = any(
            (it.name or "").lower() in {"write_todos", "todo_write", "todos"}
            or str(it.label or "").startswith("Todos ")
            for it in items
        )
        keep_open = has_todo or self._app._transcript._tool_details_expanded()
        block.set_collapsed(not keep_open)
        block._render_block()
        return block, True

    def build_answer_divider(self) -> Any:
        """Create an AnswerDivider widget without mounting it."""
        from synapse.ui.answer_divider import AnswerDivider

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
        return AnswerDivider(usable, muted_color=lambda: _styles._C_MUTED)

    def build_restored_blocks(self, events: list[Any]) -> list[Any]:
        """Build transcript widgets from folded events without mounting them.

        Maintains the same bookkeeping lists as the live path
        (``user_turns`` / ``thought_blocks`` / ``tool_blocks``) so toggle
        actions and the turn rail keep working for restored history.
        """
        app = self._app
        ts = app._transcript.state
        blocks: list[Any] = []
        pending_divider = False
        turn_count = len(ts.user_turns)
        for ev in events:
            kind = ev.kind
            if kind == "user":
                pending_divider = False
                turn_count += 1
                block = UserTurnBlock(
                    ev.text or "",
                    stamp=_stamp(),
                    turn_index=turn_count,
                    image_count=len(getattr(ev, "images", None) or []),
                )
                ts.user_turns.append(block)
                blocks.append(block)
            elif kind == "thought":
                # Historical thoughts: collapsed, elapsed unknown.
                block = ThoughtBlock(
                    0.0,
                    ev.text,
                    expand_on_seal=bool(
                        getattr(app.settings, "expand_thinking", False)
                    ),
                    dim_color=lambda: _styles._C_DIM,
                    thought_mark=_MARK_THOUGHT,
                )
                ts.thought_blocks.append(block)
                blocks.append(block)
            elif kind == "tools":
                group, divider = self.build_restored_tool_group(
                    ev.tool_calls, ev.tool_results
                )
                if group is not None:
                    ts.tool_blocks.append(group)
                    blocks.append(group)
                    pending_divider = divider
            elif kind == "answer":
                try:
                    from synapse.runtime.context_compact import (
                        is_context_compact_text,
                    )

                    if is_context_compact_text(ev.text):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                if pending_divider:
                    blocks.append(self.build_answer_divider())
                    pending_divider = False
                block = AnswerBlock(
                    ev.text,
                    live=False,
                    fg_color=lambda: _styles._C_FG,
                    markdown_max_chars=_MARKDOWN_MAX_CHARS,
                )
                blocks.append(block)
        app._transcript._refresh_turn_rail()
        return blocks

    def mount_blocks(self, blocks: list[Any]) -> None:
        """Mount a batch of transcript blocks (single layout pass)."""
        if not blocks:
            return
        app = self._app
        app._transcript._dismiss_welcome()
        timeline = app.query_one("#log", VerticalScroll)
        follow = (
            timeline.max_scroll_y <= 0
            or timeline.scroll_y >= timeline.max_scroll_y - 1
        )
        timeline.mount(*blocks)
        if follow:
            app.call_after_refresh(app._transcript._scroll_timeline)


def _stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%I:%M %p").lstrip("0")
