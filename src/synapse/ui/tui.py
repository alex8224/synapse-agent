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
from textual.events import Click, Key, MouseUp
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from synapse.app.agent import build_coding_agent
from synapse.content.input_history import InputHistory
from synapse.content.multimodal import (
    ImageBank,
    compose_user_content,
    find_placeholders,
    provider_from_settings,
    read_clipboard,
)
from synapse.runtime.steer import SteerQueue, format_steer_message, get_agent_steer_queue
from synapse.sessions.session_recap import SessionRecapController
from synapse.subagent_monitor import MONITOR_CONFIG_KEY, SubagentMonitor
from synapse.tool_output.metrics import clear_metrics_notifier, set_metrics_notifier
from synapse.tool_output.repository import ToolOutputRepository
from synapse.ui.answer_divider import AnswerDivider
from synapse.ui.bottombar import (
    BottomBarAlign,
    BottomBarComponent,
    BottomBarContext,
    BottomBarRegion,
    BottomBarRegionSpec,
    BottomBarRegistry,
)
from synapse.ui.bottombar import (
    install_default_components as install_default_bottombar_components,
)
from synapse.ui.bottombar import (
    layout_from_registry as layout_bottombar_from_registry,
)
from synapse.ui.clipboard import copy_to_clipboard
from synapse.ui.formatters import (
    format_answer_divider as _format_answer_divider,
)
from synapse.ui.formatters import (
    format_byte_count,
    format_context_occupancy_label,
    format_mcp_status_label,
    format_token_count,
    model_status_label,
    short_workspace_label,
    soften_turn_footer,
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
from synapse.ui.selectable_static import SelectableStatic as _SelectableStatic
from synapse.ui.selectable_static import (
    _annotate_strip_offsets as _annotate_strip_offsets_impl,
)
from synapse.ui.selectable_static import (
    _stylize_strip_char_span as _stylize_strip_char_span_impl,
)
from synapse.ui.steer_widget import SteerQueueWidget
from synapse.ui.stream import extract_last_ai_text, render_markdown, stream_agent
from synapse.ui.textual_stream_sink import TextualStreamSink
from synapse.ui.timeline import TODO_MARK_ACTIVE as _TODO_MARK_ACTIVE
from synapse.ui.timeline import TODO_MARK_DONE as _TODO_MARK_DONE
from synapse.ui.timeline import TODO_MARK_PENDING as _TODO_MARK_PENDING
from synapse.ui.timeline import TodoRow as _TodoRow
from synapse.ui.timeline import ToolItem, summarize_items
from synapse.ui.timeline import is_todo_tool as _is_todo_tool
from synapse.ui.timeline import parse_todo_preview_lines as _parse_todo_preview_lines
from synapse.ui.tool_blocks import TodoChecklist as _TodoChecklist
from synapse.ui.tool_blocks import ToolGroupBlock
from synapse.ui.tool_blocks import (
    render_todo_checklist_from_preview as _render_todo_checklist_from_preview,
)
from synapse.ui.tool_blocks import render_todo_row_texts as _render_todo_row_texts
from synapse.ui.tool_blocks import todo_kind_style as _todo_kind_style
from synapse.ui.topbar import (
    TopBarAlign,
    TopBarComponent,
    TopBarContext,
    TopBarRegion,
    TopBarRegionSpec,
    TopBarRegistry,
    install_default_components,
    layout_from_registry,
    truncate_to_width,
)
from synapse.ui.topbar import (
    display_width as _display_width,
)
from synapse.ui.topbar.git_chrome import (
    GitBranchChrome,
    probe_git_branch_chrome,
    render_branch_chrome,
)
from synapse.ui.topbar.widget import TopBar
from synapse.ui.transcript_blocks import AnswerBlock, ThoughtBlock
from synapse.ui.turn_rail import format_turn_rail_bucket_label as _format_turn_rail_bucket_label
from synapse.ui.turn_rail import format_turn_rail_preview
from synapse.ui.turn_rail import turn_rail_tick_slots as _turn_rail_tick_slots
from synapse.ui.turn_rail_widgets import TurnRail
from synapse.ui.turn_rail_widgets import TurnRailGap as _TurnRailGap
from synapse.ui.turn_rail_widgets import TurnRailItem as _TurnRailItem
from synapse.ui.user_turn import format_user_turn_meta as _format_user_turn_meta
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
TodoRow = _TodoRow
TODO_MARK_ACTIVE = _TODO_MARK_ACTIVE
TODO_MARK_DONE = _TODO_MARK_DONE
TODO_MARK_PENDING = _TODO_MARK_PENDING
is_todo_tool = _is_todo_tool
parse_todo_preview_lines = _parse_todo_preview_lines
display_width = _display_width
_annotate_strip_offsets = _annotate_strip_offsets_impl
_stylize_strip_char_span = _stylize_strip_char_span_impl

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


# Palette slots — kept as module globals so render paths stay cheap.
# Values track ``synapse.ui.theme.get_theme()`` via ``_sync_theme_colors``.
_C_FG = "#e8eaed"
_C_DIM = "#9aa0a6"
_C_MUTED = "#5f6368"
_C_GREEN = "#81c995"
_C_ORANGE = "#f4b183"
_C_BAR = "#2b2d31"
_C_BG = "#1a1b1e"
_C_TOP = "#121316"
_C_USER = "#8ab4f8"
_C_ERROR = "#f28b82"
_C_BORDER = "#3c4043"
_C_BORDER_FOCUS = "#5f6368"
_CODE_THEME = "monokai"


def _sync_theme_colors(theme: object | None = None) -> None:
    """Copy active theme palette into module-level color slots."""
    global _C_FG, _C_DIM, _C_MUTED, _C_GREEN, _C_ORANGE, _C_BAR, _C_BG, _C_TOP
    global _C_USER, _C_ERROR, _C_BORDER, _C_BORDER_FOCUS, _CODE_THEME
    try:
        from synapse.ui.theme import get_theme

        t = theme or get_theme()
    except Exception:  # noqa: BLE001
        return
    _C_FG = str(getattr(t, "fg", _C_FG))
    _C_DIM = str(getattr(t, "dim", _C_DIM))
    _C_MUTED = str(getattr(t, "muted", _C_MUTED))
    _C_GREEN = str(getattr(t, "green", _C_GREEN))
    _C_ORANGE = str(getattr(t, "orange", _C_ORANGE))
    _C_BAR = str(getattr(t, "bar", _C_BAR))
    _C_BG = str(getattr(t, "bg", _C_BG))
    _C_TOP = str(getattr(t, "top", _C_TOP))
    _C_USER = str(getattr(t, "user", _C_USER))
    _C_ERROR = str(getattr(t, "error", _C_ERROR))
    _C_BORDER = str(getattr(t, "border", _C_BORDER))
    _C_BORDER_FOCUS = str(getattr(t, "border_focus", _C_BORDER_FOCUS))
    _CODE_THEME = str(getattr(t, "code_theme", _CODE_THEME) or "monokai")


try:
    from synapse.ui.theme import on_theme_change

    on_theme_change(_sync_theme_colors)
    _sync_theme_colors()
except Exception:  # noqa: BLE001
    pass

# Shared UI marks (not emoji): keep prefixes consistent across chrome.
_MARK_USER = "●"  # user prompt / input
_MARK_INPUT = "›"  # input box placeholder only
_MARK_THOUGHT = "◆"  # reasoning

_USER_PREVIEW_MAX_LINES = 3
_USER_PREVIEW_MIN_COLS = 20

# Live stream must stay cheap: full-body Text/Markdown re-layout freezes the
# Textual event loop (status can still tick, transcript becomes unusable).
_MARKDOWN_MAX_CHARS = 24_000


def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


# Text prefix for git branch (not emoji; terminal-safe branch mark).
_TOPBAR_BRANCH_MARK = "⎇"  # APL upwards vane / branch mark



def _git_branch(cwd: Path) -> str | None:
    """Backward-compatible branch name probe."""
    info = probe_git_branch_chrome(cwd)
    return info.name if info is not None else None


_RAIL_PREVIEW_MAX = 28
_RAIL_BAR = "───"
_RAIL_BAR_DENSE = "━━━"
_RAIL_BAR_HEAVY = "▓▓▓"








class CodingAgentApp(App[None]):
    """Cursor-like agent transcript."""

    CSS = """
    Screen {
        layout: vertical;
        background: $theme-bg;
        color: $theme-fg;
    }
    #topbar {
        height: 1;
        /* Outer pad is theme-driven ($theme-top-pad-x); default 0 = edge-to-edge. */
        padding: 0 $theme-top-pad-x;
        color: $theme-fg;
        background: $theme-top;
    }
    #main {
        height: 1fr;
        layout: vertical;
        background: $theme-bg;
        padding: 0 1;
        overflow-y: hidden;
    }
    WelcomeView {
        display: none;
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        content-align: center middle;
        text-align: center;
        background: $theme-bg;
    }
    #main.welcome WelcomeView {
        display: block;
    }
    #main.welcome #log,
    #main.welcome #turn-rail,
    #main.welcome #stream {
        display: none;
    }
    #log {
        width: 1fr;
        height: 1fr;
        background: $theme-bg;
        color: $theme-fg;
        padding: 0 1;
        /* Match #turn-rail width so meta/time is not painted under the overlay. */
        padding-right: 34;
        /* Hide chrome; wheel / keys / programmatic scroll still work. */
        scrollbar-size: 0 0;
        scrollbar-background: $theme-bg;
        scrollbar-color: $theme-bg;
    }
    #turn-rail {
        dock: right;
        layer: overlay;
        width: 34;
        min-width: 34;
        max-width: 34;
        height: 1fr;
        background: transparent;
        scrollbar-size: 0 0;
        overflow-y: hidden;
    }
    #stream {
        /* Legacy fixed slot — live text now mounts in #log in place.
           Keep the node for compat but never reserve vertical space. */
        display: none;
        height: 0;
        max-height: 0;
        padding: 0;
        overflow-y: hidden;
    }
    #stream.active {
        display: none;
    }
    /* Single bottom stack: Textual multi-dock bottom does NOT stack (overlaps). */
    #bottom-chrome {
        dock: bottom;
        height: auto;
        layout: vertical;
        background: $theme-bg;
    }
    #status {
        height: 1;
        padding: 0 2;
        color: $theme-muted;
        background: $theme-bg;
    }
    #status.busy {
        color: $theme-orange;
    }
    #steer-queue {
        height: auto;
        max-height: 12;
        width: 48;
        max-width: 56;
        min-width: 28;
        margin: 0 1;
        /* Theme vars must live in app CSS so DEFAULT_CSS can resolve them. */
    }
    SteerQueueWidget {
        background: $theme-bg;
        border: round $theme-user;
    }
    SteerHeader {
        color: $theme-orange;
        background: $theme-bg;
    }
    SteerHeader:hover {
        background: $theme-bar;
    }
    SteerRow {
        color: $theme-dim;
        background: $theme-bg;
    }
    SteerRow.-next {
        color: $theme-user;
        background: $theme-bar;
        text-style: bold;
    }
    SteerRow:hover {
        background: $theme-bar;
    }
    #complete-hint {
        height: auto;
        padding: 0 2;
        color: $theme-muted;
        background: $theme-bg;
    }
    #prompt {
        background: $theme-bg;
        color: $theme-fg;
        border: $theme-prompt-border-style $theme-border;
        padding: 0 1;
        margin: 0 1 0 1;
        height: 3;
    }
    #prompt:focus {
        border: $theme-prompt-border-style $theme-border-focus;
    }
    #bottombar {
        height: 1.5;
        padding: 0 2;
        /* Gap under the prompt so chrome does not feel glued to the input. */
        margin: 1 0 0 0;
        /* No forced color: Rich Text carries per-region styles. */
        background: $theme-bg;
        content-align: left middle;
    }
    /* Must be in the app stylesheet: widget DEFAULT_CSS is parsed separately
       and cannot resolve the app's $theme-* variables. */
    TurnRailItem {
        height: 1;
        width: 1fr;
        color: $theme-muted;
        padding: 0 0;
        margin: 0 0 0 0;
        content-align: right middle;
        text-align: right;
    }
    TurnRailItem.-hover {
        color: $theme-fg;
    }
    TurnRailItem.-dense {
        color: $theme-dim;
    }
    /* Tool groups: faint left edge on hover marks the whole block as one unit.
       Always keep a transparent left border so hover does not reflow width. */
    ToolGroupBlock {
        width: 1fr;
        height: auto;
        border-left: solid transparent;
    }
    ToolGroupBlock.-hover {
        border-left: solid $theme-dim;
    }
    AnswerDivider {
        color: $theme-muted;
    }
    """

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
        self._image_bank = ImageBank()
        # 粘贴截断映射: {占位符: 完整原始文本}
        self._paste_replacements: dict[str, str] = {}
        # 补全下拉菜单当前高亮行索引
        self._complete_active_idx = 0
        # 当前补全会话的基准值（用户原始输入，用于 Tab 循环）
        self._complete_base_value = ""
        if agent is not None:
            self._agent_ready.set()
        self._busy = False
        self._cancel_event = threading.Event()
        self._phase = "idle"
        self._detail = "ready" if agent is not None else "starting"
        self._activity_started = time.monotonic()
        self._spin_i = 0
        self._steer_items: list[str] = []
        self._steer_last_count = 0
        self._steer_bound_queue: SteerQueue | None = None
        self._steer_listener: Any | None = None
        self._active_turn_agent: Any | None = None
        self._active_turn_thread_id: str | None = None
        self._active_steer_queue: SteerQueue | None = None
        self._subagent_monitor = SubagentMonitor()
        self._skip_steer_followup = False
        self._last_thought_body = ""
        self._last_thought_elapsed = 0.0
        self._thought_expanded = False
        self._last_tool_items: list[ToolItem] = []
        self._last_tool_summary = ""
        self._last_answer_text = ""
        self._live_tool_items: list[ToolItem] = []
        self._live_tool_summary = ""
        self._thought_blocks: list[ThoughtBlock] = []
        self._tool_blocks: list[ToolGroupBlock] = []
        self._live_tool_block: ToolGroupBlock | None = None
        # In-timeline live stream (reasoning / answer), like tool groups.
        self._live_stream_block: ThoughtBlock | AnswerBlock | None = None
        self._live_stream_kind: str | None = None
        self._user_turns: list[UserTurnBlock] = []
        self._in_tool_rail = False
        # After tools run, next final answer gets a ◇ divider above it.
        self._pending_answer_divider = False
        self._session_recap = SessionRecapController(
            enabled=bool(getattr(settings, "session_recap_enabled", True)),
            idle_seconds=float(
                getattr(settings, "session_recap_idle_seconds", 180.0) or 180.0
            ),
            min_turns=int(getattr(settings, "session_recap_min_turns", 3) or 3),
        )
        self._context_tokens = 0
        self._last_out_tokens = 0
        self._input_tokens = 0
        self._cache_tokens = 0
        self._output_tokens = 0
        # Snapshot before a live turn so mid-turn updates stay absolute.
        self._usage_base_input = 0
        self._usage_base_output = 0
        self._usage_base_cache = 0
        self._session_title = ""
        self._complete_applied: str | None = None
        self._complete_cands: list[str] = []
        # Ephemeral left-side status notice (slash confirms etc.); not transcript.
        self._status_notice: str = ""
        self._status_notice_style: str = "dim"
        self._status_notice_until: float = 0.0
        self._status_notice_timer = None
        ws = Path(getattr(settings, "workspace", Path.cwd()) or Path.cwd())
        self._git_chrome: GitBranchChrome | None = probe_git_branch_chrome(ws)
        self._git_branch = self._git_chrome.name if self._git_chrome else None
        hist_root = Path(project_root or ws)
        self._input_history = InputHistory.for_project(hist_root)
        self._tool_output_repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
        self._tool_output_stats: dict[str, Any] = {}
        self._tool_output_stats_thread_id: str | None = None
        self._tool_output_refresh_pending = False
        self._topbar = TopBarRegistry()
        self._install_default_topbar()
        self._bottombar = BottomBarRegistry()
        self._install_default_bottombar()
        self.title = "Synapse"
        self.sub_title = model_status_label(settings)
        self._reload_session_title()

    def _slash_complete_ctx(self):
        from synapse.commands.slash_complete import build_complete_context

        return build_complete_context(self.settings)

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
                "clean": _C_GREEN,
                "dirty": _C_ERROR,
                "dim": _C_DIM,
                "fg": _C_FG,
                "orange": _C_ORANGE,
                "added": _C_GREEN,
                "deleted": _C_ERROR,
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
            yield Static("", id="status")
            yield Static("", id="complete-hint")
            yield Input(
                placeholder=f"{_MARK_INPUT}  Build anything  (/ for commands, Tab complete)",
                id="prompt",
                suggester=make_textual_suggester(
                    self._slash_complete_ctx,
                    workspace=self.project_root,
                ),
            )
            yield Static("", id="bottombar")

    def on_unmount(self) -> None:
        clear_metrics_notifier()

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
        self.set_interval(0.1, self._tick_status)
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

    def _on_agent_ready(self, with_mcp: bool) -> None:
        label = "agent ready" + (" + MCP" if with_mcp else " (MCP pending)")
        self.append_event(label, "dim")
        self.set_activity("idle", "ready", True)
        self._bind_steer_queue()
        self._restore_session_transcript(announce=True)

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

    def _set_complete_hint(self, value: str) -> None:
        from synapse.commands.slash_complete import (
            complete_at_line,
            complete_slash,
        )

        hint = self.query_one("#complete-hint", Static)

        # ---- 先更新补全会话基准值 ----
        # 防御：value 有 @ 但 base_value 没有 → 强制更新
        if self.project_root and "@" in value and "@" not in (self._complete_base_value or ""):
            self._complete_base_value = value
            self._complete_active_idx = 0
        elif not self._complete_base_value:
            # 新会话
            self._complete_base_value = value
            self._complete_active_idx = 0
        elif self._complete_applied and self._complete_applied == value:
            # 补全已应用 → 保持 base_value 不变（Tab / 箭头导航中）
            pass
        elif value.startswith(self._complete_base_value):
            # 用户继续输入更多字符 → 更新 base_value 缩小匹配范围
            self._complete_base_value = value
            self._complete_active_idx = 0
        else:
            # 用户改变了前缀方向 → 重置
            self._complete_base_value = value
            self._complete_active_idx = 0

        # ---- 基于 base_value 计算候选列表 ----
        cands: list[str] = []
        if value.startswith("/"):
            cands = complete_slash(self._complete_base_value or value, self._slash_complete_ctx())
        elif self.project_root and "@" in value:
            cands = complete_at_line(self._complete_base_value or value, self.project_root)

        if not cands:
            hint.update("")
            self._complete_cands = []
            return

        # 多行下拉菜单渲染（最多 6 行）
        max_rows = 6
        active = self._complete_active_idx
        # 确保 active 在有效范围内
        if active >= len(cands):
            active = len(cands) - 1
        # 滚动窗口：尽量让 active 行保持可见
        if active >= max_rows:
            window_start = active - max_rows + 1
            shown = cands[window_start : window_start + max_rows]
            offset = window_start
        else:
            shown = cands[:max_rows]
            offset = 0

        lines: list[str] = []
        for i, c in enumerate(shown):
            idx = offset + i
            # 提取 @/command 尾部用于紧凑显示
            if "@" in c:
                at_pos = c.rfind("@")
                tail = c[at_pos:]
            elif c.startswith("/"):
                tail = c
            else:
                tail = c
            if idx == active:
                lines.append(f"[bold reverse] {tail} [/]")
            else:
                lines.append(f"  {tail}")
        if len(cands) > offset + max_rows:
            lines.append(f"  [dim]...+{len(cands) - offset - max_rows} more[/]")
        elif offset > 0:
            lines.append(f"  [dim]...+{offset} above[/]")
        hint.update("\n".join(lines))

    def _apply_completion(self, line: str) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.value = line
        prompt.cursor_position = len(line)
        self._complete_applied = line
        self._set_complete_hint(line)

    def action_complete_slash(self) -> None:
        """Accept / cycle slash completions (Tab)."""
        from synapse.commands.slash_complete import (
            complete_at_line,
            complete_slash,
        )

        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            return
        value = prompt.value or ""

        # --- @ path completion ---
        if self.project_root and "@" in value:
            # ghost 首次接受
            ghost = getattr(prompt, "_suggestion", "") or ""
            if (
                not self._complete_applied
                and ghost
                and ghost != value
                and "@" in ghost
            ):
                cands = complete_at_line(self._complete_base_value or value, self.project_root)
                self._complete_active_idx = 0
                self._apply_completion_candidate(cands, 0)
                return

            # 循环候选
            cands = self._current_completion_cands()
            if cands:
                nxt_idx = (self._complete_active_idx + 1) % len(cands)
                self._apply_completion_candidate(cands, nxt_idx)
            return

        # --- / command completion ---
        if not value.startswith("/"):
            return
        ctx = self._slash_complete_ctx()

        # ghost 首次接受
        ghost = getattr(prompt, "_suggestion", "") or ""
        if (
            not self._complete_applied
            and ghost
            and ghost.casefold().startswith(value.casefold())
            and ghost != value
        ):
            cands = complete_slash(self._complete_base_value or value, ctx)
            self._complete_active_idx = 0
            self._apply_completion_candidate(cands, 0)
            return

        # 循环候选
        cands = self._current_completion_cands()
        if cands:
            nxt_idx = (self._complete_active_idx + 1) % len(cands)
            self._apply_completion_candidate(cands, nxt_idx)

    def action_complete_slash_prev(self) -> None:
        """Cycle slash completions backwards (Shift+Tab)."""

        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            return
        value = prompt.value or ""

        # --- @ path completion (prev) ---
        if self.project_root and "@" in value:
            cands = self._current_completion_cands()
            if cands:
                nxt_idx = (self._complete_active_idx - 1) % len(cands)
                self._apply_completion_candidate(cands, nxt_idx)
            return

        # --- / command completion (prev) ---
        cands = self._current_completion_cands()
        if cands:
            nxt_idx = (self._complete_active_idx - 1) % len(cands)
            self._apply_completion_candidate(cands, nxt_idx)

    # ------------------------------------------------------------------
    # Intercept Tab/Shift+Tab to run completion before focus switching
    # ------------------------------------------------------------------

    def action_focus_next(self) -> None:
        """Tab: run completion for @/slash, or focus next widget."""
        prompt = self.query_one("#prompt", Input)
        if prompt.has_focus:
            value = prompt.value or ""
            if self.project_root and "@" in value:
                self.action_complete_slash()
                return
            if value.startswith("/"):
                self.action_complete_slash()
                return
        self.screen.focus_next()

    def action_focus_previous(self) -> None:
        """Shift+Tab: run completion (prev) for @/slash, or focus previous widget."""
        prompt = self.query_one("#prompt", Input)
        if prompt.has_focus:
            value = prompt.value or ""
            if self.project_root and "@" in value:
                self.action_complete_slash_prev()
                return
            if value.startswith("/"):
                self.action_complete_slash_prev()
                return
        self.screen.focus_previous()

    def action_show_completions(self) -> None:
        """List available slash completions (Ctrl+Space)."""
        from synapse.commands.slash_complete import complete_slash

        prompt = self.query_one("#prompt", Input)
        value = prompt.value or ""
        if not value.startswith("/"):
            self.append_event("type / to start a slash command", "dim")
            return
        cands = complete_slash(value, self._slash_complete_ctx())
        if not cands and " " in value.rstrip():
            parent = value.rstrip().rsplit(" ", 1)[0] + " "
            cands = complete_slash(parent, self._slash_complete_ctx())
        if not cands:
            self.append_event("no completions", "yellow")
            return
        self.append_event("completions:", "dim")
        for c in cands[:20]:
            mark = "*" if c == value else " "
            self.append_event(f" {mark} {c}", "dim")
        if len(cands) > 20:
            self.append_event(f"  ... +{len(cands) - 20} more", "dim")

    def _set_prompt_value(self, text: str) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.value = text
        prompt.cursor_position = len(text)
        self._set_complete_hint(text)

    def action_history_up(self) -> None:
        """Recall older project input history / navigate completion (up)."""
        if isinstance(self.screen, ModalScreen):
            return
        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            return

        # 补全菜单活跃时：将 up/down 重定向为菜单导航
        if self._complete_base_value:
            cands = self._current_completion_cands()
            if cands:
                self._complete_active_idx = (
                    self._complete_active_idx - 1
                    if self._complete_active_idx > 0
                    else len(cands) - 1
                )
                self._apply_completion_candidate(cands, self._complete_active_idx)
                return

        nxt = self._input_history.up(prompt.value or "")
        if nxt is not None:
            self._set_prompt_value(nxt)

    def action_history_down(self) -> None:
        """Recall newer project input history / navigate completion (down)."""
        if isinstance(self.screen, ModalScreen):
            return
        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            return

        # 补全菜单活跃时：将 up/down 重定向为菜单导航
        if self._complete_base_value:
            cands = self._current_completion_cands()
            if cands:
                self._complete_active_idx = (
                    self._complete_active_idx + 1
                ) % len(cands)
                self._apply_completion_candidate(cands, self._complete_active_idx)
                return

        nxt = self._input_history.down(prompt.value or "")
        if nxt is not None:
            self._set_prompt_value(nxt)

    def _current_completion_cands(self) -> list[str]:
        """Return candidates for the active completion session
        (always based on _complete_base_value)."""
        from synapse.commands.slash_complete import complete_at_line, complete_slash

        if self.project_root and "@" in (self._complete_base_value or ""):
            return complete_at_line(self._complete_base_value, self.project_root)
        if (self._complete_base_value or "").startswith("/"):
            ctx = self._slash_complete_ctx()
            base = self._complete_base_value
            cands = complete_slash(base, ctx)
            if len(cands) <= 1 and " " in base.rstrip():
                cands = complete_slash(base.rstrip().rsplit(" ", 1)[0] + " ", ctx)
            return cands
        return []

    def _apply_completion_candidate(self, cands: list[str], idx: int) -> None:
        """Apply the candidate at *idx* and refresh the dropdown."""
        if not cands:
            return
        prompt = self.query_one("#prompt", Input)
        nxt = cands[idx % len(cands)]
        self._complete_cands = cands
        self._complete_active_idx = idx
        prompt.value = nxt
        prompt.cursor_position = len(nxt)
        self._complete_applied = nxt
        self._set_complete_hint(nxt)

    @on(Input.Changed, "#prompt")
    def handle_prompt_changed(self, event: Input.Changed) -> None:
        value = event.value or ""
        # 清理已失效的粘贴占位符映射（用户编辑后占位符被破坏）
        if self._paste_replacements:
            stale = [p for p in self._paste_replacements if p not in value]
            for p in stale:
                del self._paste_replacements[p]
        # 清理 / 命令补全状态（但不影响 @ 补全会话）
        in_at_session = bool(
            self.project_root
            and "@" in value
            and self._complete_base_value
            and "@" in self._complete_base_value
        )
        if not value.startswith("/") and not in_at_session:
            self._complete_applied = None
            self._complete_cands = []
            self._complete_active_idx = 0
            self._complete_base_value = ""
        elif self._complete_applied and not value.casefold().startswith(
            self._complete_applied[: max(1, len(value))].casefold()
        ):
            self._complete_applied = None
            self._complete_active_idx = 0
            self._complete_base_value = ""
        # 清理 @ 补全状态：当 value 不再包含 @ 或不再以已应用的补齐开头
        if self._complete_applied and "@" in self._complete_applied:
            if "@" not in value or not value.startswith(
                self._complete_applied[: max(1, len(value))]
            ):
                self._complete_applied = None
                self._complete_cands = []
                self._complete_active_idx = 0
                self._complete_base_value = ""
        self._set_complete_hint(value)

    def _mcp_snapshot(self) -> tuple[bool, list[str], list[str], list[str], bool]:
        enabled = bool(getattr(self.settings, "enable_mcp", True))
        servers = list(getattr(build_coding_agent, "last_mcp_servers", []) or [])
        tools = list(getattr(build_coding_agent, "last_mcp_tool_names", []) or [])
        warnings = list(getattr(build_coding_agent, "last_mcp_warnings", []) or [])
        deferred = bool(getattr(build_coding_agent, "last_mcp_deferred", False))
        return enabled, servers, tools, warnings, deferred

    def _mcp_label(self) -> str:
        enabled, servers, tools, warnings, deferred = self._mcp_snapshot()
        return format_mcp_status_label(
            enabled=enabled,
            servers=servers,
            tools=tools,
            warnings=warnings,
            deferred=deferred,
        )

    def _reload_session_title(self) -> None:
        """Load human title for the active thread into chrome state."""
        title = ""
        try:
            from synapse.sessions.store import SessionStore

            info = SessionStore(self.settings.resolved_sessions_path()).get(
                self.thread_id
            )
            if info is not None:
                title = (info.title or "").strip()
        except Exception:  # noqa: BLE001
            title = ""
        self._session_title = title

    def _session_title_label(self, *, max_len: int = 48) -> str:
        title = (self._session_title or "").strip()
        if not title:
            # Compact fallback so middle is never empty.
            tid = str(self.thread_id or "")
            title = tid if len(tid) <= 12 else f"{tid[:8]}…"
        if len(title) <= max_len:
            return title
        return title[: max(0, max_len - 1)] + "…"

    def _context_window_tokens(self) -> int | None:
        """Model context window (tokens) from chat model profile or models.json."""
        agent = getattr(self, "agent", None)
        model = getattr(agent, "_coding_model", None) if agent is not None else None
        profile = getattr(model, "profile", None) if model is not None else None
        if isinstance(profile, dict):
            raw = profile.get("max_input_tokens")
            try:
                n = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return n

        reg = getattr(agent, "_coding_model_registry", None) if agent is not None else None
        name = None
        if agent is not None:
            name = getattr(agent, "_coding_model_profile", None)
        if not name:
            name = getattr(self.settings, "active_model", None) or getattr(
                self.settings, "model", None
            )
        if reg is not None and name:
            try:
                prof = reg.get(name)
                win = getattr(prof, "context_window", None)
                if win is not None and int(win) > 0:
                    return int(win)
            except Exception:  # noqa: BLE001
                pass

        try:
            from synapse.models.registry import registry_from_settings

            reg2 = registry_from_settings(self.settings)
            if reg2 is not None:
                prof2 = reg2.get(name)
                win2 = getattr(prof2, "context_window", None)
                if win2 is not None and int(win2) > 0:
                    return int(win2)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _usage_right_label(self) -> str | Text:
        """Render token totals and occupancy using distinct theme colors.

        Input, cache, output, and the current context occupancy each keep their
        own visual role while separators remain muted.
        """
        last_in = int(getattr(self, "_context_tokens", 0) or 0)
        has_totals = bool(self._input_tokens or self._cache_tokens or self._output_tokens)
        if has_totals:
            input_tokens = self._input_tokens
            cache_tokens = self._cache_tokens
            output_tokens = self._output_tokens
        elif last_in:
            input_tokens, cache_tokens, output_tokens = last_in, 0, 0
        else:
            input_tokens = cache_tokens = output_tokens = 0

        occupancy = format_context_occupancy_label(
            last_input_tokens=last_in,
            context_window=self._context_window_tokens(),
        )
        if not (has_totals or last_in or occupancy):
            return ""

        label = Text()
        if has_totals or last_in:
            label.append(format_token_count(input_tokens), style=_C_FG)
            label.append("/", style=_C_MUTED)
            label.append(format_token_count(cache_tokens), style=_C_GREEN)
            label.append("/", style=_C_MUTED)
            label.append(format_token_count(output_tokens), style=_C_ORANGE)
        if occupancy:
            if label:
                label.append(" ", style=_C_MUTED)
            label.append(occupancy, style=_C_GREEN)
        return label


    def _tool_output_label(self) -> str | Text:
        """Render the stable net tool-output saving for the active session."""
        stats = self._tool_output_stats
        if self._tool_output_stats_thread_id != self.thread_id or not stats:
            return ""
        if not int(stats.get("transformed", 0) or 0):
            return ""
        saved = max(0, int(stats.get("effective_saved_bytes", 0) or 0))
        # Keep the chrome to a stable absolute metric. The ratio is cumulative and
        # changes whenever any later tool output is recorded, so it belongs in hover.
        return Text(
            f"saved {format_byte_count(saved)}",
            style=_C_GREEN if saved else _C_ORANGE,
        )

    def _tool_output_hover_stats(self) -> dict[str, Any]:
        """Return a snapshot for the tool-output topbar hover popover."""
        if self._tool_output_stats_thread_id != self.thread_id:
            return {}
        return dict(self._tool_output_stats or {})

    def _reload_tool_output_stats(self) -> None:
        """Load persistent metrics for the active session outside the render path."""
        try:
            stats = self._tool_output_repo.stats(thread_id=self.thread_id)
        except Exception:  # noqa: BLE001
            stats = {}
        self._tool_output_stats = stats
        self._tool_output_stats_thread_id = self.thread_id
        self._refresh_topbar()

    def _on_tool_output_metrics_changed(self, thread_id: str) -> None:
        """Receive a worker-thread metric write and coalesce UI refreshes."""
        if thread_id != self.thread_id or self._tool_output_refresh_pending:
            return
        self._tool_output_refresh_pending = True
        try:
            self.call_from_thread(self._refresh_tool_output_stats)
        except Exception:  # noqa: BLE001
            self._tool_output_refresh_pending = False

    def _refresh_tool_output_stats(self) -> None:
        self._tool_output_refresh_pending = False
        self._reload_tool_output_stats()

    def _begin_turn_usage(self) -> None:
        """Mark session totals baseline for live per-call topbar updates."""
        self._usage_base_input = int(self._input_tokens or 0)
        self._usage_base_output = int(self._output_tokens or 0)
        self._usage_base_cache = int(self._cache_tokens or 0)

    def apply_turn_usage(
        self,
        *,
        turn_input: int = 0,
        turn_output: int = 0,
        turn_cache: int = 0,
        last_input: int = 0,
        last_output: int = 0,
        last_cache: int = 0,
    ) -> None:
        """Apply cumulative-in-turn usage (from stream) onto session chrome.

        ``turn_*`` are totals for the *current* stream/turn so far (not deltas).
        Session display = baseline + turn totals. Occupancy uses last call input.
        """
        self._input_tokens = int(self._usage_base_input or 0) + max(0, int(turn_input or 0))
        self._output_tokens = int(self._usage_base_output or 0) + max(
            0, int(turn_output or 0)
        )
        self._cache_tokens = int(self._usage_base_cache or 0) + max(0, int(turn_cache or 0))
        if last_input or last_output or last_cache:
            self._context_tokens = int(last_input or 0)
            self._last_out_tokens = int(last_output or 0)
        self._refresh_topbar()

    def _apply_restored_usage(self, messages: list[Any] | None) -> None:
        """Hydrate topbar totals from checkpoint messages (session open / switch)."""
        try:
            from synapse.ui.stream import aggregate_usage_from_messages
        except Exception:  # noqa: BLE001
            return
        try:
            agg = aggregate_usage_from_messages(messages)
        except Exception:  # noqa: BLE001
            return
        self._input_tokens = int(agg.get("input_tokens") or 0)
        self._output_tokens = int(agg.get("output_tokens") or 0)
        self._cache_tokens = int(agg.get("cache_tokens") or 0)
        self._context_tokens = int(agg.get("last_input_tokens") or 0)
        self._last_out_tokens = int(agg.get("last_output_tokens") or 0)
        self._usage_base_input = self._input_tokens
        self._usage_base_output = self._output_tokens
        self._usage_base_cache = self._cache_tokens
        self._refresh_topbar()

    def _render_branch_chrome(self):
        """Styled branch + dirty/diff stats/ahead/behind for the topbar."""
        return render_branch_chrome(
            self._git_chrome,
            mark=_TOPBAR_BRANCH_MARK,
            color_clean=_C_GREEN,
            color_dirty=_C_ERROR,
            color_ahead=_C_USER,
            color_behind=_C_ORANGE,
            color_diverged=_C_FG,
            color_files=_C_DIM,
            color_added=_C_GREEN,
            color_deleted=_C_ERROR,
        )

    def _refresh_git_chrome(self) -> None:
        """Re-probe local git status for the topbar (cheap, local-only)."""
        try:
            ws = Path(getattr(self.settings, "workspace", Path.cwd()) or Path.cwd())
            self._git_chrome = probe_git_branch_chrome(ws)
            self._git_branch = self._git_chrome.name if self._git_chrome else None
        except Exception:  # noqa: BLE001
            pass
        try:
            bar = self.query_one("#topbar", TopBar)
            bar.invalidate_files_cache()
            if not (self._git_chrome and self._git_chrome.dirty):
                bar.dismiss()
        except Exception:  # noqa: BLE001
            pass
        self._refresh_topbar()

    def _install_default_topbar(self) -> None:
        """Register built-in workspace / title / branch / usage components."""
        install_default_components(
            self._topbar,
            TopBarContext(
                workspace=lambda: short_workspace_label(self.settings.workspace),
                title=lambda: (self._session_title or "").strip()
                or self._session_title_label(max_len=56),
                branch=self._render_branch_chrome,
                usage=self._usage_right_label,
                tool_output=self._tool_output_label,
                branch_mark=_TOPBAR_BRANCH_MARK,
            ),
        )
        self._apply_topbar_region_bands()

    def _apply_topbar_region_bands(self) -> None:
        """Apply theme topbar metrics (gap/pad already in CSS; optional band bg).

        Built-in themes leave region backgrounds empty. ``top_gap`` controls
        spacing between left/center/right. Explicit ``top_left`` /
        ``top_center`` / ``top_right`` still enable optional color bands.
        Layout stays classic: left/right hug content, center flex-fills.
        """
        gap = 0
        try:
            from synapse.ui.theme import get_theme

            theme = get_theme()
            bands = theme.topbar_region_bands()
            gap = max(0, int(getattr(theme, "top_gap", 0) or 0))
        except Exception:  # noqa: BLE001
            bands = {
                "left": (_C_FG, ""),
                "center": (_C_FG, ""),
                "right": (_C_DIM, ""),
            }

        layout = {
            "left": {
                "flex": 0,
                "align": "left",
                "min_width": 0,
                "priority": 40,
                "gap_after": gap,
            },
            "center": {
                "flex": 1,
                "align": "center",
                "min_width": 4,
                "priority": 10,
                "gap_after": gap,
            },
            "right": {
                "flex": 0,
                "align": "right",
                "min_width": 0,
                "priority": 50,
                "gap_after": 0,
            },
        }
        for rid, (fg, bg) in bands.items():
            conf = layout.get(rid, {})
            # bg="" clears band (set_region_style treats None as "leave unchanged").
            self._topbar.set_region_style(
                rid,
                fg=fg or _C_FG,
                bg=bg if bg else "",
                flex=int(conf.get("flex", 0)),
                align=str(conf.get("align", "left")),
                min_width=int(conf.get("min_width", 0)),
                priority=int(conf.get("priority", 0)),
                gap_after=int(conf.get("gap_after", 0)),
            )

    def _install_default_bottombar(self) -> None:
        """Register key_hints / mode / model / mcp under the prompt."""
        install_default_bottombar_components(
            self._bottombar,
            BottomBarContext(
                busy=lambda: bool(self._busy),
                thread=lambda: "",  # thread chrome disabled on bottombar
                mode=self._bottombar_mode_label,
                idle_hints=lambda: (
                    "Tab complete · / · Alt+C copy · F2 model · F4 sessions · F9 agents"
                ),
                busy_hints=lambda: "Esc cancel · Enter queue · Alt+C copy · F9 agents",
                model=lambda: model_status_label(self.settings),
                mcp=self._mcp_label,
            ),
        )

    def _bottombar_thread_label(self) -> str:
        """Short thread id for the bottombar right slot."""
        tid = (self.thread_id or "").strip()
        if not tid:
            return ""
        if len(tid) <= 12:
            return tid
        return f"{tid[:4]}…{tid[-4:]}"

    def _bottombar_mode_label(self) -> str:
        """Optional center mode badge. Steer count shown in status line only."""
        return ""

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
            left_style=_C_USER,
            center_style=_C_ORANGE,
            right_style=_C_MUTED,
            gap_style=_C_MUTED,
        )
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
                    "dim": _C_DIM,
                    "fg": _C_FG,
                    "orange": _C_ORANGE,
                    "added": _C_GREEN,
                    "deleted": _C_ERROR,
                    "hunk": _C_USER,
                },
            ),
            self._on_git_explore_done,
        )

    def _on_git_explore_done(self, _result: object) -> None:
        # Refresh chrome after explore closes (user may have committed outside).
        self._refresh_git_chrome()

    def _refresh_topbar(self, tokens: str | None = None) -> None:
        del tokens  # legacy arg; usage is tracked on the app
        usable = self._topbar_usable_width()
        line = layout_from_registry(
            self._topbar,
            usable_width=usable,
            left_style=_C_FG,
            center_style=_C_FG,
            right_style=_C_DIM,
            gap_style=_C_DIM,
        )
        self.query_one("#topbar", TopBar).update(line)

    def on_resize(self, event: object) -> None:  # noqa: ANN001
        del event
        self._refresh_topbar()
        self._render_status()
        self._refresh_bottombar()
        self._refresh_turn_rail()

    # -- status ----------------------------------------------------------

    def flash_status(
        self,
        message: str,
        style: str = "dim",
        *,
        ttl: float = 4.0,
    ) -> None:
        """Show a short notice in #status left activity slot (not transcript)."""
        msg = (message or "").strip()
        if not msg:
            return
        # Keep single-line chrome; collapse whitespace.
        msg = " ".join(msg.split())
        self._status_notice = msg
        self._status_notice_style = (style or "dim").strip() or "dim"
        self._status_notice_until = time.monotonic() + max(0.5, float(ttl or 0))
        if self._status_notice_timer is not None:
            try:
                self._status_notice_timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._status_notice_timer = None
        # Only schedule auto-clear when the app loop is actually running.
        if bool(getattr(self, "is_running", False)):
            try:
                self._status_notice_timer = self.set_timer(
                    max(0.5, float(ttl or 0)), self._clear_status_notice
                )
            except Exception:  # noqa: BLE001
                self._status_notice_timer = None
        try:
            self._render_status()
        except Exception:  # noqa: BLE001
            pass

    def _clear_status_notice(self) -> None:
        self._status_notice_timer = None
        self._status_notice = ""
        self._status_notice_style = "dim"
        self._status_notice_until = 0.0
        try:
            self._render_status()
        except Exception:  # noqa: BLE001
            pass

    def _active_status_notice(self) -> str:
        msg = (self._status_notice or "").strip()
        if not msg:
            return ""
        if time.monotonic() >= float(self._status_notice_until or 0):
            self._status_notice = ""
            self._status_notice_until = 0.0
            return ""
        return msg

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
        detail = detail or ""
        if reset_timer or phase != self._phase:
            self._activity_started = time.monotonic()
        self._phase = phase or "idle"
        self._detail = detail
        busy = self._phase not in {"idle", "ready", ""}
        self.query_one("#status", Static).set_class(busy, "busy")
        if busy:
            self.sub_title = f"{model_status_label(self.settings)} · {self._phase}"
        else:
            self.sub_title = model_status_label(self.settings)
        self._render_status()

    def _resident_status_right(self) -> str:
        """Deprecated: model/mcp live on the bottombar now."""
        return ""

    def _idle_status_label(self) -> str:
        """Bottom status when idle (activity/notice only; model/mcp on bottombar)."""
        return ""

    def _status_notice_style_token(self) -> str:
        """Map notice style name to a palette color for the left activity slot."""
        key = (self._status_notice_style or "dim").lower()
        if "red" in key or "error" in key:
            return _C_ERROR
        if "yellow" in key or "warn" in key or "orange" in key:
            return _C_ORANGE
        return _C_DIM

    def _compose_status_left(
        self,
        *,
        busy: bool,
        elapsed: float,
        steer_n: int,
        left_budget: int,
    ) -> tuple[str, str]:
        """Build activity/notice text for #status (full width).

        Layout target (above the prompt)::

            [ left activity / notice ]

        Model · thinking · mcp moved to the bottombar under the prompt.
        """
        notice = self._active_status_notice()
        if notice:
            # Prefer the ephemeral confirm while it is live, even mid-run.
            return truncate_to_width(notice, left_budget), self._status_notice_style_token()
        if not busy:
            return "", _C_MUTED
        spin = _SPINNER[self._spin_i % len(_SPINNER)]
        detail = f" {self._detail}" if self._detail else ""
        steer_badge = f" · queue×{steer_n}" if steer_n else ""
        left = f"{spin} {self._phase}{detail}{steer_badge} · {elapsed:.1f}s"
        return truncate_to_width(left, left_budget), _C_ORANGE

    def _render_status(self) -> None:
        """Paint #status activity/notice only; model/mcp live on #bottombar."""
        elapsed = max(0.0, time.monotonic() - self._activity_started)
        busy = self._phase not in {"idle", "ready", ""}
        status = self.query_one("#status", Static)
        width = max(int(getattr(self.size, "width", 0) or 0), 48)
        # Account for CSS padding (0 2).
        usable = max(16, width - 4)
        steer_n = len(self._steer_items)
        left, left_style = self._compose_status_left(
            busy=busy,
            elapsed=elapsed,
            steer_n=steer_n,
            left_budget=usable,
        )
        if not left:
            status.update("")
        else:
            status.update(Text(left, style=left_style))
        self._refresh_bottombar()

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
        if self._busy and self._active_steer_queue is not None:
            return self._active_steer_queue
        return get_agent_steer_queue(self.agent)

    def _capture_turn_context(self) -> None:
        """Freeze the agent, thread, and queue used by one graph run."""
        turn_agent = self.agent
        self._active_turn_agent = turn_agent
        self._active_turn_thread_id = self.thread_id
        self._active_steer_queue = get_agent_steer_queue(turn_agent)

    def _clear_turn_context(self) -> None:
        self._active_turn_agent = None
        self._active_turn_thread_id = None
        self._active_steer_queue = None

    def _on_steer_items_changed(self, items: list[str]) -> None:
        self._steer_items = [str(item).strip() for item in items if str(item).strip()]
        self._steer_last_count = len(self._steer_items)
        try:
            self.query_one("#steer-queue", SteerQueueWidget).set_items(
                self._steer_items
            )
        except Exception:  # noqa: BLE001
            pass
        self._render_status()
        self._sync_prompt_placeholder()

    def _sync_prompt_placeholder(self) -> None:
        """Prompt copy guides mode: normal vs mid-run queue."""
        try:
            prompt = self.query_one("#prompt", Input)
        except Exception:  # noqa: BLE001
            return
        if self._busy:
            n = len(self._steer_items)
            if n:
                prompt.placeholder = f"{_MARK_INPUT}  Add guidance ({n} queued)…"
            else:
                prompt.placeholder = f"{_MARK_INPUT}  Enter guidance, takes effect next turn…"
        else:
            prompt.placeholder = f"{_MARK_INPUT}  Build anything  (/ for commands, Tab complete)"

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
        if self._phase not in {"idle", "ready", ""}:
            self._spin_i += 1
            self._render_status()
        else:
            # Drop expired status notices without waiting for the timer edge case.
            if self._status_notice and time.monotonic() >= float(self._status_notice_until or 0):
                self._clear_status_notice()
            elif self._status_notice:
                self._render_status()
            self._maybe_show_session_recap()
        # Keep Thought "Thinking… Xs" / final seal clock honest between tokens.
        live = self._live_stream_block
        if isinstance(live, ThoughtBlock) and live.live:
            live.tick_live()

    # -- stream ----------------------------------------------------------

    def set_stream(self, kind: str, body: str, elapsed_s: float = 0.0) -> None:
        """Mount or update a live block at the end of #log (in place)."""
        text = body or ""
        kind = (kind or "answer").strip() or "answer"
        if self._live_stream_kind and self._live_stream_kind != kind:
            self._live_stream_block = None
            self._live_stream_kind = None
        if not text.strip() and self._live_stream_block is None:
            return
        if kind == "reasoning":
            block = self._live_stream_block
            if not isinstance(block, ThoughtBlock) or self._live_stream_kind != "reasoning":
                block = ThoughtBlock(
                    float(elapsed_s or 0.0),
                    text,
                    live=True,
                    dim_color=lambda: _C_DIM,
                    thought_mark=_MARK_THOUGHT,
                )
                self._live_stream_block = block
                self._live_stream_kind = "reasoning"
                self._thought_blocks.append(block)
                self._mount_block(block)
            else:
                block.update_live(float(elapsed_s or 0.0), text)
                self._follow_timeline_if_needed()
            self._in_tool_rail = False
            return
        block = self._live_stream_block
        if not isinstance(block, AnswerBlock) or self._live_stream_kind != "answer":
            if self._pending_answer_divider:
                self._mount_answer_divider()
                self._pending_answer_divider = False
            block = AnswerBlock(
                text,
                live=True,
                fg_color=lambda: _C_FG,
                markdown_max_chars=_MARKDOWN_MAX_CHARS,
            )
            self._live_stream_block = block
            self._live_stream_kind = "answer"
            self._mount_block(block)
        else:
            block.update_live(text)
            self._follow_timeline_if_needed()

    def clear_stream(self) -> None:
        """Drop unsealed live stream row; legacy #stream stays empty."""
        try:
            stream = self.query_one("#stream", Static)
            stream.update("")
            stream.remove_class("active")
        except Exception:  # noqa: BLE001
            pass
        block = self._live_stream_block
        if block is not None and getattr(block, "live", False):
            try:
                if block.is_attached:
                    block.remove()
            except Exception:  # noqa: BLE001
                pass
            if isinstance(block, ThoughtBlock) and block in self._thought_blocks:
                self._thought_blocks.remove(block)
        self._live_stream_block = None
        self._live_stream_kind = None

    def _follow_timeline_if_needed(self) -> None:
        try:
            timeline = self.query_one("#log", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        follow = timeline.max_scroll_y <= 0 or timeline.scroll_y >= timeline.max_scroll_y - 1
        if follow:
            self.call_after_refresh(self._scroll_timeline)

    # -- transcript writers ----------------------------------------------

    def _show_welcome(self) -> None:
        try:
            self.query_one("#main", Vertical).add_class("welcome")
            self.query_one("#welcome", WelcomeView).start_animation()
        except Exception:  # noqa: BLE001
            pass

    def _dismiss_welcome(self) -> None:
        try:
            self.query_one("#main", Vertical).remove_class("welcome")
            self.query_one("#welcome", WelcomeView).stop_animation()
        except Exception:  # noqa: BLE001
            pass

    def _mount_block(self, block: Any, *, dismiss_welcome: bool = True) -> None:
        if dismiss_welcome:
            self._dismiss_welcome()
        timeline = self.query_one("#log", VerticalScroll)
        follow = timeline.max_scroll_y <= 0 or timeline.scroll_y >= timeline.max_scroll_y - 1
        timeline.mount(block)
        if follow:
            self.call_after_refresh(self._scroll_timeline)

    def _scroll_timeline(self) -> None:
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    def _mount_markdown_block(self, text: str) -> None:
        """Render a Markdown string into the transcript, dismissing welcome."""
        if not isinstance(text, str) or not text.strip():
            return
        self._dismiss_welcome()
        if len(text) > _MARKDOWN_MAX_CHARS:
            renderable: Any = Text(text, style=_C_FG)
        else:
            renderable = render_markdown(text)
        self._mount_block(Static(renderable), dismiss_welcome=False)

    def append_user(
        self,
        text: str,
        images: list[Any] | None = None,
    ) -> None:
        imgs = list(images or [])
        block = UserTurnBlock(
            text or "",
            stamp=_stamp(),
            turn_index=len(self._user_turns) + 1,
            image_count=len(imgs),
        )
        self._user_turns.append(block)
        self._mount_block(block)
        self._refresh_turn_rail()
        self._in_tool_rail = False
        self._pending_answer_divider = False

    def _refresh_turn_rail(self) -> None:
        """Rebuild right-side turn markers from current user anchors."""
        try:
            rail = self.query_one("#turn-rail", TurnRail)
        except Exception:  # noqa: BLE001
            return
        turns = [
            (format_turn_rail_preview(block.full_text), block)
            for block in self._user_turns
        ]
        rail.set_turns(turns)

    def jump_to_user_turn(self, target: UserTurnBlock) -> None:
        """Scroll the transcript so the selected user turn is at the top."""
        if target is None or not target.is_attached:
            return
        timeline = self.query_one("#log", VerticalScroll)
        try:
            timeline.scroll_to_widget(target, animate=True, top=True)
        except Exception:  # noqa: BLE001
            try:
                timeline.scroll_to_center(target, animate=True)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    #  Copy selection / last answer (Alt+C / Ctrl+Shift+Y)
    # ------------------------------------------------------------------
    def action_copy_selection(self) -> None:
        """Copy current text selection, or fall back to the last answer body."""
        text: str | None = None
        try:
            text = self.screen.get_selected_text()
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
            timeline = self.query_one("#log", VerticalScroll)
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
            self.copy_to_clipboard(body)
        except Exception as exc:  # noqa: BLE001
            self.append_event(f"copy failed: {exc}", "yellow")
            return
        n = len(body)
        preview = body.replace("\n", " ").strip()
        if len(preview) > 48:
            preview = preview[:47].rstrip() + "…"
        self.append_event(f"copied {label} ({n} chars): {preview}", "dim")

    # ------------------------------------------------------------------
    #  Auto-copy on mouse-up selection
    # ------------------------------------------------------------------
    def on_mouse_up(self, event: MouseUp) -> None:
        """After drag-select, auto-copy selected text to clipboard."""
        # Defer one frame so Textual's compositor has fully finalised the selection.
        self.call_after_refresh(self._auto_copy_selection)

    def _auto_copy_selection(self) -> None:
        if not self.screen.selections:
            return
        text = self.screen.get_selected_text()
        if text and str(text).strip():
            self._copy_text_to_clipboard(str(text), label="selection")

    # ------------------------------------------------------------------
    #  Alt+V clipboard paste (image or text)
    # ------------------------------------------------------------------
    def action_clipboard_paste(self) -> None:
        try:
            result = read_clipboard()
        except Exception:  # noqa: BLE001
            self.append_event("clipboard read failed", "yellow")
            return

        if result.kind == "empty":
            self.append_event("clipboard empty", "dim")
            return

        if result.kind == "text":
            text = result.text or ""
            prompt = self.query_one("#prompt", Input)
            if len(text) > 200 or "\n" in text or "\r" in text:
                prefix = text[:20].replace("\r", " ").replace("\n", " ").strip()
                placeholder = f"[{prefix}... {len(text)} chars]"
                self._paste_replacements[placeholder] = text
                old = prompt.value or ""
                prompt.value = old + placeholder
                self.append_event(
                    f"pasted text truncated: {len(text)} chars -> "
                    "placeholder (content preserved)",
                    "dim",
                )
            else:
                old = prompt.value or ""
                prompt.value = old + text
            prompt.focus()
            return

        if result.kind == "image":
            try:
                att = self._image_bank.add_bytes(
                    result.data, mime=result.mime, name=result.name
                )
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"image rejected: {exc}", "yellow")
                return
            self.append_event(
                f"pasted {att.name} -> [image#{att.id}]", "dim"
            )
            prompt = self.query_one("#prompt", Input)
            old = prompt.value or ""
            prompt.value = old + f" [image#{att.id}]"
            prompt.focus()

    # ------------------------------------------------------------------

    def commit_thought(self, elapsed_s: float, body: str) -> None:
        self._last_thought_body = body or ""
        self._last_thought_elapsed = elapsed_s
        self._thought_expanded = False
        live = self._live_stream_block
        if (
            isinstance(live, ThoughtBlock)
            and self._live_stream_kind == "reasoning"
        ):
            live.seal(elapsed_s, body or "")
            self._live_stream_block = None
            self._live_stream_kind = None
            self._follow_timeline_if_needed()
        else:
            block = ThoughtBlock(
                elapsed_s,
                body,
                dim_color=lambda: _C_DIM,
                thought_mark=_MARK_THOUGHT,
            )
            self._thought_blocks.append(block)
            self._mount_block(block)
        self._in_tool_rail = False

    def action_toggle_last_thought(self) -> None:
        """Toggle the most recent ThoughtBlock (supports historical/frozen ones in transcript)."""
        timeline = self.query_one("#log", VerticalScroll)
        for child in reversed(list(timeline.children)):
            if isinstance(child, ThoughtBlock):
                child.toggle()
                self._thought_expanded = not child.collapsed
                return
        # Fallback to tracked list if DOM query yields nothing (e.g. cleared state)
        if self._thought_blocks:
            self._thought_blocks[-1].toggle()
            self._thought_expanded = not self._thought_blocks[-1].collapsed

    def action_toggle_last_tools(self) -> None:
        """Toggle the latest tool group (supports historical/frozen ones after commit).
        Queries live DOM so collapsed state works even for groups from prior turns.
        """
        timeline = self.query_one("#log", VerticalScroll)
        for child in reversed(list(timeline.children)):
            if isinstance(child, ToolGroupBlock):
                child.toggle()
                return
        # Fallback
        if self._tool_blocks:
            self._tool_blocks[-1].toggle()

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
        self._last_answer_text = body
        self._commit_live_tools_to_log()
        live = self._live_stream_block
        if isinstance(live, AnswerBlock) and self._live_stream_kind == "answer":
            # Divider was mounted when the live answer row started (set_stream).
            live.seal(body)
            self._live_stream_block = None
            self._live_stream_kind = None
            self._follow_timeline_if_needed()
            return
        if self._pending_answer_divider:
            self._mount_answer_divider()
            self._pending_answer_divider = False
        # No live row (e.g. restore / non-stream path): mount sealed answer once.
        self._mount_block(
            AnswerBlock(
                body,
                live=False,
                fg_color=lambda: _C_FG,
                markdown_max_chars=_MARKDOWN_MAX_CHARS,
            )
        )

    def _mount_answer_divider(self) -> None:
        """Insert centered ◇ rule with vertical spacing before the answer."""
        width = 0
        try:
            log = self.query_one("#log", VerticalScroll)
            width = int(getattr(log.size, "width", 0) or 0)
        except Exception:  # noqa: BLE001
            width = 0
        if width <= 0:
            width = int(getattr(self.size, "width", 0) or 0)
        # Subtract log padding (0 1) so the rule centers in the content box.
        usable = max(28, (width or 56) - 2)
        self._mount_block(AnswerDivider(usable, muted_color=lambda: _C_MUTED))

    # -- tool group rendering (live panel) --------------------------------

    def _render_live_tools(self) -> None:
        if self._live_tool_block is not None:
            self._live_tool_block.set_summary(self._live_tool_summary or "tools")

    def _tool_details_expanded(self) -> bool:
        """Whether finished tool groups keep detail rows visible (config default: True)."""
        return bool(getattr(self.settings, "tool_details_expanded", True))

    def _commit_live_tools_to_log(self) -> None:
        if self._live_tool_block is None:
            return
        self._last_tool_items = list(self._live_tool_block.items)
        self._last_tool_summary = self._live_tool_block.summary
        self._live_tool_items.clear()
        self._live_tool_summary = ""
        self._live_tool_block = None

    def write_tool_group_header(self, summary: str, collapsed: bool = True) -> None:
        # Never paint empty placeholder groups ("0 tools").
        if (summary or "").strip() in {"", "0 tools", "tools", "Running 0 tools"}:
            if self._live_tool_block is None or not self._live_tool_block.items:
                return
        # A sealed previous group must leave _live_tool_block as None so the
        # next batch always creates a fresh block (never reuses a frozen one).
        if self._live_tool_block is None:
            block = ToolGroupBlock(summary)
            block.collapsed = collapsed
            block._render_block()
            self._live_tool_block = block
            self._tool_blocks.append(block)
            self._mount_block(block)
        else:
            self._live_tool_block.set_summary(summary)
            self._live_tool_block.set_collapsed(collapsed)
        self._live_tool_summary = summary
        self._last_tool_summary = summary

    def update_tool_group_header(self, summary: str) -> None:
        self._live_tool_summary = summary
        self._last_tool_summary = summary
        self._render_live_tools()

    def write_tool_item(self, item: ToolItem) -> None:
        if self._live_tool_block is None:
            self.write_tool_group_header("tools", collapsed=False)
        assert self._live_tool_block is not None
        # Keep live groups expanded while tools are still arriving/running,
        # even when auto-collapse-after-finish is enabled.
        if any(it.status == "running" for it in [*self._live_tool_block.items, item]):
            self._live_tool_block.set_collapsed(False)
        elif self._tool_details_expanded():
            self._live_tool_block.set_collapsed(False)
        self._live_tool_block.add_item(item)
        # Prefer the block's self-derived summary (always matches items).
        self._live_tool_summary = self._live_tool_block.summary
        self._last_tool_summary = self._live_tool_block.summary
        self._live_tool_items = list(self._live_tool_block.items)
        self._last_tool_items = list(self._live_tool_items)

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
        if self._live_tool_block is None:
            return
        self._live_tool_block.update_item(
            item_id,
            status=status,
            preview=preview,
            error=error,
            label=label,
            path=path,
            name=name,
            category=category,
        )
        self._live_tool_summary = self._live_tool_block.summary
        self._last_tool_summary = self._live_tool_block.summary
        self._live_tool_items = list(self._live_tool_block.items)
        self._last_tool_items = list(self._live_tool_items)

    def write_tool_preview(
        self, item_id: str, preview: str, *, error: bool = False
    ) -> None:
        if self._live_tool_block is not None:
            self._live_tool_block.update_preview(item_id, preview, error=error)

    def close_tool_group(self) -> None:
        """Freeze the live tool block so the next batch creates a new group."""
        if self._live_tool_block is not None:
            # Final header from items, not a stale early partial summary.
            self._live_tool_block._sync_summary_from_items(running=False)
            # Default: keep details expanded. Config can auto-collapse finished batches.
            # write_todos checklists always stay expanded for readability.
            has_todo = any(
                (it.name or "").lower() in {"write_todos", "todo_write", "todos"}
                or str(it.label or "").startswith("Todos ")
                for it in self._live_tool_block.items
            )
            keep_open = has_todo or self._tool_details_expanded()
            self._live_tool_block.set_collapsed(not keep_open)
            self._live_tool_summary = self._live_tool_block.summary
            self._last_tool_summary = self._live_tool_block.summary
            self._live_tool_block._render_block()
            # Tools finished → next final answer should show the ◇ rule.
            if self._live_tool_block.items:
                self._pending_answer_divider = True
        self._commit_live_tools_to_log()

    def append_meta(self, message: str) -> None:
        self._commit_live_tools_to_log()
        body = soften_turn_footer(message)
        self._mount_block(Static(Text(f"  {body}", style=_C_MUTED)))

    def append_event(self, message: str, style: str = "dim") -> None:
        self._mount_block(
            Static(Text(f"  {message}", style=style)),
            dismiss_welcome=(style or "dim").lower() != "dim",
        )

    def action_cancel_run(self) -> None:
        """ESC: abort the in-flight agent loop so the user can start a new turn."""
        if isinstance(self.screen, ModalScreen):
            return
        if not self._busy:
            return
        # Idempotent: repeated ESC only re-asserts the cancel flag.
        self._cancel_event.set()
        self.set_activity("idle", "cancelling…", True)
        self.append_event("正在终止当前任务… (Esc)", "yellow")

    def on_key(self, event: Key) -> None:
        # When a modal dialog is open, let it handle keys exclusively.
        if isinstance(self.screen, ModalScreen):
            return
        # Backup path if a child widget swallows Escape before bindings fire.
        if event.key == "escape" and self._busy:
            self.action_cancel_run()
            event.stop()
            event.prevent_default()

    def action_clear_log(self) -> None:
        self.query_one("#log", VerticalScroll).remove_children()
        self.clear_stream()
        self._last_thought_body = ""
        self._last_tool_items.clear()
        self._last_answer_text = ""
        self._live_tool_items.clear()
        self._live_tool_summary = ""
        self._thought_blocks.clear()
        self._tool_blocks.clear()
        self._live_tool_block = None
        self._live_stream_block = None
        self._live_stream_kind = None
        self._user_turns.clear()
        self._in_tool_rail = False
        self._session_recap.reset()
        try:
            self.query_one("#turn-rail", TurnRail).clear_turns()
        except Exception:  # noqa: BLE001
            pass
        self._show_welcome()

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

    def _reset_session_token_chrome(self) -> None:
        self._input_tokens = 0
        self._cache_tokens = 0
        self._output_tokens = 0
        self._context_tokens = 0
        self._last_out_tokens = 0
        self._usage_base_input = 0
        self._usage_base_output = 0
        self._usage_base_cache = 0

    def _render_restored_tools(
        self,
        tool_calls: list[dict],
        tool_results: list[dict],
    ) -> None:
        """Render a historical tool batch as a collapsed group."""
        from synapse.ui.timeline import (
            build_tool_item,
            extract_todos,
            format_todos_preview,
            is_todo_tool,
            truncate_preview,
        )

        if not tool_calls and not tool_results:
            return
        items: list[ToolItem] = []
        result_by_id = {
            str(r.get("id") or ""): r for r in (tool_results or []) if isinstance(r, dict)
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
            return
        summary = summarize_items(items, running=False)
        self.write_tool_group_header(summary, collapsed=True)
        for it in items:
            self.write_tool_item(it)
        self.close_tool_group()

    def _restore_session_transcript(self, *, announce: bool = True) -> None:
        """Load checkpoint messages for current thread and paint the timeline.

        LLM context is restored by reusing the same ``thread_id`` with the
        LangGraph checkpointer; this method only rebuilds the visual history.
        """
        if self.agent is None:
            return
        from synapse.sessions.transcript import fold_messages_for_ui, load_thread_messages

        try:
            messages = load_thread_messages(
                agent=self.agent,
                settings=self.settings,
                thread_id=self.thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            if announce:
                self.append_event(f"restore transcript failed: {exc}", "yellow")
            return

        events = fold_messages_for_ui(messages)
        if not events:
            if announce and messages is not None:
                # Only announce emptiness on explicit /switch restore.
                self.append_event("(empty session transcript)", "dim")
            return

        n_user = n_answer = n_tools = n_thought = 0
        for ev in events:
            kind = ev.kind
            if kind == "user":
                self.append_user(ev.text, images=getattr(ev, "images", None) or None)
                n_user += 1
            elif kind == "thought":
                # Historical thoughts: collapsed, elapsed unknown.
                self.commit_thought(0.0, ev.text)
                n_thought += 1
            elif kind == "tools":
                self._render_restored_tools(ev.tool_calls, ev.tool_results)
                n_tools += 1
            elif kind == "answer":
                self.commit_answer(ev.text)
                n_answer += 1

        # Hydrate session token chrome from AIMessage usage_metadata.
        self._apply_restored_usage(messages)

        if announce:
            self.append_event(
                f"restored transcript: {n_user} user / {n_answer} answers"
                f" / {n_tools} tool groups / {n_thought} thoughts"
                f"  ({len(messages)} msgs)",
                "dim",
            )
        # Jump to bottom after paint.
        self.call_after_refresh(self._scroll_timeline)

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

    def _open_compression_diagnostics(self) -> None:
        from synapse.ui.dialogs import CompressionDiagnosticsDialog

        self._reload_tool_output_stats()
        self.push_screen(
            CompressionDiagnosticsDialog(self._tool_output_repo, self.thread_id),
        )

    def _open_model_dialog(self, _args: list[str]) -> None:
        from synapse.ui.dialogs import ModelPickerDialog

        self.push_screen(
            ModelPickerDialog(self.settings),
            self._on_model_dialog_done,
        )

    def _on_model_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, value = result
        if action == "model":
            self._apply_model_switch(value)
        elif action == "thinking":
            self._apply_thinking_switch(value)

    def _apply_model_switch(self, alias: str) -> None:
        self._switch_model_bg(f"/model {alias}", f"switching model to {alias}")

    def _apply_thinking_switch(self, level: str) -> None:
        self._switch_model_bg(f"/model thinking {level}", f"thinking -> {level}")

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

    def _open_theme_dialog(self) -> None:
        from synapse.ui.dialogs import ThemePickerDialog

        self.push_screen(
            ThemePickerDialog(self.settings, project_root=self.project_root),
            self._on_theme_dialog_done,
        )

    def _open_theme_designer(self) -> None:
        from synapse.ui.dialogs.theme_designer import ThemeDesignerDialog

        self.push_screen(
            ThemeDesignerDialog(self.settings, project_root=self.project_root),
            self._on_theme_dialog_done,
        )

    def _on_theme_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, name = result
        if action == "theme":
            try:
                self.apply_theme(str(name), persist=True, announce=True)
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"theme failed: {exc}", "yellow")

    def _open_codex_import_dialog(self) -> None:
        if self._busy:
            self.append_event("still running previous turn…", "yellow")
            return
        from synapse.ui.dialogs import CodexSessionListDialog

        self.push_screen(
            CodexSessionListDialog(self.settings),
            self._on_codex_import_dialog_done,
        )

    def _on_codex_import_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, native_id = result
        if action == "codex-import" and native_id:
            self._start_codex_import(str(native_id))

    def _start_codex_import(self, native_id: str) -> None:
        if self._busy:
            self.append_event("still running previous turn…", "yellow")
            return
        self._capture_turn_context()
        self._busy = True
        self.set_activity("importing", "importing Codex session", True)
        self.flash_status("importing Codex session…", "dim")
        self._sync_prompt_placeholder()
        self._import_codex_session_bg(native_id)

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

    def _finish_codex_import(self, result: Any) -> None:
        self._apply_session_switch(str(result.thread_id))
        status = "reused" if result.reused else "recovered" if result.recovered else "imported"
        self.flash_status(f"Codex session {status}: {result.thread_id}", "dim")

    def _open_session_dialog(self, parts: list[str]) -> None:
        mode = "switch"
        if len(parts) >= 2 and parts[1].casefold() in {"delete", "del", "rm"}:
            mode = "delete"
        elif len(parts) >= 2 and parts[1].casefold() in {"switch", "sel"}:
            mode = "switch"
        elif len(parts) >= 2 and parts[1].casefold() in {"multi_delete", "multi"}:
            mode = "multi_delete"
        from synapse.ui.dialogs import SessionListDialog

        self.push_screen(
            SessionListDialog(
                self.settings,
                current_thread=self.thread_id,
                mode=mode,
            ),
            self._on_session_dialog_done,
        )

    def _on_session_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, thread_ids = result
        # Always a list now (single-select modes wrap in list too).
        if action == "switch":
            if thread_ids:
                self._apply_session_switch(thread_ids[0])
        elif action == "delete" or action == "multi_delete":
            if thread_ids:
                self._apply_session_multi_delete(thread_ids)

    def _apply_session_multi_delete(self, thread_ids: list[str]) -> None:
        """Batch delete sessions, one by one."""
        from synapse.commands.slash_cmds import handle_slash

        deleted = 0
        failed = 0
        for tid in thread_ids:
            try:
                ok = handle_slash(
                    f"/session delete {tid}",
                    settings=self.settings,
                    agent=self.agent,
                    thread_id=self.thread_id,
                    project_root=self.project_root,
                )
                if ok:
                    deleted += 1
                else:
                    failed += 1
            except Exception:  # noqa: BLE001
                failed += 1
        if deleted:
            self.append_event(
                f"Deleted {deleted} session{'s' if deleted != 1 else ''}",
                "green",
            )
        if failed:
            self.append_event(
                f"Failed to delete {failed} session{'s' if failed != 1 else ''}",
                "yellow",
            )
        # If current session was among the deleted ones, the last
        # handle_slash call for the current thread_id will have switched
        # or will show an error — the apply_ok_result below handles that.
        self._apply_ok_result(deleted > 0)

    def _apply_session_switch(self, thread_id: str) -> None:
        from synapse.commands.slash_cmds import handle_slash

        try:
            ok = handle_slash(
                f"/switch {thread_id}",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_event(f"switch failed: {exc}", "yellow")
            return
        self._apply_ok_result(ok)

    def _apply_session_delete(self, thread_id: str) -> None:
        from synapse.commands.slash_cmds import handle_slash

        try:
            ok = handle_slash(
                f"/session delete {thread_id}",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_event(f"delete failed: {exc}", "yellow")
            return
        self._apply_ok_result(ok)

    def _open_mcp_dialog(self) -> None:
        from synapse.ui.dialogs import McpPanelDialog

        self.push_screen(
            McpPanelDialog(
                self.settings,
                project_root=self.project_root,
            ),
            self._on_mcp_dialog_done,
        )

    def _open_subagent_monitor(self) -> None:
        from synapse.ui.dialogs import SubagentMonitorDialog

        self.push_screen(SubagentMonitorDialog(self._subagent_monitor))

    def _on_mcp_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action = result[0] if result else None
        if action == "mcp-reload":
            self._apply_mcp_reload()
        elif action == "mcp-save":
            to_save = result[1] if len(result) > 1 else {}
            self._apply_mcp_save(to_save)
        elif action == "mcp-toggle-server":
            server_name = result[1] if len(result) > 1 else ""
            if server_name:
                self._apply_mcp_server_toggle(server_name)

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

    def _apply_mcp_save(self, to_save: dict[str, list[str] | None]) -> None:
        """Write include_tools to config, then reload — all on a worker thread."""
        if not to_save:
            self._apply_mcp_reload()
            return
        if getattr(self, "_mcp_reloading", False):
            return
        self._mcp_reloading = True
        self.set_activity("switching", "saving MCP config\u2026", True)
        self._apply_mcp_save_bg(to_save)

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

    def _apply_mcp_reload(self) -> None:
        """Dispatch MCP reload to a background worker so the UI stays responsive."""
        if getattr(self, "_mcp_reloading", False):
            return
        self._mcp_reloading = True
        self.set_activity("switching", "reloading MCP\u2026", True)
        self._apply_mcp_reload_bg()

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

    def _open_safety_dialog(self) -> None:
        from synapse.ui.dialogs import SafetyPanelDialog

        self.push_screen(
            SafetyPanelDialog(self.settings),
            self._on_safety_dialog_done,
        )

    def _on_safety_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, profile = result
        if action == "safety":
            from synapse.commands.slash_cmds import handle_slash

            try:
                ok = handle_slash(
                    f"/safety {profile}",
                    settings=self.settings,
                    agent=self.agent,
                    thread_id=self.thread_id,
                    project_root=self.project_root,
                )
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"safety switch failed: {exc}", "yellow")
                return
            self._apply_ok_result(ok)

    def _apply_ok_result(self, ok: object, notice_ttl: float = 4.0) -> None:
        """Apply a SlashResult returned by handle_slash after a dialog pick."""
        agent = getattr(ok, "agent", None)
        if agent is not None:
            self.agent = agent
            self._bind_steer_queue()
        thread_id = getattr(ok, "thread_id", None)
        if thread_id is not None and thread_id != self.thread_id:
            self.thread_id = thread_id
            self.action_clear_log()
            self._reset_session_token_chrome()
            self._reload_tool_output_stats()
        if getattr(ok, "clear_log", False):
            self.action_clear_log()
        if agent is not None or getattr(ok, "settings_changed", False):
            self.sub_title = model_status_label(self.settings)
            self._render_status()
        if getattr(ok, "reload_transcript", False):
            self._restore_session_transcript(announce=True)
        theme_name = getattr(ok, "theme_name", None)
        if theme_name:
            try:
                self.apply_theme(str(theme_name), persist=False, announce=False)
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"theme apply failed: {exc}", "yellow")
        _notice = (getattr(ok, "notice", None) or "").strip()
        if _notice and not getattr(ok, "error", False):
            self.flash_status(_notice, "dim", ttl=notice_ttl)
        else:
            lines = getattr(ok, "lines", []) or []
            if notice_ttl != 4.0 and len(lines) <= 2:
                cleaned = [str(x).strip() for x in lines if str(x or "").strip()]
                if cleaned and sum(len(x) for x in cleaned) <= 140:
                    style = "yellow" if getattr(ok, "error", False) else "dim"
                    self.flash_status(" · ".join(cleaned), style, ttl=notice_ttl)
                else:
                    self._emit_system_lines(lines, error=bool(getattr(ok, "error", False)))
            else:
                self._emit_system_lines(
                    lines,
                    error=bool(getattr(ok, "error", False)),
                )
        self._reload_session_title()
        self._refresh_topbar()

    # -- input / turn ----------------------------------------------------

    def _handle_slash(self, text: str) -> bool:
        """Handle local slash commands. Return True if consumed."""
        from synapse.commands.slash_cmds import handle_slash

        if self.agent is None:
            low = text.strip().split()[0].casefold() if text.strip() else ""
            if low not in {
                "/quit", "/exit", "/help", "/?", "/clear",
                "/theme", "/model", "/switch", "/safety",
            }:
                self.append_event(
                    "agent still starting — try again in a moment",
                    "yellow",
                )
                return True

        # ---- dialog-capable commands (push ModalScreen) ----
        raw = (text or "").strip()
        parts = raw.split()
        cmd = parts[0].casefold() if parts else ""

        if cmd in {"/compression", "/tool-output", "/tool-compress"} and len(parts) == 1:
            self._open_compression_diagnostics()
            return True
        if cmd == "/model" and len(parts) == 1:
            self._open_model_dialog(parts[1:])
            return True
        if cmd == "/model":
            # Args form (/model <alias> [thinking ...]): rebuild in background.
            self._switch_model_bg(raw, f"model {' '.join(parts[1:])}")
            return True
        if cmd == "/switch" and len(parts) == 1:
            self._open_session_dialog(["switch"])
            return True
        if cmd == "/session" and len(parts) >= 2 and parts[1].casefold() in {"delete", "del", "rm"}:
            # /session delete (without thread_id) → pick from list
            if len(parts) == 2:
                self._open_session_dialog(parts)
                return True
        if cmd == "/codex":
            if len(parts) == 1 or (len(parts) == 2 and parts[1].casefold() == "import"):
                self._open_codex_import_dialog()
                return True
            if len(parts) == 3 and parts[1].casefold() == "import":
                self._start_codex_import(parts[2])
                return True
            self.append_event("usage: /codex import [native_id]", "yellow")
            return True
        if cmd == "/theme" and (len(parts) == 1 or parts[1].casefold() in {"list", "ls"}):
            self._open_theme_dialog()
            return True
        if cmd == "/mcp" and len(parts) == 1:
            self._open_mcp_dialog()
            return True
        if cmd in {"/subagents", "/agents"} and len(parts) == 1:
            self._open_subagent_monitor()
            return True
        if cmd == "/safety" and len(parts) == 1:
            self._open_safety_dialog()
            return True
        if cmd == "/select":
            self.action_open_selectable_view()
            return True

        prev_thread = self.thread_id
        result = handle_slash(
            text,
            settings=self.settings,
            agent=self.agent,
            thread_id=self.thread_id,
            project_root=self.project_root,
        )
        if not result.handled:
            return False
        if result.exit_requested:
            self.exit()
            return True

        if result.agent is not None:
            self.agent = result.agent
            self._bind_steer_queue()

        thread_changed = False
        if result.thread_id is not None and result.thread_id != prev_thread:
            self.thread_id = result.thread_id
            thread_changed = True

        if result.clear_log or thread_changed:
            self.action_clear_log()
            if thread_changed:
                self._reset_session_token_chrome()

        # Title may change via /rename, /switch, /new, first-message bind, etc.
        self._reload_session_title()
        self._refresh_topbar()
        if result.agent is not None or getattr(result, "settings_changed", False):
            self.sub_title = model_status_label(self.settings)
            self._render_status()

        # Restore visual history after switch/new. LLM context follows thread_id
        # via checkpointer; this only rebuilds the transcript chrome.
        if getattr(result, "reload_transcript", False):
            self._restore_session_transcript(announce=True)
            self._refresh_topbar()

        theme_name = getattr(result, "theme_name", None)
        if theme_name:
            try:
                self.apply_theme(str(theme_name), persist=False, announce=False)
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"theme apply failed: {exc}", "yellow")

        _notice = (getattr(result, "notice", None) or "").strip()
        _has_lines = bool([x for x in (result.lines or []) if str(x or "").strip()])
        _markdown = getattr(result, "markdown", None)
        if isinstance(_markdown, str) and _markdown.strip():
            self._mount_markdown_block(_markdown)
        elif _notice or _has_lines:
            self._dismiss_welcome()
        if isinstance(_markdown, str) and _markdown.strip():
            pass  # already rendered
        elif _notice and not result.error:
            self.flash_status(_notice, "dim")
        else:
            self._emit_system_lines(result.lines, error=bool(result.error))

        # HITL: /approve or /reject resumes the paused graph.
        resume_action = getattr(result, "resume_action", None)
        if resume_action:
            if self.agent is None:
                self.append_event("agent not ready — cannot resume HITL", "yellow")
                return True
            if self._busy:
                self.append_event("still running previous turn…", "yellow")
                return True
            self._capture_turn_context()
            self._busy = True
            self.set_activity("tool", f"HITL {resume_action}", True)
            self.run_resume(
                str(resume_action),
                getattr(result, "resume_message", None),
            )
        return True

    @on(Input.Submitted, "#prompt")
    def handle_submit(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return

        # 将被截断的粘贴占位符替换回完整原始文本
        for placeholder, full_text in list(self._paste_replacements.items()):
            if placeholder in text:
                text = text.replace(placeholder, full_text)
        self._paste_replacements.clear()

        # Parse [image#N] placeholders from text and resolve to attachments.
        ids = find_placeholders(text)
        attachments: list[Any] = []
        if ids:
            seen: set[int] = set()
            for pid in ids:
                if pid in seen:
                    continue
                seen.add(pid)
                att = self._image_bank.items.get(pid)
                if att is not None:
                    attachments.append(att)

        try:
            self._input_history.add(text)
        except Exception:  # noqa: BLE001
            pass
        if self._handle_slash(text):
            self._image_bank.clear()
            return
        if self._busy:
            # Mid-run guidance: queue only (panel + prompt mode). No transcript/status.
            self._bind_steer_queue()
            q = self._turn_steer_queue()
            if q is not None:
                pending = q.push(text)
                if pending:
                    return
            self.append_event("still running previous turn…", "yellow")
            return
        try:
            from synapse.sessions.store import SessionStore

            SessionStore(self.settings.resolved_sessions_path()).touch(
                self.thread_id,
                title_hint=text,
                model=str(self.settings.model),
            )
            self._reload_session_title()
            self._refresh_topbar()
        except Exception:  # noqa: BLE001
            pass

        # Snapshot image bank BEFORE clear so run_turn retains data.
        turn_images = list(attachments)
        resolved_ids = {a.id for a in attachments}
        not_found = [f"[image#{pid}]" for pid in ids if pid not in resolved_ids]
        if not_found:
            # Keep bank + restore prompt; do not send a half-image turn.
            self.append_event(
                f"missing images: {' '.join(not_found)} (not sent)",
                "yellow",
            )
            prompt = self.query_one("#prompt", Input)
            prompt.value = text
            prompt.focus()
            return

        self._image_bank.clear()
        display = text

        self.append_user(display, images=turn_images or None)
        self._capture_turn_context()
        self._busy = True
        self._skip_steer_followup = False
        self._cancel_event = threading.Event()
        self._last_tool_items = []
        self._live_tool_items = []
        self._live_tool_summary = ""
        self._live_tool_block = None
        self._subagent_monitor.reset()
        self.clear_stream()
        self.set_activity("thinking", "starting", True)
        self._sync_prompt_placeholder()
        self.run_turn(text, turn_images or None)

    @work(thread=True, exclusive=True)
    def run_turn(self, text: str, attachments: list[Any] | None = None) -> None:
        if not self._agent_ready.wait(timeout=180):
            self.call_from_thread(
                self.append_event,
                "agent start timeout (180s)",
                "bold red",
            )
            self.call_from_thread(self._turn_done)
            return
        turn_agent = self._active_turn_agent or self.agent
        turn_thread_id = self._active_turn_thread_id or self.thread_id
        if self._agent_error or turn_agent is None:
            self.call_from_thread(
                self.append_event,
                f"agent unavailable: {self._agent_error or 'not built'}",
                "bold red",
            )
            self.call_from_thread(self._turn_done)
            return

        self._begin_turn_usage()
        sink = TextualStreamSink(self)
        config = {
            "configurable": {
                "thread_id": turn_thread_id,
                MONITOR_CONFIG_KEY: self._subagent_monitor.monitor_id,
            },
            "max_concurrency": self.settings.max_concurrency,
        }
        provider = provider_from_settings(self.settings)
        # None / empty attachments keep plain-string content (legacy path).
        atts = list(attachments or [])
        content = compose_user_content(
            text,
            attachments=atts if atts else None,
            provider=provider,
        )
        payload = {"messages": [{"role": "user", "content": content}]}
        try:
            result = stream_agent(
                turn_agent,
                payload,
                config,
                token_stream=self.settings.token_stream,
                prefer_async=True,
                max_concurrency=self.settings.max_concurrency,
                sink=sink,
                cancel_event=self._cancel_event,
            )
            if getattr(result, "cancelled", False):
                self._skip_steer_followup = True
                self.call_from_thread(
                    self.append_event,
                    "已终止（上下文已保留）。可继续输入。",
                    "yellow",
                )
                return
            # Session token totals for chrome: input / cache / output.
            if (
                result.input_tokens
                or result.output_tokens
                or getattr(result, "cache_tokens", 0)
                or result.total_tokens
                or getattr(result, "last_input_tokens", 0)
            ):
                # Idempotent with live note_usage: baseline + turn totals.
                self.call_from_thread(
                    self.apply_turn_usage,
                    turn_input=int(result.input_tokens or 0),
                    turn_output=int(result.output_tokens or 0),
                    turn_cache=int(getattr(result, "cache_tokens", 0) or 0),
                    last_input=int(
                        getattr(result, "last_input_tokens", 0)
                        or result.input_tokens
                        or 0
                    ),
                    last_output=int(
                        getattr(result, "last_output_tokens", 0)
                        or result.output_tokens
                        or 0
                    ),
                    last_cache=int(getattr(result, "last_cache_tokens", 0) or 0),
                )

            if getattr(result, "compact_events", 0):
                self.call_from_thread(
                    self.append_event,
                    f"context compacted ×{result.compact_events}",
                    "dim",
                )

            if not result.streamed_answer:
                answer = result.final_text or extract_last_ai_text(result.state)
                if answer:
                    self.call_from_thread(self.commit_answer, answer)
                elif getattr(result, "interrupted", False):
                    self.call_from_thread(
                        self.append_event,
                        "HITL: use /approve or /reject",
                        "yellow",
                    )
                else:
                    self.call_from_thread(self.append_event, "(empty response)", "dim")
            elif getattr(result, "interrupted", False):
                self.call_from_thread(
                    self.append_event,
                    "HITL: use /approve or /reject",
                    "yellow",
                )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.append_event, f"ERROR: {exc}", "bold red")
        finally:
            self.call_from_thread(self._turn_done)

    @work(thread=True, exclusive=True)
    def run_resume(self, action: str, message: str | None = None) -> None:
        """Resume graph after /approve or /reject."""
        from synapse.runtime.hitl import (
            build_decisions,
            build_resume_payload,
            extract_pending_interrupt,
            format_interrupt_lines,
        )

        turn_agent = self._active_turn_agent or self.agent
        turn_thread_id = self._active_turn_thread_id or self.thread_id
        if turn_agent is None:
            self.call_from_thread(self.append_event, "agent unavailable", "bold red")
            self.call_from_thread(self._turn_done)
            return
        self._begin_turn_usage()
        sink = TextualStreamSink(self)
        # Allow Esc to abort resume stream as well.
        self._cancel_event = threading.Event()
        config = {
            "configurable": {
                "thread_id": turn_thread_id,
                MONITOR_CONFIG_KEY: self._subagent_monitor.monitor_id,
            },
            "max_concurrency": self.settings.max_concurrency,
        }
        try:
            pending = extract_pending_interrupt(turn_agent, config)
            if pending is None or (not pending.actions and not pending.raw):
                self.call_from_thread(self.append_event, "no pending approval", "yellow")
                return
            for line in format_interrupt_lines(pending):
                self.call_from_thread(self.append_event, line, "dim")
            decisions = build_decisions(pending, action=action, message=message)
            payload = build_resume_payload(decisions)
            result = stream_agent(
                turn_agent,
                payload,
                config,
                token_stream=self.settings.token_stream,
                prefer_async=True,
                max_concurrency=self.settings.max_concurrency,
                sink=sink,
                cancel_event=self._cancel_event,
            )
            if getattr(result, "cancelled", False):
                self._skip_steer_followup = True
                self.call_from_thread(
                    self.append_event,
                    "已终止（上下文已保留）。可继续输入。",
                    "yellow",
                )
                return
            if (
                result.input_tokens
                or result.output_tokens
                or getattr(result, "cache_tokens", 0)
                or result.total_tokens
                or getattr(result, "last_input_tokens", 0)
            ):
                # Idempotent with live note_usage: baseline + turn totals.
                self.call_from_thread(
                    self.apply_turn_usage,
                    turn_input=int(result.input_tokens or 0),
                    turn_output=int(result.output_tokens or 0),
                    turn_cache=int(getattr(result, "cache_tokens", 0) or 0),
                    last_input=int(
                        getattr(result, "last_input_tokens", 0)
                        or result.input_tokens
                        or 0
                    ),
                    last_output=int(
                        getattr(result, "last_output_tokens", 0)
                        or result.output_tokens
                        or 0
                    ),
                    last_cache=int(getattr(result, "last_cache_tokens", 0) or 0),
                )
            if not result.streamed_answer:
                answer = result.final_text or extract_last_ai_text(result.state)
                if answer:
                    self.call_from_thread(self.commit_answer, answer)
            if getattr(result, "interrupted", False):
                self.call_from_thread(
                    self.append_event,
                    "still waiting for approval — /approve or /reject",
                    "yellow",
                )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.append_event, f"ERROR: {exc}", "bold red")
        finally:
            self.call_from_thread(self._turn_done)

    def _turn_done(self) -> None:
        completed_queue = self._active_steer_queue or get_agent_steer_queue(self.agent)
        self._busy = False
        self._sync_prompt_placeholder()
        # An immediate middleware drain retains the panel while the turn is
        # active. Reconcile it now so applied guidance disappears at turn end.
        if completed_queue is not None:
            self._on_steer_items_changed(completed_queue.peek_items())
        try:
            self._commit_live_tools_to_log()
        except Exception:  # noqa: BLE001
            pass
        self.clear_stream()
        self.set_activity("idle", "ready", True)
        try:
            self._refresh_git_chrome()
        except Exception:  # noqa: BLE001
            pass
        self.query_one("#prompt", Input).focus()
        # If the model finished without another tool/model step, apply leftover
        # guidance as a follow-up turn (unless the run was Esc-cancelled).
        if getattr(self, "_skip_steer_followup", False):
            self._skip_steer_followup = False
            self._clear_turn_context()
            self._bind_steer_queue()
            self._note_session_recap_turn()
            return
        # Capture snapshot before steer follow-up may start another busy turn.
        self._note_session_recap_turn()
        if self._schedule_followup_steer(completed_queue):
            return
        self._clear_turn_context()
        self._bind_steer_queue()

    def _note_session_recap_turn(self) -> None:
        """Remember latest turn facts for idle recap."""
        user_text = ""
        if self._user_turns:
            user_text = getattr(self._user_turns[-1], "full_text", "") or ""
        try:
            self._session_recap.note_turn_done(
                time.monotonic(),
                user_text=user_text,
                tool_summary=self._last_tool_summary or "",
                tool_items=list(self._last_tool_items or []),
                answer_text=self._last_answer_text or "",
                turn_count=len(self._user_turns),
            )
        except Exception:  # noqa: BLE001
            pass

    def _prompt_has_draft(self) -> bool:
        try:
            prompt = self.query_one("#prompt", Input)
            return bool((prompt.value or "").strip())
        except Exception:  # noqa: BLE001
            return False

    def _maybe_show_session_recap(self) -> None:
        """After idle, mount one recap line (no slash command)."""
        if self._busy:
            return
        try:
            line = self._session_recap.try_fire(
                time.monotonic(),
                busy=self._busy,
                draft_nonempty=self._prompt_has_draft(),
            )
        except Exception:  # noqa: BLE001
            return
        if not line:
            return
        self.append_event(line, "dim")

    def _schedule_followup_steer(self, queue: SteerQueue | None) -> bool:
        if queue is None or queue.peek_count() <= 0:
            return False
        self._busy = True
        self._sync_prompt_placeholder()
        if self.call_after_refresh(self._start_followup_steer, queue):
            return True
        self._busy = False
        self._sync_prompt_placeholder()
        return False

    def _start_followup_steer(self, queue: SteerQueue) -> None:
        if self._cancel_event.is_set():
            self._skip_steer_followup = True
            self._turn_done()
            return
        if queue.peek_count() <= 0:
            self._busy = False
            self._sync_prompt_placeholder()
            self._clear_turn_context()
            self._bind_steer_queue()
            self.set_activity("idle", "ready", True)
            return
        self._maybe_followup_steer(queue)

    def _maybe_followup_steer(self, queue: SteerQueue | None = None) -> None:
        q = queue or get_agent_steer_queue(self.agent)
        if q is None or q.peek_count() <= 0:
            return
        items = q.drain()
        content = format_steer_message(items)
        if not content:
            return
        # Silent follow-up: model gets content; no transcript/status steer copy.
        if self._active_turn_agent is None:
            self._capture_turn_context()
        self._active_steer_queue = q
        self._busy = True
        self._skip_steer_followup = False
        self._cancel_event = threading.Event()
        self.clear_stream()
        self.set_activity("thinking", "", True)
        self._sync_prompt_placeholder()
        self.run_turn(content, None)

def run_tui(
    *,
    settings: Any,
    thread_id: str | None = None,
    env_path: Path | None = None,
    project_root: Path | None = None,
    cli_model: str | None = None,
) -> None:
    """Launch the Textual app; agent build is deferred off the UI thread by default."""
    try:
        from synapse.ui.theme import bootstrap_theme

        bootstrap_theme(getattr(settings, "theme", None), workspace=settings.workspace)
    except Exception:  # noqa: BLE001
        pass
    root = project_root or Path.cwd()
    tid = thread_id or "pending"
    try:
        from synapse.sessions.store import (
            SessionStore,
            apply_binding_to_settings,
            binding_from_settings,
            pick_startup_thread_id,
            resolve_startup_binding,
        )

        store = SessionStore(settings.resolved_sessions_path())
        try:
            store.prune_empty(except_ids=set())
        except Exception:  # noqa: BLE001
            pass
        tid, resumed = pick_startup_thread_id(store, thread_id, resume_last=True)
        binding = resolve_startup_binding(
            store, thread_id=tid if resumed else None, cli_model=cli_model
        )
        if binding is not None:
            apply_binding_to_settings(settings, binding)
        bind = binding_from_settings(settings)
        store.set_last_model_binding(bind)
    except Exception:  # noqa: BLE001
        from synapse.sessions.store import allocate_thread_id

        tid = thread_id or allocate_thread_id()

    defer = bool(getattr(settings, "tui_defer_agent", True))
    agent = None
    if not defer:
        agent = build_coding_agent(
            settings,
            project_root=root,
            load_mcp=bool(settings.enable_mcp)
            and bool(getattr(settings, "mcp_eager", False)),
        )
        if settings.enable_mcp and not getattr(agent, "_coding_mcp_attached", True):
            from synapse.app.agent import attach_mcp_to_agent

            agent = attach_mcp_to_agent(settings, agent, project_root=root)

    app = CodingAgentApp(
        agent=agent,
        settings=settings,
        thread_id=tid,
        env_path=env_path,
        project_root=root,
        defer_agent_build=defer,
    )
    app.run()