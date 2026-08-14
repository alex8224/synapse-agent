"""TUI style constants: palette slots, marks, and the app stylesheet.

Palette slots are module globals tracked from ``synapse.ui.theme.get_theme()``
via ``_sync_theme_colors`` so render paths stay cheap (no per-frame lookups).
Consumers must read them through this module (``tui_styles._C_FG``) because the
theme can change at runtime and re-binding module names would freeze the old
values.

The ``CodingAgentApp`` stylesheet lives here as ``APP_CSS``; theme variables
(``$theme-*``) are resolved by Textual at parse time from the app's
``get_css_variables()``.
"""

from __future__ import annotations

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


# Text prefix for git branch (not emoji; terminal-safe branch mark).
_TOPBAR_BRANCH_MARK = "⎇"  # APL upwards vane / branch mark


_RAIL_PREVIEW_MAX = 28
_RAIL_BAR = "───"
_RAIL_BAR_DENSE = "━━━"
_RAIL_BAR_HEAVY = "▓▓▓"


APP_CSS = """
    Screen {
        layout: vertical;
        background: $theme-bg;
        color: $theme-fg;
    }
    /* Background-session completion notices. Keep them compact so a Markdown
       answer preview never obscures the transcript or prompt. */
    Toast {
        width: 52;
        max-width: 44%;
        margin-top: 1;
        margin-right: 1;
        padding: 1 2;
        background: $theme-bar;
        color: $theme-fg;
        border-left: thick $theme-green;
    }
    Toast .toast--title {
        color: $theme-green;
        text-style: bold;
    }
    Toast.-warning {
        border-left: thick $theme-orange;
    }
    Toast.-warning .toast--title {
        color: $theme-orange;
    }
    Toast.-error {
        border-left: thick $theme-error;
    }
    Toast.-error .toast--title {
        color: $theme-error;
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
