"""Selectable-text modal: full-conversation plain-text view with mouse selection.

Opened via ``Ctrl+Shift+V``.  Displays every user / assistant / tool / thought
message in chronological order inside a read-only ``TextArea`` where the user
can select text with the mouse and copy with ``Ctrl+C``.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static, TextArea

from synapse.ui.clipboard import copy_to_clipboard

_copy_to_clipboard = copy_to_clipboard


class SelectableTextModal(ModalScreen[None]):
    """Full-conversation plain-text view for selection & copy."""

    DEFAULT_CSS = """
    SelectableTextModal {
        align: center middle;
    }
    SelectableTextModal > Vertical {
        width: 90%;
        height: 85%;
        border: solid $theme-border;
        background: $theme-bg;
    }
    SelectableTextModal Header {
        dock: top;
        height: 1;
    }
    SelectableTextModal #sel-header {
        dock: top;
        height: 1;
        padding: 0 1;
        color: $theme-fg;
        background: $theme-bar;
        text-style: bold;
    }
    SelectableTextModal #sel-body {
        height: 1fr;
    }
    SelectableTextModal Footer {
        dock: bottom;
        height: 1;
    }
    SelectableTextModal #sel-footer {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: $theme-dim;
        background: $theme-bar;
    }
    SelectableTextModal Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("ctrl+c", "copy_selection", "Copy selection"),
    ]

    def __init__(self, transcript: str, *, char_count: int = 0) -> None:
        super().__init__()
        self._transcript = transcript
        self._char_count = char_count

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f" Conversation plain-text view — {self._char_count} chars | "
            "Mouse select + Enter to copy | Esc to close",
            id="sel-header",
        )
        yield TextArea(
            self._transcript,
            id="sel-body",
            read_only=True,
            soft_wrap=True,
            show_line_numbers=False,
        )
        yield Static(
            "  Drag to select text  |  Ctrl+C to copy selection  |  Esc / click blank area to close",
            id="sel-footer",
        )
        yield Footer()

    def on_mount(self) -> None:
        body = self.query_one("#sel-body", TextArea)
        body.focus()
        # Move cursor to start so the full content is available for selection.
        body.move_cursor((0, 0))

    def action_copy_selection(self) -> None:
        """Copy selected text to clipboard."""
        body = self.query_one("#sel-body", TextArea)
        selected = body.selected_text
        if selected:
            if _copy_to_clipboard(selected):
                try:
                    self.notify(
                        f"Copied {len(selected)} characters to clipboard",
                        timeout=2.0,
                        severity="information",
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            if _copy_to_clipboard(self._transcript):
                try:
                    self.notify(
                        f"Copied all {self._char_count} characters to clipboard",
                        timeout=2.0,
                        severity="information",
                    )
                except Exception:  # noqa: BLE001
                    pass

    def action_dismiss(self) -> None:
        self.dismiss()


def build_transcript_from_log(log_container: Any) -> str:
    """Walk ``#log`` children and build a chronological plain-text transcript.

    Recognised widget types:
        UserTurnBlock   → ``[User] ...``
        ThoughtBlock    → ``[Thought] ...``
        ToolGroupBlock  → ``[Tools] item1, item2, ...``
        AnswerBlock     → ``[Answer] ...``
        AnswerDivider   → ``---``
        Static          → fallback (plain text)
    """
    from textual.widgets import Static

    parts: list[str] = []

    for child in log_container.children:
        cls_name = type(child).__name__

        if cls_name == "UserTurnBlock":
            text = getattr(child, "body", "")
            if text:
                parts.append(_fmt_user(text))

        elif cls_name == "ThoughtBlock":
            text = getattr(child, "body", "")
            elapsed = getattr(child, "elapsed", 0)
            if text:
                parts.append(_fmt_thought(text, elapsed))

        elif cls_name == "ToolGroupBlock":
            summary = getattr(child, "summary", "")
            items = getattr(child, "items", None) or []
            parts.append(_fmt_tools(summary, items))

        elif cls_name == "AnswerBlock":
            text = getattr(child, "body", "")
            if text:
                parts.append(_fmt_answer(text))

        elif cls_name == "AnswerDivider":
            parts.append("─" * 60)

        elif isinstance(child, Static):
            rendered = child.render()
            if rendered:
                from rich.text import Text as RichText

                if isinstance(rendered, RichText):
                    plain = rendered.plain.strip()
                else:
                    plain = str(rendered).strip()
                if plain:
                    parts.append(plain)

    return "\n\n".join(parts)


def _fmt_user(text: str) -> str:
    return f"═══ User ═══\n{text.strip()}"


def _fmt_thought(text: str, elapsed: float = 0) -> str:
    header = f"─── Thought ({(elapsed or 0):.0f}s) ───"
    return f"{header}\n{text.strip()}"


def _fmt_tools(summary: str, items: list[Any]) -> str:
    header = f"─── Tools: {summary} ───"
    lines = [header]
    for item in items:
        name = getattr(item, "name", str(item))
        status = getattr(item, "status", "")
        mark = {"ok": "+", "error": "x", "running": "…", "cancelled": "-"}.get(
            status, " "
        )
        lines.append(f"  [{mark}] {name}")
    return "\n".join(lines)


def _fmt_answer(text: str) -> str:
    return f"─── Answer ───\n{text.strip()}"
