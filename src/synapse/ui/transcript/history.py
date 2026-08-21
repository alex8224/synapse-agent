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

from synapse.ui.tool_blocks import ToolGroupBlock
from synapse.ui.transcript.factory import (
    build_answer_divider,
    build_restored_blocks,
    build_restored_tool_group,
)
from synapse.ui.transcript_blocks import ThoughtBlock
from synapse.ui.turn_rail_widgets import TurnRail
from synapse.ui.user_turn_block import UserTurnBlock

# Restored blocks are mounted in chunks so a large transcript never forces one
# giant layout + first-paint pass on the Textual event loop.
_MOUNT_BATCH_SIZE = 12


def _projection_needs_rebuild(
    projection: Any,
    thread_id: str,
    checkpoint_path: Any,
) -> bool:
    """Return whether the derived projection must be (re)built from the checkpoint.

    ``contains_thread`` alone is insufficient: after an abnormal shutdown the
    checkpoint may hold more turns than the projection. Reuse the domain-level
    reconciliation so the TUI, migration worker, and tests share one rule.
    """
    if not checkpoint_path:
        return not projection.contains_thread(thread_id)
    from synapse.sessions.transcript_migration import projection_needs_rebuild

    return projection_needs_rebuild(projection, thread_id, checkpoint_path)


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

    async def _reset_then_complete(
        self,
        *,
        reload_transcript: bool,
        announce: bool,
        generation: int,
        on_complete: Any | None,
    ) -> None:
        await self.reset_transcript_async(
            reload_transcript=reload_transcript,
            announce=announce,
            generation=generation,
        )
        if generation == self._app._transcript_generation and callable(on_complete):
            on_complete()

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
        on_complete: Any | None = None,
    ) -> None:
        """Serialize transcript replacement in one exclusive Textual worker."""
        app = self._app
        # Invalidate old stream callbacks synchronously; waiting until the worker
        # starts leaves a race where a queued callback can enter the new session.
        self.state.generation += 1
        app._transcript_generation += 1
        generation = app._transcript_generation
        app.run_worker(
            self._reset_then_complete(
                reload_transcript=reload_transcript,
                announce=announce,
                generation=generation,
                on_complete=on_complete,
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
        if _projection_needs_rebuild(
            projection, app.thread_id, getattr(app.settings, "checkpoint_path", None)
        ):
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
        # Turn chrome: restored sessions continue from their completed turns;
        # the next user submit advances the counter.
        app._current_turn = int(page.total_turns or 0)

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
        return build_restored_tool_group(self._app, tool_calls, tool_results)

    def build_answer_divider(self) -> Any:
        return build_answer_divider(self._app)

    def build_restored_blocks(self, events: list[Any]) -> list[Any]:
        return build_restored_blocks(self._app, events)

    def mount_blocks(self, blocks: list[Any]) -> None:
        """Mount restored blocks in small chunks across refresh frames.

        A single ``timeline.mount(*blocks)`` forces one big layout pass plus the
        first paint of every block in the same frame, which stalls the UI on
        large transcripts. Mounting ``_MOUNT_BATCH_SIZE`` blocks per refresh
        keeps each frame bounded while the rest paint over subsequent frames.
        """
        if not blocks:
            return
        app = self._app
        app._transcript._dismiss_welcome()
        try:
            timeline = app.query_one("#log", VerticalScroll)
        except Exception:  # noqa: BLE001 - transcript may be detaching
            return
        follow = (
            timeline.max_scroll_y <= 0
            or timeline.scroll_y >= timeline.max_scroll_y - 1
        )
        # Guard each follow-up chunk against session switches: a queued callback
        # survives ``remove_children()`` and would otherwise remount stale blocks
        # into the new session's timeline.
        generation = self.state.generation
        thread_id = app.thread_id

        def _mount_chunk(start: int) -> None:
            if self.state.generation != generation or app.thread_id != thread_id:
                return
            try:
                current = app.query_one("#log", VerticalScroll)
            except Exception:  # noqa: BLE001 - session switched mid-batch
                return
            chunk = blocks[start : start + _MOUNT_BATCH_SIZE]
            if not chunk:
                if follow:
                    app.call_after_refresh(app._transcript._scroll_timeline)
                return
            try:
                current.mount(*chunk)
            except Exception:  # noqa: BLE001 - widget may detach mid-batch
                return
            app.call_after_refresh(_mount_chunk, start + _MOUNT_BATCH_SIZE)

        # ``TranscriptController._mount_block`` tracks live writes; restored
        # batches are owned by ``state.pages`` and must not be counted twice.
        _mount_chunk(0)
