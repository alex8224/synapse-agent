"""Textual TUI — Cursor-style agent transcript with tool timeline.

Layout (Grok/Cursor chrome):
  top:     1-line centered: ≡ path · ⎇ branch · title · in/cache/out + ctx
  user:    accent bar ● prompt (multi-line, click expand) · time
  thought: ◆ Thought for Xs  (Ctrl+E expand)
  tools:   ▾ group header + ◆ per-item labels
  answer:  clean Markdown
  footer:  Worked for Xs.
  status:  [activity…]  (notices / spinner only)
  input:   › Build anything
  bottom:  model · thinking · mcp | mode | key hints
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Click, Key, MouseMove, MouseUp
from textual.screen import ModalScreen
from textual.widgets import Input, Static

import synapse.ui.tui_styles as _styles
from synapse.app.agent import build_coding_agent
from synapse.content.input_history import InputHistory
from synapse.content.multimodal import (
    ImageBank,
)
from synapse.integrations.openai_usage import CodexUsageService, ConsumeResetResult
from synapse.runtime.steer import SteerQueue, get_agent_steer_queue
from synapse.sessions.session_recap import SessionRecapController
from synapse.sessions.transcript_projection import (
    TranscriptProjection,
    TranscriptUsage,
    default_transcript_projection_path,
)
from synapse.subagent_monitor import SubagentMonitor
from synapse.tool_output.metrics import clear_metrics_notifier, set_metrics_notifier
from synapse.tool_output.repository import ToolOutputRepository
from synapse.ui.bottombar import (
    BottomBarAlign,
    BottomBarComponent,
    BottomBarRegion,
    BottomBarRegionSpec,
    BottomBarRegistry,
)
from synapse.ui.bottombar import (
    layout_from_registry as layout_bottombar_from_registry,
)
from synapse.ui.chrome.controller import ChromeController
from synapse.ui.clipboard import copy_to_clipboard
from synapse.ui.dialogs.codex_reset import CodexResetDialog
from synapse.ui.dialogs.controller import SlashController
from synapse.ui.formatters import (
    format_answer_divider as _format_answer_divider,
)
from synapse.ui.formatters import (
    format_byte_count,  # noqa: F401  - public re-export
    format_context_occupancy_label,  # noqa: F401  - public re-export
    format_token_rate,  # noqa: F401  - public re-export
    model_status_label,
)
from synapse.ui.formatters import (
    format_usage_label as _format_usage_label,
)
from synapse.ui.formatters import (
    short_model_name as _short_model_name,
)
from synapse.ui.formatters import (
    stream_tail_preview as _stream_tail_preview,
)
from synapse.ui.prompt_controller import PromptController
from synapse.ui.selectable_static import SelectableStatic as _SelectableStatic
from synapse.ui.selectable_static import (
    _annotate_strip_offsets as _annotate_strip_offsets_impl,
)
from synapse.ui.selectable_static import (
    _stylize_strip_char_span as _stylize_strip_char_span_impl,
)
from synapse.ui.status_controller import StatusController
from synapse.ui.steer_widget import SteerQueueWidget
from synapse.ui.subagent_status_bar import SubagentStatusBar
from synapse.ui.timeline import TODO_MARK_ACTIVE as _TODO_MARK_ACTIVE
from synapse.ui.timeline import TODO_MARK_DONE as _TODO_MARK_DONE
from synapse.ui.timeline import TODO_MARK_PENDING as _TODO_MARK_PENDING
from synapse.ui.timeline import TodoRow as _TodoRow
from synapse.ui.timeline import ToolItem
from synapse.ui.timeline import is_todo_tool as _is_todo_tool
from synapse.ui.timeline import parse_todo_preview_lines as _parse_todo_preview_lines
from synapse.ui.tool_blocks import TodoChecklist as _TodoChecklist
from synapse.ui.tool_blocks import ToolGroupBlock  # noqa: F401 - public re-export
from synapse.ui.tool_blocks import (
    render_todo_checklist_from_preview as _render_todo_checklist_from_preview,
)
from synapse.ui.tool_blocks import render_todo_row_texts as _render_todo_row_texts
from synapse.ui.tool_blocks import todo_kind_style as _todo_kind_style
from synapse.ui.topbar import (
    TopBarAlign,
    TopBarComponent,
    TopBarRegion,
    TopBarRegionSpec,
    TopBarRegistry,
)
from synapse.ui.topbar import (
    display_width as _display_width,
)
from synapse.ui.topbar.git_chrome import (
    GitBranchChrome,
    probe_git_branch_chrome,
)
from synapse.ui.topbar.widget import TopBar
from synapse.ui.transcript.controller import TranscriptController
from synapse.ui.transcript.history import TranscriptHistoryController
from synapse.ui.transcript_blocks import AnswerBlock, ThoughtBlock, _MarkdownBlock  # noqa: F401
from synapse.ui.tui_launch import run_tui  # noqa: F401  - public re-export
from synapse.ui.tui_styles import (
    _MARK_INPUT,
)
from synapse.ui.turn.controller import TurnController
from synapse.ui.turn_rail import format_turn_rail_bucket_label as _format_turn_rail_bucket_label
from synapse.ui.turn_rail import turn_rail_tick_slots as _turn_rail_tick_slots
from synapse.ui.turn_rail_widgets import TurnRail
from synapse.ui.turn_rail_widgets import TurnRailGap as _TurnRailGap
from synapse.ui.turn_rail_widgets import TurnRailItem as _TurnRailItem
from synapse.ui.user_turn import (
    compress_paste_placeholder as _compress_paste_placeholder,
)
from synapse.ui.user_turn import format_user_turn_meta as _format_user_turn_meta
from synapse.ui.user_turn import has_paste_placeholder as _has_paste_placeholder
from synapse.ui.user_turn import wrap_user_turn_text as _wrap_user_turn_text
from synapse.ui.user_turn_block import UserTurnBlock
from synapse.ui.welcome import WelcomeView

_copy_to_clipboard = copy_to_clipboard
format_answer_divider = _format_answer_divider
format_usage_label = _format_usage_label
short_model_name = _short_model_name
stream_tail_preview = _stream_tail_preview
TodoChecklist = _TodoChecklist
render_todo_checklist_from_preview = _render_todo_checklist_from_preview
render_todo_row_texts = _render_todo_row_texts
todo_kind_style = _todo_kind_style
SelectableStatic = _SelectableStatic
TurnRailGap = _TurnRailGap
TurnRailItem = _TurnRailItem
format_turn_rail_bucket_label = _format_turn_rail_bucket_label
turn_rail_tick_slots = _turn_rail_tick_slots
format_user_turn_meta = _format_user_turn_meta
wrap_user_turn_text = _wrap_user_turn_text
compress_paste_placeholder = _compress_paste_placeholder
has_paste_placeholder = _has_paste_placeholder
TodoRow = _TodoRow
TODO_MARK_ACTIVE = _TODO_MARK_ACTIVE
TODO_MARK_DONE = _TODO_MARK_DONE
TODO_MARK_PENDING = _TODO_MARK_PENDING
is_todo_tool = _is_todo_tool
parse_todo_preview_lines = _parse_todo_preview_lines
display_width = _display_width
_annotate_strip_offsets = _annotate_strip_offsets_impl
_stylize_strip_char_span = _stylize_strip_char_span_impl


def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


def _git_branch(cwd: Path) -> str | None:
    """Backward-compatible branch name probe."""
    info = probe_git_branch_chrome(cwd)
    return info.name if info is not None else None
class CodingAgentApp(App[None]):
    """Cursor-like agent transcript."""

    CSS = _styles.APP_CSS

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_log", "Clear", show=False),
        Binding("ctrl+e", "toggle_last_thought", "Expand thought", show=False),
        Binding("ctrl+t", "toggle_last_tools", "Toggle tools", show=False),
        # Copy selection (or last answer). Not ctrl+c — that quits the app.
        Binding(
            "alt+c",
            "copy_selection",
            "Copy selection",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+shift+y",
            "copy_last_answer",
            "Copy last answer",
            show=False,
            priority=True,
        ),
        Binding("ctrl+shift+c", "copy_selection", "Copy", show=False),
        Binding("ctrl+shift+s", "open_selectable_view", "Select text", show=True, priority=True),
        Binding("f7", "open_selectable_view", "Select text", show=False),
        Binding("alt+v", "clipboard_paste", "Paste image", show=False, priority=True),
        # priority: capture ESC even while the prompt Input has focus
        Binding("escape", "cancel_run", "Cancel", show=False, priority=True),
        Binding("up", "history_up", "HistoryUp", show=False, priority=True),
        Binding("down", "history_down", "HistoryDown", show=False, priority=True),
        # Dialog shortcuts (F-keys)
        Binding("f2", "dialog_model", "Model", show=False),
        Binding("f3", "dialog_theme", "Theme", show=False),
        Binding("f4", "dialog_sessions", "Sessions", show=False),
        Binding("f5", "dialog_mcp", "MCP", show=False),
        Binding("f6", "dialog_safety", "Safety", show=False),
        Binding("f7", "dialog_codex_import", "Import Codex", show=False),
        Binding("f8", "dialog_theme_designer", "Design Theme", show=False),
        Binding("f9", "dialog_subagents", "Subagents", show=False),
        Binding("f10", "dialog_sessions_delete", "Delete sessions", show=False),
        Binding("f11", "dialog_debug_inspector", "Debug Inspector", show=True),
    ]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Yield Esc/Up/Down to modal dialogs (App priority bindings run first)."""
        if isinstance(self.screen, ModalScreen) and action in {
            "cancel_run",
            "history_up",
            "history_down",
        }:
            return False
        return True

    def action_dialog_model(self) -> None:
        self._open_model_dialog([])

    def action_dialog_theme(self) -> None:
        self._open_theme_dialog()

    def action_dialog_sessions(self) -> None:
        self._open_session_dialog(["switch"])

    def action_dialog_sessions_delete(self) -> None:
        self._open_session_dialog(["session", "multi_delete"])

    def action_dialog_mcp(self) -> None:
        self._open_mcp_dialog()

    def action_dialog_safety(self) -> None:
        self._open_safety_dialog()

    def action_dialog_codex_import(self) -> None:
        self._open_codex_import_dialog()

    def action_dialog_theme_designer(self) -> None:
        self._open_theme_designer()

    def action_dialog_subagents(self) -> None:
        self._open_subagent_monitor()

    def action_dialog_debug_inspector(self) -> None:
        """Start the inspector without blocking Textual's event loop."""
        self._start_debug_inspector()

    @work(thread=True, exclusive=True, group="debug-inspector")
    def _start_debug_inspector(self) -> None:
        from synapse.observability.debug_server import get_debug_server
        from synapse.observability.llm_debug import get_debug_store

        store = get_debug_store()
        server = get_debug_server()
        try:
            server.start()
            store.enabled = True
            self.call_from_thread(
                self.append_event,
                f"Debug inspector: {server.url}  (open this address in a browser)",
                "green",
            )
        except OSError as exc:
            self.call_from_thread(
                self.append_event,
                f"Debug server failed: {exc}",
                "yellow",
            )

    def get_css_variables(self) -> dict[str, str]:
        """Merge Textual defaults with the active theme's ``$theme-*`` palette."""
        variables = super().get_css_variables()
        try:
            from synapse.ui.theme import get_theme

            return {**variables, **get_theme().css_variables()}
        except Exception:  # noqa: BLE001
            return variables

    def __init__(
        self,
        *,
        agent: Any,
        settings: Any,
        thread_id: str,
        env_path: Path | None = None,
        project_root: Path | None = None,
        defer_agent_build: bool = False,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.settings = settings
        self.thread_id = thread_id
        self.env_path = env_path
        self.project_root = project_root or Path.cwd()
        self._defer_agent_build = bool(defer_agent_build and agent is None)
        self._agent_ready = threading.Event()
        self._agent_error: str | None = None
        self._mcp_attaching = False
        self._mcp_reloading = False
        self._codex = CodexUsageService(settings=settings)
        self._codex_bottombar_hovered = False
        self._current_goal: Any = None
        self._goal_listener_bound = False
        self._goal_listener_fn: Any = None
        self._image_bank = ImageBank()
        if agent is not None:
            self._agent_ready.set()
        self._busy = False
        self._compacting_context = False
        self._cancel_event = threading.Event()
        # Optional background provider prewarm after resuming a large session.
        # Cancelled as soon as the user submits a real message so the warm-up
        # and the real turn never queue two huge requests concurrently.
        self._prewarm_cancel_event = threading.Event()
        self._prewarm_started = False
        self._steer_bound_queue: SteerQueue | None = None
        self._steer_listener: Any | None = None
        self._status = StatusController(self)
        self._status._detail = "ready" if agent is not None else "starting"
        self._active_turn_agent: Any | None = None
        self._active_turn_thread_id: str | None = None
        self._active_steer_queue: SteerQueue | None = None
        self._subagent_monitor = SubagentMonitor()
        self._subagent_status_text = ""
        self._skip_steer_followup = False
        self._transcript = TranscriptController(self)
        # Paginated transcript restore: only the last N visible turns are
        # painted at startup; older pages load on demand when scrolling up.
        # `history_tail_turns` is an optional settings knob (default 20).
        self._transcript_projection = TranscriptProjection(
            default_transcript_projection_path(settings.resolved_sessions_path())
        )
        self._history = TranscriptHistoryController(self)
        self._turn = TurnController(self)
        self._slash = SlashController(self)
        # Invalidates callbacks queued by a stream sink from an older session.
        self._transcript_generation = 0
        self._session_recap = SessionRecapController(
            enabled=bool(getattr(settings, "session_recap_enabled", True)),
            idle_seconds=float(
                getattr(settings, "session_recap_idle_seconds", 180.0) or 180.0
            ),
            min_turns=int(getattr(settings, "session_recap_min_turns", 3) or 3),
        )
        # Global project catalog (projection) and per-turn summary persistence.
        self._project_catalog: Any = None
        self._summary_store: Any = None
        self._context_tokens = 0
        self._last_out_tokens = 0
        self._input_tokens = 0
        self._cache_tokens = 0
        self._output_tokens = 0
        self._output_tokens_per_second: float | None = None
        self._last_ttft_s: float | None = None
        self._last_rate_basis = "end_to_end"
        self._token_rate_estimated = False
        # Snapshot before a live turn so mid-turn updates stay absolute.
        self._usage_base_input = 0
        self._usage_base_output = 0
        self._usage_base_cache = 0
        self._session_title = ""
        # Ephemeral left-side status notice (slash confirms etc.); not transcript.
        ws = Path(getattr(settings, "workspace", Path.cwd()) or Path.cwd())
        self._git_chrome: GitBranchChrome | None = probe_git_branch_chrome(ws)
        self._git_branch = self._git_chrome.name if self._git_chrome else None
        hist_root = Path(project_root or ws)
        self._prompt = PromptController(
            self,
            InputHistory.for_project(hist_root),
            self._image_bank,
        )
        self._tool_output_repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
        self._tool_output_stats: dict[str, Any] = {}
        self._tool_output_stats_thread_id: str | None = None
        self._tool_output_refresh_pending = False
        self._chrome = ChromeController(self)
        self._topbar = TopBarRegistry()
        self._install_default_topbar()
        self._bottombar = BottomBarRegistry()
        self._install_default_bottombar()
        self.title = "Synapse"
        self.sub_title = model_status_label(settings)
        self._reload_session_title()

    def compose(self) -> ComposeResult:
        from synapse.commands.slash_complete import make_textual_suggester

        yield TopBar(
            registry_provider=lambda: self._topbar,
            workspace_provider=lambda: Path(
                getattr(self.settings, "workspace", Path.cwd()) or Path.cwd()
            ),
            dirty_provider=lambda: bool(
                self._git_chrome is not None and self._git_chrome.dirty
            ),
            tool_output_stats_provider=self._tool_output_hover_stats,
            usable_width_provider=self._topbar_usable_width,
            colors={
                "clean": _styles._C_GREEN,
                "dirty": _styles._C_ERROR,
                "dim": _styles._C_DIM,
                "fg": _styles._C_FG,
                "orange": _styles._C_ORANGE,
                "added": _styles._C_GREEN,
                "deleted": _styles._C_ERROR,
            },
            id="topbar",
        )
        with Vertical(id="main", classes="welcome"):
            yield WelcomeView(self.project_root, id="welcome")
            yield VerticalScroll(id="log")
            # Floating overlay: hover previews must not reflow the transcript.
            yield TurnRail(id="turn-rail")
            yield Static(id="stream")
        with Vertical(id="bottom-chrome"):
            yield SteerQueueWidget(id="steer-queue")
            yield SubagentStatusBar("", id="subagent-status")
            yield Static("", id="status")
            yield Static("", id="complete-hint")
            yield Input(
                placeholder=f"{_MARK_INPUT}  Build anything  (/ for commands, Tab complete)",
                id="prompt",
                suggester=make_textual_suggester(
                    self._prompt.complete_ctx,
                    workspace=self.project_root,
                ),
            )
            yield Static("", id="bottombar")

    @property
    def transcript_generation(self) -> int:
        """Generation used to reject callbacks from an older session stream."""
        return self._transcript_generation

    def _call_for_transcript(
        self,
        generation: int,
        callback: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Run a UI callback only while its originating transcript is current."""
        if generation == self._transcript_generation:
            self.call_from_thread(callback, *args, **kwargs)

    def on_unmount(self) -> None:
        clear_metrics_notifier()
        try:
            self._transcript_projection.close()
        except Exception:  # noqa: BLE001 - shutdown best effort
            pass

    def on_mount(self) -> None:
        # Apply configured theme before first paint of chrome widgets.
        try:
            self.apply_theme(
                getattr(self.settings, "theme", None),
                persist=False,
                announce=False,
            )
        except Exception:  # noqa: BLE001
            pass
        self._reload_tool_output_stats()
        set_metrics_notifier(self._on_tool_output_metrics_changed)
        self._refresh_bottombar()
        self._refresh_codex_usage()
        self.set_interval(0.1, self._tick_status)
        self.set_interval(60.0, self._refresh_codex_usage)
        # Poll scroll position: when the transcript reaches the top and older
        # turns remain, kick off an async page load (no Textual scroll events
        # in this version, so a cheap 300 ms poll is used instead).
        self.set_interval(0.3, self._check_history_edge)
        log = self.query_one("#log", VerticalScroll)
        # Hide scrollbar chrome; mouse-wheel / keys / scroll_* still work.
        log.show_vertical_scrollbar = False
        log.show_horizontal_scrollbar = False
        self.query_one("#prompt", Input).focus()
        if self._defer_agent_build or self.agent is None:
            self.set_activity("starting", "loading agent…", True)
            self.append_event("starting agent in background…", "dim")
            self._bg_build_agent()
        else:
            self.call_after_refresh(self._restore_session_transcript)

    @work(thread=True, exclusive=True, group="startup")
    def _bg_build_agent(self) -> None:
        """Build agent off the UI thread; attach MCP in a second phase."""
        from synapse.app.agent import attach_mcp_to_agent, build_coding_agent
        from synapse.observability.startup_trace import duration

        startup_started = time.perf_counter()

        def report_progress(detail: str) -> None:
            self.call_from_thread(self.set_activity, "starting", detail, False)

        try:
            agent = build_coding_agent(
                self.settings,
                project_root=self.project_root,
                load_mcp=False,
                progress=report_progress,
                prompt_cache_key=lambda: self.thread_id,
            )
            self.agent = agent
            self._agent_ready.set()
            duration("agent.ready", startup_started, phase="startup")
            self.call_from_thread(self._on_agent_ready, False)
        except Exception as exc:  # noqa: BLE001
            self._agent_error = str(exc)
            self._agent_ready.set()
            self.call_from_thread(
                self.append_event,
                f"agent start failed: {exc}",
                "bold red",
            )
            self.call_from_thread(self.set_activity, "idle", "agent failed", True)
            return

        if not bool(getattr(self.settings, "enable_mcp", True)):
            return
        if getattr(agent, "_coding_mcp_attached", False):
            return
        mcp_started = time.perf_counter()
        try:
            self._mcp_attaching = True
            self.call_from_thread(
                self.set_activity, "starting", "connecting MCP…", False
            )
            agent2 = attach_mcp_to_agent(
                self.settings,
                agent,
                project_root=self.project_root,
            )
            if self.agent is not agent:
                # A model switch replaced phase-1 while MCP was connecting.
                # Rebuild the current graph with the now-live pool; this path
                # reuses tools and performs no second network connection.
                current = self.agent
                if current is None:
                    return
                current_with_mcp = attach_mcp_to_agent(
                    self.settings,
                    current,
                    project_root=self.project_root,
                )
                if self.agent is current:
                    self.agent = current_with_mcp
                    self.call_from_thread(self._bind_steer_queue)
                    self.call_from_thread(self._on_mcp_attached)
                return
            self.agent = agent2
            self.call_from_thread(self._bind_steer_queue)
            if not self._busy:
                self.call_from_thread(self._on_mcp_attached)
            else:
                self.call_from_thread(
                    self.append_event,
                    "MCP tools attached (will apply next turn)",
                    "dim",
                )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.append_event,
                f"MCP attach failed (agent still usable): {exc}",
                "yellow",
            )
        finally:
            duration("mcp.attach", mcp_started, phase="startup")
            self._mcp_attaching = False
            if not self._busy:
                self.call_from_thread(self.set_activity, "idle", "ready", True)
                # MCP attach failed or was skipped: the phase-1 agent is the
                # final shape, so prewarm it (no-op if already started).
                self.call_from_thread(self._maybe_start_prewarm)

    def _on_agent_ready(self, with_mcp: bool) -> None:
        label = "agent ready" + (" + MCP" if with_mcp else " (MCP pending)")
        self.append_event(label, "dim")
        self.set_activity("idle", "ready", True)
        self._bind_steer_queue()
        self._bind_goal_listener()
        self._load_current_goal()
        self._restore_session_transcript(announce=True)
        # Prewarm only when MCP will never attach: a prewarm sent before the
        # MCP phase would use a different tool schema than real turns, which
        # breaks the provider prefix cache entirely. With MCP enabled, prewarm
        # fires from _on_mcp_attached instead.
        if not bool(getattr(self.settings, "enable_mcp", True)):
            self._maybe_start_prewarm()

    def _maybe_start_prewarm(self) -> None:
        """Kick off an optional background provider prewarm after resume.

        Enabled only via ``session_prewarm_enabled`` (default off). Uses the
        live agent on a throwaway thread so the cached prefix matches a real
        turn; the user's first message cancels it. Must be called only once the
        agent is in its final shape (MCP attached), otherwise the tool schema
        differs from real turns and the provider cache cannot be hit. See
        ``synapse.runtime.session_prewarm`` for details.
        """
        if not bool(getattr(self.settings, "session_prewarm_enabled", False)):
            return
        if self._prewarm_started or self.agent is None or not self.thread_id:
            return
        # MCP is attaching right now: wait for _on_mcp_attached to call us
        # again with the final agent so the cached prefix matches real turns.
        if self._mcp_attaching:
            return
        self._prewarm_started = True
        self._prewarm_cancel_event = threading.Event()

        def _worker() -> None:
            from synapse.runtime.session_prewarm import prewarm_session

            try:
                prewarm_session(
                    self.agent,
                    self.thread_id,
                    cancel_event=self._prewarm_cancel_event,
                    notify=lambda text: self.call_from_thread(
                        self.append_event, text, "dim"
                    ),
                )
            except Exception:  # noqa: BLE001 - prewarm must never break the session
                pass

        threading.Thread(target=_worker, name="session-prewarm", daemon=True).start()

    def _on_mcp_attached(self) -> None:
        servers = list(getattr(build_coding_agent, "last_mcp_servers", []) or [])
        tools = list(getattr(build_coding_agent, "last_mcp_tool_names", []) or [])
        warnings = list(getattr(build_coding_agent, "last_mcp_warnings", []) or [])
        self.append_event(
            f"MCP ready: servers={servers or '-'} tools={len(tools)}",
            "dim",
        )
        for w in warnings:
            self.append_event(f"mcp: {w}", "yellow")
        self.set_activity("idle", "ready", True)
        # Agent is now in its final shape (MCP tools included); prewarm with
        # the exact same tool schema real turns will use.
        self._maybe_start_prewarm()

    # -- prompt controller forwarding --------------------------------------

    def _set_complete_hint(self, value: str) -> None:
        self._prompt.set_complete_hint(value)

    def _apply_completion(self, line: str) -> None:
        self._prompt.apply_completion(line)

    def action_complete_slash(self) -> None:
        """Accept / cycle slash completions (Tab)."""
        self._prompt.complete_next()

    def action_complete_slash_prev(self) -> None:
        """Cycle slash completions backwards (Shift+Tab)."""
        self._prompt.complete_prev()

    def action_focus_next(self) -> None:
        """Tab: run completion for @/slash, or focus next widget."""
        if not self._prompt.focus_next():
            self.screen.focus_next()

    def action_focus_previous(self) -> None:
        """Shift+Tab: run completion (prev) for @/slash, or focus previous widget."""
        if not self._prompt.focus_previous():
            self.screen.focus_previous()

    def action_show_completions(self) -> None:
        """List available slash completions (Ctrl+Space)."""
        self._prompt.show_completions()

    def _set_prompt_value(self, text: str) -> None:
        self._prompt.set_prompt_value(text)

    def action_history_up(self) -> None:
        """Recall older project input history / navigate completion (up)."""
        self._prompt.history_up()

    def action_history_down(self) -> None:
        """Recall newer project input history / navigate completion (down)."""
        self._prompt.history_down()

    def action_clipboard_paste(self) -> None:
        """Alt+V clipboard paste (image or text)."""
        self._prompt.paste_clipboard()

    @on(Input.Changed, "#prompt")
    def handle_prompt_changed(self, event: Input.Changed) -> None:
        self._prompt.on_prompt_changed(event.value or "")

    # -- chrome controller forwarding --------------------------------------

    def _mcp_snapshot(self):
        return self._chrome.mcp_snapshot()

    def _mcp_label(self) -> str:
        return self._chrome.mcp_label()

    def _reload_session_title(self) -> None:
        self._chrome.reload_session_title()

    def _session_title_label(self, *, max_len: int = 48) -> str:
        return self._chrome.session_title_label(max_len=max_len)

    def _context_window_tokens(self) -> int | None:
        return self._chrome.context_window_tokens()

    def _usage_right_label(self) -> str | Text:
        return self._chrome.usage_right_label()

    def _tool_output_label(self) -> str | Text:
        return self._chrome.tool_output_label()

    def _tool_output_hover_stats(self) -> dict[str, Any]:
        return self._chrome.tool_output_hover_stats()

    def _reload_tool_output_stats(self) -> None:
        self._chrome.reload_tool_output_stats()

    def _on_tool_output_metrics_changed(self, thread_id: str) -> None:
        self._chrome.on_tool_output_metrics_changed(thread_id)

    def _refresh_tool_output_stats(self) -> None:
        self._chrome.refresh_tool_output_stats()

    def _begin_turn_usage(self) -> None:
        self._chrome.begin_turn_usage()

    def apply_turn_usage(
        self,
        *,
        turn_input: int = 0,
        turn_output: int = 0,
        turn_cache: int = 0,
        last_input: int = 0,
        last_output: int = 0,
        last_cache: int = 0,
        output_tokens_per_second: float | None = None,
        ttft_s: float | None = None,
        rate_basis: str = "end_to_end",
        rate_estimated: bool = False,
    ) -> None:
        self._chrome.apply_turn_usage(
            turn_input=turn_input,
            turn_output=turn_output,
            turn_cache=turn_cache,
            last_input=last_input,
            last_output=last_output,
            last_cache=last_cache,
            output_tokens_per_second=output_tokens_per_second,
            ttft_s=ttft_s,
            rate_basis=rate_basis,
            rate_estimated=rate_estimated,
        )

    def _reset_session_token_chrome(self) -> None:
        self._chrome.reset_session_token_chrome()

    def _apply_restored_usage(self, messages: list[Any] | None) -> None:
        self._chrome.apply_restored_usage(messages)

    def _apply_projected_usage(self, usage: TranscriptUsage) -> None:
        self._chrome.apply_projected_usage(usage)

    def _render_branch_chrome(self):
        return self._chrome.render_branch_chrome()

    def _refresh_git_chrome(self) -> None:
        self._chrome.refresh_git_chrome()

    def _install_default_topbar(self) -> None:
        self._chrome.install_default_topbar()

    def _apply_topbar_region_bands(self) -> None:
        self._chrome.apply_topbar_region_bands()

    def _install_default_bottombar(self) -> None:
        self._chrome.install_default_bottombar()

    def _goal_label(self) -> str:
        return self._chrome.goal_label()

    def _bind_goal_listener(self) -> None:
        """订阅 GoalService 变更以刷新 bottombar。"""
        service = getattr(self.agent, "_coding_goal_service", None)
        if service is None:
            return
        if getattr(self, "_goal_listener_bound", False):
            return

        def _on_goal_changed(thread_id: str, goal: object | None) -> None:
            def _apply() -> None:
                current_thread_id = self.__dict__.get("thread_id")
                if thread_id and current_thread_id and thread_id != current_thread_id:
                    return
                previous = self.__dict__.get("_current_goal")
                self.__dict__["_current_goal"] = goal
                try:
                    self._bottombar.refresh()
                except Exception:  # noqa: BLE001
                    pass
                from synapse.goals.model import ThreadGoalStatus

                # 预算耗尽且回合仍在运行：注入收尾引导，让模型停止新工作。
                if goal is not None and self.__dict__.get("_busy", False):
                    if goal.status == ThreadGoalStatus.BUDGET_LIMITED:
                        from synapse.goals.steering import budget_limit_prompt

                        runtime = service.runtime(thread_id)
                        queue = (
                            getattr(self, "_active_steer_queue", None)
                            or self._turn_steer_queue()
                            if runtime.mark_budget_reported(goal.goal_id)
                            else None
                        )
                        if queue is not None:
                            try:
                                queue.push(budget_limit_prompt(goal))
                            except Exception:  # noqa: BLE001
                                pass
                    elif (
                        previous is not None
                        and getattr(previous, "objective", None) != goal.objective
                    ):
                        from synapse.goals.steering import objective_updated_prompt

                        queue = (
                            getattr(self, "_active_steer_queue", None)
                            or self._turn_steer_queue()
                        )
                        if queue is not None:
                            try:
                                queue.push(objective_updated_prompt(goal))
                            except Exception:  # noqa: BLE001
                                pass
                    return
                # 目标变为 active 且线程空闲（/goal 设置、resume 等）：立即续跑。
                if goal is not None and goal.status == ThreadGoalStatus.ACTIVE:
                    try:
                        queue = self.__dict__.get("_active_steer_queue")
                        if queue is None:
                            queue = get_agent_steer_queue(self.__dict__.get("agent"))
                        if queue is not None:
                            from synapse.goals.steering import (
                                GOAL_STEER_PREFIX,
                                continuation_prompt,
                            )

                            if not any(
                                str(item).strip().startswith(GOAL_STEER_PREFIX)
                                for item in queue.peek_items()
                            ):
                                queue.push(
                                    f"{GOAL_STEER_PREFIX}\n{continuation_prompt(goal)}"
                                )
                                schedule = getattr(self, "_schedule_followup_steer", None)
                                if schedule is not None:
                                    schedule(queue)
                    except Exception:  # noqa: BLE001
                        pass

            try:
                self.call_from_thread(_apply)
            except RuntimeError:
                # 已在 UI 线程（slash 处理中同步通知）：直接执行。
                _apply()
            except Exception:  # noqa: BLE001 - 非 Textual 线程环境同步回退
                try:
                    _apply()
                except Exception:  # noqa: BLE001 - 通知不能阻断 goal 持久化
                    pass

        self._goal_listener_bound = True
        self._goal_listener_fn = _on_goal_changed
        service.add_listener(_on_goal_changed)

    def _load_current_goal(self) -> None:
        """启动/切会话后加载当前 thread 的 goal（用于 bottombar 与续跑）。"""
        service = getattr(self.agent, "_coding_goal_service", None)
        if service is None:
            return
        try:
            goal = service.get(self.thread_id)
        except Exception:  # noqa: BLE001
            goal = None
        self._current_goal = goal
        try:
            self._bottombar.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _codex_usage_label(self) -> str | Text:
        return self._chrome.codex_usage_label()

    # -- codex reset-credits popup ------------------------------------------

    def _open_codex_reset_dialog(self) -> None:
        """Show reset-credit details in a popup; fetch details if needed."""
        credits = self._codex.reset_credits
        sn = self._codex.snapshot
        available = (
            sn.reset_credits.available_count
            if sn and sn.reset_credits
            else 0
        )
        # When we have a count but no detail rows yet, fetch first then re-open.
        if available > 0 and credits is None:
            self.flash_status("fetching reset-credit details…", "dim")
            self._fetch_codex_reset_credits_for_dialog_bg()
            return
        dialog = CodexResetDialog(
            credits=list(credits.credits) if credits else [],
            available_count=available,
            on_reset=self._on_codex_reset_request,
        )
        self.push_screen(dialog, lambda _: None)

    def _fetch_codex_reset_credits_for_dialog_bg(self) -> None:
        self._chrome.fetch_codex_reset_credits_for_dialog_bg()

    def _on_codex_reset_request(self, credit_id: str) -> None:
        self._chrome.on_codex_reset_request(credit_id)

    def _consume_codex_reset_bg(self, credit_id: str) -> None:
        self._chrome.consume_codex_reset_bg(credit_id)

    def _on_codex_reset_consumed(self, result: ConsumeResetResult) -> None:
        self._chrome.on_codex_reset_consumed(result)

    def _on_codex_reset_consume_done(self) -> None:
        self._chrome.on_codex_reset_consume_done()

    def _has_codex_oauth_profile(self) -> bool:
        return self._chrome.has_codex_oauth_profile()

    def _refresh_codex_usage(self, *, force: bool = False) -> None:
        self._chrome.refresh_codex_usage(force=force)

    def _fetch_codex_usage_bg(self) -> None:
        self._chrome.fetch_codex_usage_bg()

    def _on_codex_usage_ready(self) -> None:
        self._chrome.on_codex_usage_ready()

    def _bottombar_thread_label(self) -> str:
        return self._chrome.bottombar_thread_label()

    def _bottombar_mode_label(self) -> str:
        return self._chrome.bottombar_mode_label()

    def register_bottombar_region(
        self,
        id: str,
        *,
        order: int | None = None,
        width: int | None = None,
        min_width: int | None = None,
        max_width: int | None = None,
        flex: int | None = None,
        align: BottomBarAlign | str | None = None,
        fg: str | None = None,
        bg: str | None = None,
        gap_after: int | None = None,
        priority: int | None = None,
        visible: bool | None = None,
        replace: bool = True,
    ) -> BottomBarRegionSpec:
        """Add or configure a freeform bottombar region (same model as topbar)."""
        return self._bottombar.register_region(
            id,
            order=order,
            width=width,
            min_width=min_width,
            max_width=max_width,
            flex=flex,
            align=align,
            fg=fg,
            bg=bg,
            gap_after=gap_after,
            priority=priority,
            visible=visible,
            replace=replace,
        )

    def unregister_bottombar_region(
        self, id: str, *, drop_components: bool = False
    ) -> bool:
        """Remove a bottombar region (optionally its components)."""
        return self._bottombar.unregister_region(id, drop_components=drop_components)

    def configure_bottombar_region(
        self,
        id: str,
        *,
        order: int | None = None,
        width: int | None = None,
        min_width: int | None = None,
        max_width: int | None = None,
        flex: int | None = None,
        align: BottomBarAlign | str | None = None,
        fg: str | None = None,
        bg: str | None = None,
        gap_after: int | None = None,
        priority: int | None = None,
        visible: bool | None = None,
    ) -> bool:
        """Update style/layout of an existing bottombar region."""
        return self._bottombar.set_region_style(
            id,
            order=order,
            width=width,
            min_width=min_width,
            max_width=max_width,
            flex=flex,
            align=align,
            fg=fg,
            bg=bg,
            gap_after=gap_after,
            priority=priority,
            visible=visible,
        )

    def register_bottombar_component(
        self,
        id: str,
        render: Any,
        *,
        region: BottomBarRegion | str = BottomBarRegion.RIGHT,
        order: int = 100,
        priority: int = 0,
        min_width: int = 0,
        gap_before: str = "  ·  ",
        style: str | None = None,
        visible: bool = True,
        replace: bool = True,
    ) -> BottomBarComponent:
        """Public extension point: add or replace a bottombar component.

        Same contract as ``register_topbar_component``: freeform region ids,
        ``order`` within the region, ``priority`` for shrink under width pressure.
        """
        return self._bottombar.register_fn(
            id,
            render,
            region=region,
            order=order,
            priority=priority,
            min_width=min_width,
            gap_before=gap_before,
            style=style,
            visible=visible,
            replace=replace,
        )

    def unregister_bottombar_component(self, id: str) -> bool:
        """Remove a previously registered bottombar component by id."""
        return self._bottombar.unregister(id)

    def set_bottombar_component_visible(self, id: str, visible: bool) -> bool:
        """Show or hide a bottombar component without unregistering it."""
        return self._bottombar.set_visible(id, visible)

    def set_bottombar_component_region(
        self,
        id: str,
        region: BottomBarRegion | str,
    ) -> bool:
        """Move a bottombar component to another horizontal region."""
        return self._bottombar.set_region(id, region)

    def set_bottombar_component_order(self, id: str, order: int) -> bool:
        """Change draw order within the component's region."""
        return self._bottombar.set_order(id, order)

    def _bottombar_usable_width(self) -> int:
        """Usable content width for the bottombar line (excludes CSS padding)."""
        width = max(int(getattr(self.size, "width", 0) or 0), 48)
        return max(16, width - 4)

    def _refresh_bottombar(self) -> None:
        """Paint the extensible bottombar under the prompt."""
        try:
            bar = self.query_one("#bottombar", Static)
        except Exception:  # noqa: BLE001
            return
        usable = self._bottombar_usable_width()
        # Left = model/mcp (accent blue); right = key hints (muted gray).
        # dim vs muted are both gray and look identical in screenshots.
        line = layout_bottombar_from_registry(
            self._bottombar,
            usable_width=usable,
            left_style=_styles._C_USER,
            center_style=_styles._C_ORANGE,
            right_style=_styles._C_MUTED,
            gap_style=_styles._C_MUTED,
        )
        if self._codex_bottombar_hovered:
            from synapse.ui.topbar.core import locate_component_span

            span = locate_component_span(
                self._bottombar, "codex_usage", usable_width=usable
            )
            if span is not None:
                start, width = span
                line.stylize("on #2a2d31", start, start + width)
        bar.update(line)

    def register_topbar_region(
        self,
        id: str,
        *,
        order: int | None = None,
        width: int | None = None,
        min_width: int | None = None,
        max_width: int | None = None,
        flex: int | None = None,
        align: TopBarAlign | str | None = None,
        fg: str | None = None,
        bg: str | None = None,
        gap_after: int | None = None,
        priority: int | None = None,
        visible: bool | None = None,
        replace: bool = True,
    ) -> TopBarRegionSpec:
        """Add or configure a freeform topbar region.

        Regions are horizontal slots (not limited to left/center/right).
        ``width`` is fixed cells; omit for hug-content. ``flex>0`` shares leftover
        row width. ``align`` is left/center/right inside the allocated band.
        ``fg`` / ``bg`` are Rich style colors (bg paints the whole region band).
        """
        return self._topbar.register_region(
            id,
            order=order,
            width=width,
            min_width=min_width,
            max_width=max_width,
            flex=flex,
            align=align,
            fg=fg,
            bg=bg,
            gap_after=gap_after,
            priority=priority,
            visible=visible,
            replace=replace,
        )

    def unregister_topbar_region(self, id: str, *, drop_components: bool = False) -> bool:
        """Remove a topbar region (optionally its components)."""
        return self._topbar.unregister_region(id, drop_components=drop_components)

    def configure_topbar_region(
        self,
        id: str,
        *,
        order: int | None = None,
        width: int | None = None,
        min_width: int | None = None,
        max_width: int | None = None,
        flex: int | None = None,
        align: TopBarAlign | str | None = None,
        fg: str | None = None,
        bg: str | None = None,
        gap_after: int | None = None,
        priority: int | None = None,
        visible: bool | None = None,
    ) -> bool:
        """Update style/layout of an existing region; returns False if missing."""
        return self._topbar.set_region_style(
            id,
            order=order,
            width=width,
            min_width=min_width,
            max_width=max_width,
            flex=flex,
            align=align,
            fg=fg,
            bg=bg,
            gap_after=gap_after,
            priority=priority,
            visible=visible,
        )

    def register_topbar_component(
        self,
        id: str,
        render: Any,
        *,
        region: TopBarRegion | str = TopBarRegion.RIGHT,
        order: int = 100,
        priority: int = 0,
        min_width: int = 0,
        gap_before: str = "  ·  ",
        style: str | None = None,
        visible: bool = True,
        replace: bool = True,
    ) -> TopBarComponent:
        """Public extension point: add or replace a topbar component.

        ``region`` is any region id (built-in left/center/right or a custom
        region created via ``register_topbar_region``). Unknown region ids are
        auto-created as hug-content slots. ``order`` controls position inside
        that region; ``priority`` controls shrink order when the row is narrow.
        """
        return self._topbar.register_fn(
            id,
            render,
            region=region,
            order=order,
            priority=priority,
            min_width=min_width,
            gap_before=gap_before,
            style=style,
            visible=visible,
            replace=replace,
        )

    def unregister_topbar_component(self, id: str) -> bool:
        """Remove a previously registered topbar component by id."""
        return self._topbar.unregister(id)

    def set_topbar_component_visible(self, id: str, visible: bool) -> bool:
        """Show or hide a topbar component without unregistering it."""
        return self._topbar.set_visible(id, visible)

    def set_topbar_component_region(
        self,
        id: str,
        region: TopBarRegion | str,
    ) -> bool:
        """Move a component to another horizontal region."""
        return self._topbar.set_region(id, region)

    def set_topbar_component_order(self, id: str, order: int) -> bool:
        """Change draw order within the component's region."""
        return self._topbar.set_order(id, order)

    def _topbar_usable_width(self) -> int:
        """Usable content width for the topbar line (excludes CSS padding)."""
        width = max(int(getattr(self.size, "width", 0) or 0), 48)
        pad = 0
        try:
            from synapse.ui.theme import get_theme

            pad = max(0, int(getattr(get_theme(), "top_pad_x", 0) or 0))
        except Exception:  # noqa: BLE001
            pad = 0
        # CSS ``padding: 0 $theme-top-pad-x`` subtracts pad on each side.
        return max(20, width - 2 * pad)

    def _keep_git_changes_popover(self) -> None:
        try:
            self.query_one("#topbar", TopBar).keep_open()
        except Exception:  # noqa: BLE001
            pass

    def _schedule_hide_git_changes_popover(self) -> None:
        try:
            self.query_one("#topbar", TopBar).schedule_hide()
        except Exception:  # noqa: BLE001
            pass

    def on_click(self, event: Click) -> None:
        # Outside click closes the branch changes popover.
        try:
            bar = self.query_one("#topbar", TopBar)
        except Exception:  # noqa: BLE001
            return
        if not bar.is_popover_open():
            return
        control = getattr(event, "control", None) or getattr(event, "widget", None)
        if bar.dismiss_if_outside(control):
            event.stop()

    @on(Click, "#bottombar")
    def _on_bottombar_click(self, event: Click) -> None:
        """Open Codex reset-credits dialog when the usage area is clicked."""
        if not self._has_codex_oauth_profile():
            return
        if self._codex.snapshot is None:
            return
        if not self._point_in_codex_span(int(event.x)):
            return
        self._open_codex_reset_dialog()

    @on(MouseMove)
    def _on_app_mouse_move(self, event: MouseMove) -> None:
        """Track whether the mouse hovers the Codex usage span."""
        widget = getattr(event, "widget", None) or getattr(event, "control", None)
        on_bottombar = widget is not None and getattr(widget, "id", "") == "bottombar"
        hovered = False
        if on_bottombar:
            hovered = self._point_in_codex_span(int(event.x))
        if hovered == self._codex_bottombar_hovered:
            return
        self._codex_bottombar_hovered = hovered
        self._refresh_bottombar()

    def _point_in_codex_span(self, x: int) -> bool:
        from synapse.ui.topbar.core import locate_component_span

        span = locate_component_span(
            self._bottombar, "codex_usage", usable_width=self._bottombar_usable_width()
        )
        return span is not None and span[0] <= x < span[0] + span[1]

    @on(TopBar.OpenGitExplore)
    def on_top_bar_open_git_explore(self, event: TopBar.OpenGitExplore) -> None:
        """Branch chrome / popover click → open Git Explore modal."""
        event.stop()
        self._open_git_explore(getattr(event, "path", None))

    def _open_git_explore(self, path: str | None = None) -> None:
        from synapse.ui.dialogs import GitExploreScreen

        try:
            bar = self.query_one("#topbar", TopBar)
            bar.dismiss()
        except Exception:  # noqa: BLE001
            pass

        ws = Path(getattr(self.settings, "workspace", Path.cwd()) or Path.cwd())
        branch = ""
        if self._git_chrome is not None and (self._git_chrome.name or "").strip():
            branch = self._git_chrome.name.strip()
            if self._git_chrome.dirty:
                branch = f"{branch} *"
        self.push_screen(
            GitExploreScreen(
                ws,
                initial_path=path,
                branch_label=branch or None,
                colors={
                    "dim": _styles._C_DIM,
                    "fg": _styles._C_FG,
                    "orange": _styles._C_ORANGE,
                    "added": _styles._C_GREEN,
                    "deleted": _styles._C_ERROR,
                    "hunk": _styles._C_USER,
                },
            ),
            self._on_git_explore_done,
        )

    def _on_git_explore_done(self, _result: object) -> None:
        # Refresh chrome after explore closes (user may have committed outside).
        self._refresh_git_chrome()

    def _refresh_topbar(self, tokens: str | None = None) -> None:
        self._chrome.refresh_topbar(tokens)

    def on_resize(self, event: object) -> None:  # noqa: ANN001
        del event
        self._refresh_topbar()
        self._render_status()
        self._refresh_bottombar()
        self._refresh_turn_rail()

    # -- status ----------------------------------------------------------

    def flash_status(
        self, message: str, style: str = "dim", *, ttl: float = 4.0
    ) -> None:
        self._status.flash_status(message, style, ttl=ttl)

    def _clear_status_notice(self) -> None:
        self._status.clear_status_notice()

    def _active_status_notice(self) -> str:
        return self._status._active_status_notice()

    def _emit_system_lines(
        self,
        lines: list[str] | tuple[str, ...] | None,
        *,
        error: bool = False,
        prefer_status: bool = True,
    ) -> None:
        """Route short system confirms to the status bar; dumps stay in log."""
        style = "yellow" if error else "dim"
        cleaned = [str(x).rstrip() for x in (lines or []) if str(x or "").strip()]
        if not cleaned:
            return
        # Multi-line dumps / help / tables belong in the transcript.
        if not prefer_status or len(cleaned) > 2:
            for line in cleaned:
                self.append_event(line, style)
            return
        total = sum(len(x) for x in cleaned)
        if total > 140 or any(len(x) > 120 for x in cleaned):
            for line in cleaned:
                self.append_event(line, style)
            return
        # Heuristic: list/table output (session list, model catalog, help sections).
        listish = 0
        for x in cleaned:
            s = x.lstrip()
            if s.startswith(("* ", "- ", "usage:", "note:", "config=")):
                listish += 1
            if " -> " in x and len(cleaned) > 1:
                listish += 1
        if listish:
            for line in cleaned:
                self.append_event(line, style)
            return
        msg = cleaned[0] if len(cleaned) == 1 else " · ".join(cleaned)
        self.flash_status(msg, style=style)

    def set_activity(self, phase: str, detail: str = "", reset_timer: bool = False) -> None:
        self._status.set_activity(phase, detail, reset_timer)

    def _resident_status_right(self) -> str:
        """Deprecated: model/mcp live on the bottombar now."""
        return ""

    def _idle_status_label(self) -> str:
        """Bottom status when idle (activity/notice only; model/mcp on bottombar)."""
        return ""

    def _status_notice_style_token(self) -> str:
        return self._status._status_notice_style_token()

    def _compose_status_left(
        self,
        *,
        busy: bool,
        elapsed: float,
        steer_n: int,
        left_budget: int,
    ) -> tuple[str, str]:
        return self._status._compose_status_left(
            busy=busy, elapsed=elapsed, steer_n=steer_n, left_budget=left_budget
        )

    def _render_status(self) -> None:
        self._status.render_status()

    def _bind_steer_queue(self) -> None:
        """Attach the UI listener to the queue owned by the current agent."""
        queue = self._turn_steer_queue()
        if queue is self._steer_bound_queue:
            if queue is not None:
                self._on_steer_items_changed(queue.peek_items())
            return

        old_queue = self._steer_bound_queue
        old_listener = self._steer_listener
        if old_queue is not None and old_listener is not None:
            old_queue.remove_listener(old_listener)

        self._steer_bound_queue = queue
        self._steer_listener = None
        if queue is None:
            self._on_steer_items_changed([])
            return

        def _on_change(items: list[str], *, source: SteerQueue = queue) -> None:
            def _apply(snapshot: list[str]) -> None:
                if self._steer_bound_queue is source:
                    self._on_steer_items_changed(snapshot)

            try:
                self.call_from_thread(_apply, list(items))
            except Exception:  # noqa: BLE001
                _apply(list(items))

        self._steer_listener = _on_change
        queue.add_listener(_on_change)
        self._on_steer_items_changed(queue.peek_items())

    def _turn_steer_queue(self) -> SteerQueue | None:
        """Return the queue consumed by the active graph run."""
        active_queue = getattr(self, "_active_steer_queue", None)
        if getattr(self, "_busy", False) and active_queue is not None:
            return active_queue
        return get_agent_steer_queue(self.agent)

    def _capture_turn_context(self) -> None:
        self._turn.capture_turn_context()

    def _clear_turn_context(self) -> None:
        self._turn.clear_turn_context()

    def _on_steer_items_changed(self, items: list[str]) -> None:
        self._status.on_steer_items_changed(items)

    def _sync_prompt_placeholder(self) -> None:
        self._status.sync_prompt_placeholder()

    def drop_steer_at(self, index: int) -> None:
        """UI: remove one pending steer note by index."""
        q = self._turn_steer_queue()
        if q is None:
            return
        q.remove_at(int(index))

    def clear_steer_queue(self) -> None:
        """UI: clear all pending steer notes."""
        q = self._turn_steer_queue()
        if q is None:
            return
        q.clear()

    def _tick_status(self) -> None:
        busy = self._status.tick()
        if busy:
            # Auto-open subagent monitor when DAG planning registers tasks
            # during an active turn — shows live status immediately.
            self._maybe_auto_open_subagent_monitor()
        else:
            self._maybe_show_session_recap()
        # Keep Thought "Thinking… Xs" / final seal clock honest between tokens.
        live = self._transcript.state.live_stream_block
        if isinstance(live, ThoughtBlock) and live.live:
            live.tick_live()

    def _maybe_auto_open_subagent_monitor(self) -> None:
        """Render inline subagent status in the main TUI during DAG execution."""
        monitor = getattr(self, "_subagent_monitor", None)
        if monitor is None:
            return
        _, runs = monitor.snapshot()
        status_widget = self.query_one("#subagent-status", Static)
        if not runs:
            self._subagent_status_text = ""
            status_widget.remove_class("visible")
            status_widget.update("")
            return
        counts: dict[str, int] = {}
        for r in runs:
            s = r.status or ""
            counts[s] = counts.get(s, 0) + 1
        parts: list[str] = ["Subagents:"]
        if counts.get("pending"):
            parts.append(f"\u25a1 {counts['pending']} pending")
        if counts.get("running"):
            parts.append(f"\u26a1 {counts['running']} running")
        if counts.get("ok"):
            parts.append(f"\u2713 {counts['ok']} done")
        if counts.get("error"):
            parts.append(f"\u2717 {counts['error']} error")
        text = "  ".join(parts)
        if text != getattr(self, "_subagent_status_text", ""):
            self._subagent_status_text = text
            status_widget.update(text)
            status_widget.add_class("visible")
        # Keep the dialog auto-open as a fallback for the first detection
        # in a turn — then the inline bar takes over for updates.
        if not getattr(self, "_subagent_monitor_auto_opened", False):
            active = any(r.status in {"pending", "running"} for r in runs)
            if active:
                self._subagent_monitor_auto_opened = True
                self._open_subagent_monitor()

    def _clear_subagent_status(self) -> None:
        """Clear the inline subagent status bar (called on turn reset)."""
        self._subagent_status_text = ""
        try:
            w = self.query_one("#subagent-status", Static)
            w.remove_class("visible")
            w.update("")
        except Exception:  # noqa: BLE001
            pass

    # -- transcript controller forwarding ---------------------------------

    def set_stream(self, kind: str, body: str, elapsed_s: float = 0.0) -> None:
        self._transcript.set_stream(kind, body, elapsed_s)

    def clear_stream(self) -> None:
        self._transcript.clear_stream()

    def _follow_timeline_if_needed(self) -> None:
        self._transcript._follow_timeline_if_needed()

    def _show_welcome(self) -> None:
        self._transcript._show_welcome()

    def _dismiss_welcome(self) -> None:
        self._transcript._dismiss_welcome()

    def _mount_block(self, block: Any, *, dismiss_welcome: bool = True) -> None:
        self._transcript._mount_block(block, dismiss_welcome=dismiss_welcome)

    def _scroll_timeline(self) -> None:
        self._transcript._scroll_timeline()

    def _mount_markdown_block(self, text: str) -> None:
        self._transcript._mount_markdown_block(text)

    def append_user(
        self,
        text: str,
        images: list[Any] | None = None,
        *,
        full_text: str | None = None,
    ) -> None:
        self._transcript.append_user(text, images, full_text=full_text)

    def _refresh_turn_rail(self) -> None:
        self._transcript._refresh_turn_rail()

    def jump_to_user_turn(self, target: UserTurnBlock) -> None:
        self._transcript.jump_to_user_turn(target)

    def action_copy_selection(self) -> None:
        self._transcript.action_copy_selection()

    def action_copy_last_answer(self) -> None:
        self._transcript.action_copy_last_answer()

    def _get_last_answer_body(self) -> str:
        return self._transcript._get_last_answer_body()

    def _copy_text_to_clipboard(self, text: str, *, label: str = "text") -> None:
        self._transcript._copy_text_to_clipboard(text, label=label)

    def on_mouse_up(self, event: MouseUp) -> None:
        self._transcript.on_mouse_up(event)

    def _auto_copy_selection(self) -> None:
        self._transcript._auto_copy_selection()

    def commit_thought(self, elapsed_s: float, body: str) -> None:
        self._transcript.commit_thought(elapsed_s, body)

    def action_toggle_last_thought(self) -> None:
        self._transcript.action_toggle_last_thought()

    def action_toggle_last_tools(self) -> None:
        self._transcript.action_toggle_last_tools()

    def commit_answer(self, text: str) -> None:
        self._transcript.commit_answer(text)

    def _mount_answer_divider(self) -> None:
        self._transcript._mount_answer_divider()

    def _render_live_tools(self) -> None:
        self._transcript._render_live_tools()

    def _tool_details_expanded(self) -> bool:
        return self._transcript._tool_details_expanded()

    def _commit_live_tools_to_log(self) -> None:
        self._transcript._commit_live_tools_to_log()

    def write_tool_group_header(self, summary: str, collapsed: bool = True) -> None:
        self._transcript.write_tool_group_header(summary, collapsed)

    def update_tool_group_header(self, summary: str) -> None:
        self._transcript.update_tool_group_header(summary)

    def write_tool_item(self, item: ToolItem) -> None:
        self._transcript.write_tool_item(item)

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
        self._transcript.update_tool_item(
            item_id,
            status=status,
            preview=preview,
            error=error,
            label=label,
            path=path,
            name=name,
            category=category,
        )

    def write_tool_preview(
        self, item_id: str, preview: str, *, error: bool = False
    ) -> None:
        self._transcript.write_tool_preview(item_id, preview, error=error)

    def close_tool_group(self) -> None:
        self._transcript.close_tool_group()

    def append_meta(self, message: str) -> None:
        self._transcript.append_meta(message)

    def append_event(self, message: str, style: str = "dim") -> None:
        self._transcript.append_event(message, style)

    def action_cancel_run(self) -> None:
        """ESC: abort the in-flight agent loop so the user can start a new turn."""
        if isinstance(self.screen, ModalScreen):
            return
        if not self._busy:
            return
        # Idempotent: repeated ESC only re-asserts the cancel flag.
        if self._compacting_context:
            self.append_event("上下文压缩正在执行，当前无法安全取消。", "yellow")
            return
        self._cancel_event.set()
        self._pause_goal_for_interrupt()
        self.set_activity("idle", "cancelling…", True)
        self.append_event("正在终止当前任务… (Esc)", "yellow")

    def _pause_goal_for_interrupt(self) -> None:
        """Pause the current active goal when Esc interrupts a turn."""
        service = getattr(self.agent, "_coding_goal_service", None)
        thread_id = self.thread_id
        if service is None or not thread_id:
            return
        try:
            from synapse.goals.model import ThreadGoalStatus

            goal = service.get(thread_id)
            if goal is None or goal.status != ThreadGoalStatus.ACTIVE:
                return
            service.pause_goal(thread_id)
        except Exception:  # noqa: BLE001 - cancellation must remain reliable
            pass

    def on_key(self, event: Key) -> None:
        # When a modal dialog is open, let it handle keys exclusively.
        if isinstance(self.screen, ModalScreen):
            return
        # Backup path if a child widget swallows Escape before bindings fire.
        if event.key == "escape" and self._busy:
            self.action_cancel_run()
            event.stop()
            event.prevent_default()

    def _clear_transcript_state(self) -> None:
        """Release all Python-side references to the current transcript."""
        self._transcript.reset_all()
        self._image_bank.clear()
        self._prompt.clear_paste_replacements()
        self._subagent_monitor.reset()
        self._subagent_monitor_auto_opened = False
        self._subagent_status_text = ""
        # Drop paginated-history state together with the DOM.
        self._history.state.reset()
        self._session_recap.reset()

    # -- transcript history controller forwarding -------------------------

    async def _reset_transcript_async(
        self,
        *,
        reload_transcript: bool = False,
        announce: bool = False,
        generation: int | None = None,
    ) -> None:
        await self._history.reset_transcript_async(
            reload_transcript=reload_transcript,
            announce=announce,
            generation=generation,
        )

    def _schedule_transcript_reset(
        self,
        *,
        reload_transcript: bool = False,
        announce: bool = False,
    ) -> None:
        self._history.schedule_transcript_reset(
            reload_transcript=reload_transcript,
            announce=announce,
        )

    def _restore_session_transcript(self, *, announce: bool = True) -> None:
        self._history.restore_session_transcript(announce=announce)

    @work(thread=True, exclusive=True, group="transcript-migration")
    def _migrate_transcript_projection_bg(
        self,
        thread_id: str,
        generation: int,
        announce: bool,
    ) -> None:
        self._history.migrate_transcript_projection_bg(thread_id, generation, announce)

    def _transcript_migration_done(
        self,
        thread_id: str,
        generation: int,
        announce: bool,
        success: bool,
        error: str | None,
    ) -> None:
        self._history.transcript_migration_done(
            thread_id, generation, announce, success, error
        )

    def _paint_restored_transcript(self, page: Any, *, announce: bool) -> None:
        self._history.paint_restored_transcript(page, announce=announce)

    def _check_history_edge(self) -> None:
        self._history.check_history_edge()

    def _request_earlier_history(self) -> None:
        self._history.request_earlier_history()

    @work(thread=True, exclusive=True, group="history")
    def _load_earlier_history_bg(
        self,
        before_turn: int,
        tail_turns: int,
        thread_id: str,
        generation: int,
    ) -> None:
        self._history.load_earlier_history_bg(
            before_turn, tail_turns, thread_id, generation
        )

    def _history_load_done(
        self,
        page: Any,
        expected_turn: int,
        thread_id: str,
        generation: int,
        error: str | None,
    ) -> None:
        self._history.history_load_done(
            page, expected_turn, thread_id, generation, error
        )

    def _trim_mounted_history_pages(self) -> None:
        self._history.trim_mounted_history_pages()

    def _insert_earlier_blocks(self, blocks: list[Any]) -> None:
        self._history.insert_earlier_blocks(blocks)

    def _discard_earlier_blocks(self, blocks: list[Any]) -> None:
        self._history.discard_earlier_blocks(blocks)

    def _prepend_blocks(self, blocks: list[Any]) -> bool:
        return self._history.prepend_blocks(blocks)

    def _keep_scroll_after_prepend(self, old_max: int, old_y: float) -> None:
        self._history.keep_scroll_after_prepend(old_max, old_y)

    def _build_restored_tool_group(
        self,
        tool_calls: list[dict],
        tool_results: list[dict],
    ) -> tuple[Any, bool]:
        return self._history.build_restored_tool_group(tool_calls, tool_results)

    def _build_answer_divider(self) -> Any:
        return self._history.build_answer_divider()

    def _build_restored_blocks(self, events: list[Any]) -> list[Any]:
        return self._history.build_restored_blocks(events)

    def _mount_blocks(self, blocks: list[Any]) -> None:
        self._history.mount_blocks(blocks)

    def action_clear_log(self) -> None:
        self._schedule_transcript_reset()

    def action_open_selectable_view(self) -> None:
        """Open a full-conversation plain-text view for mouse selection & copy."""
        from synapse.ui.selectable_text import (
            SelectableTextModal,
            build_transcript_from_log,
        )

        try:
            log = self.query_one("#log", VerticalScroll)
            transcript = build_transcript_from_log(log)
        except Exception:  # noqa: BLE001
            transcript = "(empty)"

        if not transcript.strip():
            self.append_event("nothing to show", "dim")
            return

        self.push_screen(
            SelectableTextModal(transcript, char_count=len(transcript))
        )

    # -- theme -----------------------------------------------------------

    def apply_theme(
        self,
        name: str | None = None,
        *,
        persist: bool = False,
        announce: bool = False,
    ) -> str:
        """Activate a theme at runtime (CSS variables + Rich paint slots).

        Also switches Textual ``App.theme`` so surface/panel match the palette
        (required for transparent ``ansi``; solid dark/light shells otherwise).
        """
        from synapse.ui.theme import apply_textual_theme, get_theme, set_theme

        theme = set_theme(
            name or getattr(self.settings, "theme", None),
            workspace=self.project_root,
            persist=persist,
            scope="user",
        )
        try:
            self.settings.theme = theme.name
        except Exception:  # noqa: BLE001
            pass
        # Drive Textual surface/panel (default textual-dark paints opaque black
        # and would hide terminal acrylic under $theme-bg=transparent).
        try:
            apply_textual_theme(self, theme)
        except Exception:  # noqa: BLE001
            pass
        # refresh_css() calls get_css_variables() which returns the active
        # theme palette; this triggers a full reparse + re-apply of all
        # $theme-* variables across every widget.
        try:
            self.refresh_css(animate=False)
        except Exception:  # noqa: BLE001
            pass
        self._repaint_themed_widgets()
        if announce:
            self.flash_status(f"theme: {theme.name} ({theme.label})", "dim")
        return get_theme().name

    def _repaint_themed_widgets(self) -> None:
        """Re-render widgets that baked colors into Rich Text."""
        for cls_name, method in (
            ("WelcomeView", "refresh_logo"),
            ("UserTurnBlock", "_render_block"),
            ("ThoughtBlock", "_render_block"),
            ("ToolGroupBlock", "_render_block"),
            ("TodoChecklist", "_render_block"),
            ("AnswerBlock", "_render_block"),
            ("_MarkdownBlock", "repaint_markdown"),
            ("AnswerDivider", "_render_block"),
            ("TurnRailItem", "_show_bar"),
        ):
            try:
                for widget in self.query(cls_name):
                    fn = getattr(widget, method, None)
                    if callable(fn):
                        fn()
            except Exception:  # noqa: BLE001
                continue
        try:
            steer = self.query_one("#steer-queue", SteerQueueWidget)
            paint = getattr(steer, "_paint_block", None)
            if callable(paint):
                paint()
        except Exception:  # noqa: BLE001
            pass
        try:
            # Region band colors track the active palette.
            self._apply_topbar_region_bands()
            self._refresh_topbar()
            self._render_status()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.refresh(layout=False)
        except Exception:  # noqa: BLE001
            pass

    # -- dialogs ----------------------------------------------------------






    @work(thread=True, exclusive=True, group="model-switch")
    def _switch_model_bg(self, command: str, activity: str) -> None:
        """Run /model rebuild off the UI thread so the TUI stays responsive."""
        from synapse.commands.slash_cmds import handle_slash
        from synapse.observability.startup_trace import duration

        switch_started = time.perf_counter()
        self.call_from_thread(self._clear_status_notice)
        self.call_from_thread(self.set_activity, "switching", activity, True)
        try:
            ok = handle_slash(
                command,
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            duration("model.switch", switch_started, command=command, success=False)
            self.call_from_thread(
                self.append_event, f"{activity} failed: {exc}", "yellow"
            )
            self.call_from_thread(self.set_activity, "idle", "", True)
            return
        duration(
            "model.switch",
            switch_started,
            command=command,
            success=not bool(getattr(ok, "error", False)),
        )
        self.call_from_thread(self._apply_ok_result, ok, 1.5)
        self.call_from_thread(self.set_activity, "idle", "", True)
        if getattr(ok, "mcp_attach_pending", False):
            self.call_from_thread(self._attach_mcp_after_switch)

    def _attach_mcp_after_switch(self) -> None:
        if self._mcp_attaching or self.agent is None:
            return
        self._mcp_attaching = True
        self._attach_mcp_after_switch_bg(self.agent)

    @work(thread=True, exclusive=True, group="model-switch-mcp")
    def _attach_mcp_after_switch_bg(self, base_agent: Any) -> None:
        from synapse.app.agent import attach_mcp_to_agent
        from synapse.observability.startup_trace import duration

        mcp_started = time.perf_counter()
        self.call_from_thread(self.flash_status, "reconnecting MCP…", "dim", ttl=1.5)
        try:
            agent = attach_mcp_to_agent(
                self.settings,
                base_agent,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.append_event,
                f"MCP reconnect failed (agent still usable): {exc}",
                "yellow",
            )
            return
        finally:
            duration("mcp.attach", mcp_started, phase="model_switch")
            self._mcp_attaching = False
        if self.agent is not base_agent:
            return
        self.agent = agent
        self.call_from_thread(self._bind_steer_queue)
        self.call_from_thread(self.flash_status, "MCP reconnected", "dim", ttl=1.5)







    @work(thread=True, exclusive=True, group="codex-import")
    def _import_codex_session_bg(self, native_id: str) -> None:
        """Seed one Codex text snapshot, then switch through the normal session path."""
        turn_agent = self._active_turn_agent or self.agent
        if not self._agent_ready.wait(timeout=180) or turn_agent is None:
            self.call_from_thread(
                self.append_event,
                "Codex import unavailable: agent is still starting",
                "yellow",
            )
            self.call_from_thread(self._turn_done)
            return
        try:
            from synapse.integrations.codex_import import import_codex_session

            result = import_codex_session(
                native_id=native_id,
                settings=self.settings,
                agent=turn_agent,
                workspace=Path(self.settings.workspace),
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.append_event, f"Codex import failed: {exc}", "yellow")
        else:
            self.call_from_thread(self._finish_codex_import, result)
        finally:
            self.call_from_thread(self._turn_done)









    def should_suppress_dag_task_tool_group(self, calls: list[dict]) -> bool:
        """Suppress the parent ``task`` tool group when the subagent monitor
        is tracking matching runs.

        Called by TextualStreamSink before rendering a tool batch."""
        if not calls:
            return False
        if not all(c.get("name") == "task" for c in calls):
            return False
        monitor = getattr(self, "_subagent_monitor", None)
        if monitor is None:
            return False
        _, runs = monitor.snapshot()
        known_ids = {r.call_id for r in runs if r.status in {"pending", "running"}}
        if not known_ids:
            return False
        return all(c.get("id") in known_ids for c in calls)

    def sync_subagent_monitor_block(self, *, force: bool = False) -> None:
        """Show an in-turn subagent status indicator when DAG tasks are detected.

        On first call per turn (force=False), auto-opens the subagent monitor
        dialog so the user can see live task planning and progress immediately.
        When opened manually via /subagents, force=True refreshes the dialog."""
        monitor = getattr(self, "_subagent_monitor", None)
        if monitor is None:
            return
        _, runs = monitor.snapshot()
        if not runs:
            return
        if not force and getattr(self, "_subagent_monitor_auto_opened", False):
            return
        # Auto-open on first detection (the dialog polls and auto-refreshes).
        if not force:
            self._subagent_monitor_auto_opened = True
        self._open_subagent_monitor()


    def _apply_mcp_server_toggle(self, server_name: str) -> None:
        """Temporarily toggle one MCP server through the existing slash handler."""
        if getattr(self, "_mcp_reloading", False):
            return
        self._mcp_reloading = True
        self.set_activity("switching", f"toggling MCP server {server_name}\u2026", True)
        self._apply_mcp_server_toggle_bg(server_name)

    @work(thread=True, exclusive=True, group="mcp-reload")
    def _apply_mcp_server_toggle_bg(self, server_name: str) -> None:
        from synapse.commands.slash_cmds import handle_slash
        from synapse.observability.startup_trace import duration

        reload_started = time.perf_counter()
        try:
            ok = handle_slash(
                f"/mcp toggle {server_name}",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            duration("mcp.toggle", reload_started, success=False)
            self.call_from_thread(
                self.append_event, f"MCP server toggle failed: {exc}", "yellow"
            )
            self.call_from_thread(self.set_activity, "idle", "", True)
            self._mcp_reloading = False
            return
        duration(
            "mcp.toggle", reload_started, success=not bool(getattr(ok, "error", False))
        )
        self.call_from_thread(self._apply_ok_result, ok)
        self.call_from_thread(self.set_activity, "idle", "", True)
        self._mcp_reloading = False


    @work(thread=True, exclusive=True, group="mcp-save")
    def _apply_mcp_save_bg(self, to_save: dict[str, list[str] | None]) -> None:
        from synapse.commands.slash_cmds import handle_slash
        from synapse.integrations.mcp_client import load_mcp_server_configs
        from synapse.observability.startup_trace import duration
        from synapse.ui.dialogs.mcp_panel import _save_include_tools_to_config

        save_started = time.perf_counter()
        # 1. Write include_tools to config file for each changed server.
        for server_name, include_tools in to_save.items():
            try:
                _save_include_tools_to_config(
                    self.settings,
                    server_name,
                    include_tools,
                    self.project_root,
                )
            except Exception:  # noqa: BLE001
                pass

        # 2. Reload in-memory settings from the updated config files.
        try:
            fresh = load_mcp_server_configs(
                path=getattr(self.settings, "mcp_config_path", None),
                workspace=getattr(self.settings, "workspace", None),
            )
            import json

            raw = {
                "servers": [
                    {
                        "name": s.name,
                        "transport": s.transport,
                        "command": s.command,
                        "args": s.args,
                        "env": s.env,
                        "url": s.url,
                        "headers": s.headers,
                        "enabled": s.enabled,
                        "tool_prefix": s.tool_prefix,
                        "include_tools": s.include_tools,
                        "exclude_tools": s.exclude_tools,
                    }
                    for s in fresh
                ]
            }
            self.settings.mcp_servers_json = json.dumps(raw)
        except Exception:  # noqa: BLE001
            pass

        # 3. Reload the agent with updated MCP tools.
        try:
            ok = handle_slash(
                "/mcp reload",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            duration("mcp.save", save_started, success=False)
            self.call_from_thread(
                self.append_event, f"MCP save/reload failed: {exc}", "yellow"
            )
            self.call_from_thread(self.set_activity, "idle", "", True)
            self._mcp_reloading = False
            return
        duration(
            "mcp.save", save_started, success=not bool(getattr(ok, "error", False))
        )
        self.call_from_thread(self._apply_ok_result, ok)
        self.call_from_thread(self.set_activity, "idle", "", True)
        self._mcp_reloading = False


    @work(thread=True, exclusive=True, group="mcp-reload")
    def _apply_mcp_reload_bg(self) -> None:
        from synapse.commands.slash_cmds import handle_slash
        from synapse.observability.startup_trace import duration

        reload_started = time.perf_counter()
        try:
            ok = handle_slash(
                "/mcp reload",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            duration("mcp.reload", reload_started, success=False)
            self.call_from_thread(
                self.append_event, f"MCP reload failed: {exc}", "yellow"
            )
            self.call_from_thread(self.set_activity, "idle", "", True)
            self._mcp_reloading = False
            return
        duration(
            "mcp.reload", reload_started, success=not bool(getattr(ok, "error", False))
        )
        self.call_from_thread(self._apply_ok_result, ok)
        self.call_from_thread(self.set_activity, "idle", "", True)
        self._mcp_reloading = False



    def _apply_ok_result(self, ok: object, notice_ttl: float = 4.0) -> None:
        controller = getattr(self, "_slash", None)
        if controller is None:
            controller = SlashController(self)
            self.__dict__["_slash"] = controller
        controller.apply_ok_result(ok, notice_ttl)

    # -- input / turn ----------------------------------------------------


    @work(thread=True, exclusive=True, group="context-compact")
    def _compact_context_bg(self, agent: Any, thread_id: str) -> None:
        """Execute the model-backed /compact command away from Textual's UI loop."""
        from synapse.commands.slash_cmds import handle_slash

        try:
            result = handle_slash(
                "/compact",
                settings=self.settings,
                agent=agent,
                thread_id=thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.append_event, f"compact failed: {exc}", "bold red")
        else:
            self.call_from_thread(self._finish_context_compact, result)
        finally:
            self.call_from_thread(self._complete_context_compact)



    # -- slash/dialog controller forwarding --------------------------------

    def _open_compression_diagnostics(self) -> None:
        self._slash.open_compression_diagnostics()

    def _open_model_dialog(self, _args: list[str]) -> None:
        self._slash.open_model_dialog(_args)

    def _on_model_dialog_done(self, result: object) -> None:
        self._slash.on_model_dialog_done(result)

    def _apply_model_switch(self, alias: str) -> None:
        self._slash.apply_model_switch(alias)

    def _apply_thinking_switch(self, level: str) -> None:
        self._slash.apply_thinking_switch(level)

    def _open_theme_dialog(self) -> None:
        self._slash.open_theme_dialog()

    def _open_theme_designer(self) -> None:
        self._slash.open_theme_designer()

    def _on_theme_dialog_done(self, result: object) -> None:
        self._slash.on_theme_dialog_done(result)

    def _open_codex_import_dialog(self) -> None:
        self._slash.open_codex_import_dialog()

    def _on_codex_import_dialog_done(self, result: object) -> None:
        self._slash.on_codex_import_dialog_done(result)

    def _start_codex_import(self, native_id: str) -> None:
        self._slash.start_codex_import(native_id)

    def _finish_codex_import(self, result: Any) -> None:
        self._slash.finish_codex_import(result)

    def _open_session_dialog(self, parts: list[str]) -> None:
        self._slash.open_session_dialog(parts)

    def _on_session_dialog_done(self, result: object) -> None:
        self._slash.on_session_dialog_done(result)

    def _apply_session_multi_delete(self, thread_ids: list[str]) -> None:
        self._slash.apply_session_multi_delete(thread_ids)

    def _apply_session_switch(self, thread_id: str) -> None:
        self._slash.apply_session_switch(thread_id)

    def _apply_session_delete(self, thread_id: str) -> None:
        self._slash.apply_session_delete(thread_id)

    def _open_mcp_dialog(self) -> None:
        self._slash.open_mcp_dialog()

    def _open_subagent_monitor(self) -> None:
        self._slash.open_subagent_monitor()

    def _on_mcp_dialog_done(self, result: object) -> None:
        self._slash.on_mcp_dialog_done(result)

    def _apply_mcp_save(self, to_save: dict[str, list[str] | None]) -> None:
        self._slash.apply_mcp_save(to_save)

    def _apply_mcp_reload(self) -> None:
        self._slash.apply_mcp_reload()

    def _open_safety_dialog(self) -> None:
        self._slash.open_safety_dialog()

    def _on_safety_dialog_done(self, result: object) -> None:
        self._slash.on_safety_dialog_done(result)

    def _start_context_compact(self) -> None:
        self._slash.start_context_compact()

    def _finish_context_compact(self, result: Any) -> None:
        self._slash.finish_context_compact(result)

    def _complete_context_compact(self) -> None:
        self._slash.complete_context_compact()

    def _handle_slash(self, text: str) -> bool:
        return self._slash.handle_slash(text)

    # -- turn controller forwarding -------------------------------------------

    @on(Input.Submitted, "#prompt")
    def handle_submit(self, event: Input.Submitted) -> None:
        self._turn.submit(event)

    @work(thread=True, exclusive=True)
    def run_turn(self, text: str, attachments: list[Any] | None = None) -> None:
        self._turn.run_turn(text, attachments)

    @work(thread=True, exclusive=True)
    def run_resume(self, action: str, message: str | None = None) -> None:
        self._turn.run_resume(action, message)

    def _apply_stream_result(
        self,
        result: Any,
        *,
        transcript_generation: int | None,
        resume: bool = False,
    ) -> bool:
        return self._turn.apply_stream_result(
            result,
            transcript_generation=transcript_generation,
            resume=resume,
        )

    def _turn_done(self) -> None:
        controller = getattr(self, "_turn", None)
        if controller is None:
            controller = TurnController(self)
            self.__dict__["_turn"] = controller
        controller.turn_done()

    def _settle_goal_turn(self, completed_queue: SteerQueue | None) -> None:
        self._turn.settle_goal_turn(completed_queue)

    def _maybe_continue_goal(self, queue: SteerQueue | None = None) -> bool:
        controller = getattr(self, "_turn", None)
        if controller is None:
            controller = TurnController(self)
            self.__dict__["_turn"] = controller
        return controller.maybe_continue_goal(queue)

    def _note_session_recap_turn(self) -> None:
        self._turn.note_session_recap_turn()

    def _persist_transcript_turn(self, *, user_text: str) -> None:
        self._turn.persist_transcript_turn(user_text=user_text)

    def _persist_turn_summary(self, *, user_text: str) -> None:
        self._turn.persist_turn_summary(user_text=user_text)

    def _project_session_into_catalog(self) -> None:
        self._turn.project_session_into_catalog()

    def _prompt_has_draft(self) -> bool:
        return self._turn.prompt_has_draft()

    def _maybe_show_session_recap(self) -> None:
        self._turn.maybe_show_session_recap()

    def _schedule_followup_steer(self, queue: SteerQueue | None) -> bool:
        controller = getattr(self, "_turn", None)
        if controller is None:
            controller = TurnController(self)
            self.__dict__["_turn"] = controller
        return controller.schedule_followup_steer(queue)

    def _start_followup_steer(
        self,
        queue: SteerQueue,
        scheduled_cancel_event: threading.Event | None = None,
    ) -> None:
        controller = getattr(self, "_turn", None)
        if controller is None:
            controller = TurnController(self)
            self.__dict__["_turn"] = controller
        controller.start_followup_steer(queue, scheduled_cancel_event)

    def _maybe_followup_steer(self, queue: SteerQueue | None = None) -> None:
        self._turn.maybe_followup_steer(queue)

    def attach_project_catalog(self, catalog: Any) -> None:
        """Wire the user-layer catalog opened by ``run_tui`` (optional)."""
        self._project_catalog = catalog