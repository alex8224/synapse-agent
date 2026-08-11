"""Ctrl+Tab recent-sessions switcher dialog.

Unlike the F4 ``SessionListDialog`` (persistent history from ``SessionStore``),
this dialog lists the most recently touched in-process sessions (active or
idle), capped at 10 rows.  It receives an already-built view model from the
host so it never touches runtime registries or databases itself.  Sessions
still doing observable work are marked with a dedicated status icon and bold
meta text; stale ones are muted.

dismiss result:
  ("switch_active_session", project_id, thread_id)  → TUI switches in place
  None                                             → user closed with Esc
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from synapse.runtime.sessions import ACTIVE_SESSION_STATUSES, SessionStatus
from synapse.ui.dialogs.base import DialogBase, OptionItem

# Row key separator: project_id and thread_id are hex/opaque ids, but keep the
# two halves unambiguous anyway.
_KEY_SEP = "\u001f"

#: Status marker + label reused across the dialog rows.  Active statuses get a
#: filled/busy marker and are shown bold; terminal/stale statuses stay muted.
_STATUS_VIEW: dict[SessionStatus, tuple[str, str]] = {
    SessionStatus.RUNNING: ("\u25cf", "running"),  # ●
    SessionStatus.WAITING_APPROVAL: ("\u25d0", "approval"),  # ◐
    SessionStatus.QUEUED: ("\u23f8", "queued"),  # ⏸
    SessionStatus.STARTING: ("\u23f8", "starting"),  # ⏸
    SessionStatus.CANCELLING: ("\u00d7", "cancelling"),  # ×
    SessionStatus.IDLE: ("\u25cb", "idle"),  # ○
    SessionStatus.COLD: ("\u25cb", "cold"),  # ○
    SessionStatus.CANCELLED: ("\u00d7", "cancelled"),  # ×
    SessionStatus.FAILED: ("\u25cb", "failed"),  # ○
    SessionStatus.CLOSED: ("\u25cb", "closed"),  # ○
}


@dataclass(frozen=True, slots=True)
class ActiveSessionItem:
    """One in-process active session row for the switcher view model."""

    project_id: str
    thread_id: str
    title: str
    project_label: str
    status: SessionStatus
    last_activity_at: datetime
    current: bool = False


class ActiveSessionSwitcherDialog(DialogBase):
    """Compact centered list of recent sessions with Tab-cyclic navigation."""

    _title_icon = "\u25c6"  # ◆

    BINDINGS = [
        *DialogBase.BINDINGS,
        Binding("tab", "next_item", "Next", show=False, priority=True),
        Binding("ctrl+tab", "next_item", "Next", show=False, priority=True),
        Binding("shift+tab", "prev_item", "Previous", show=False, priority=True),
    ]

    def __init__(
        self,
        items: list[ActiveSessionItem] | tuple[ActiveSessionItem, ...],
        *,
        width: int = 110,
    ) -> None:
        # Wider window than the default dialogs so a row can show the session
        # title, project label and status without truncating the title.
        super().__init__(width=width)
        self._items = list(items)

    @property
    def title_text(self) -> str:
        return "Recent Sessions"

    @property
    def _title_keys(self) -> str:
        return "tab next \u00b7 shift+tab prev \u00b7 enter switch \u00b7 esc"

    def compose_body(self) -> ComposeResult:
        if not self._items:
            yield Static("No recent sessions")
            return

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        if not self._items:
            return
        options = [self._to_option(item) for item in self._items]
        body.set_options(options, mark="  ")
        # Default cursor lands one past the current session (cyclic); when the
        # current session is not active it stays on the first row.
        if any(item.current for item in self._items):
            body.move_down_cyclic()

    @staticmethod
    def _to_option(item: ActiveSessionItem) -> OptionItem:
        icon, label = _STATUS_VIEW.get(item.status, ("\u25cb", item.status.value))  # ○ fallback
        active = item.status in ACTIVE_SESSION_STATUSES
        meta = f"{icon} {label}"
        if item.current:
            meta = f"{meta} \u00b7 current"
        title = (item.title or "").strip() or item.thread_id[:8]
        return OptionItem(
            key=f"{item.project_id}{_KEY_SEP}{item.thread_id}",
            label=title,
            detail=(item.project_label or "").strip(),
            meta=meta,
            selected=item.current,
            # Active rows stand out (bold status, default label color); stale
            # rows stay muted so recently-touched history reads at a glance.
            meta_style="bold" if active else "dim",
            label_style="" if active else "dim",
        )

    def action_next_item(self) -> None:
        body = self.query_one("#dialog-body")
        body.move_down_cyclic()

    def action_prev_item(self) -> None:
        body = self.query_one("#dialog-body")
        body.move_up_cyclic()

    def _on_apply(self) -> None:
        body = self.query_one("#dialog-body")
        self._on_selected(body.selected_key)

    def _on_selected(self, key: str | None) -> None:
        if not key:
            # Empty list: Enter does nothing; only Esc closes.
            return
        project_id, _, thread_id = key.partition(_KEY_SEP)
        self.dismiss(("switch_active_session", project_id, thread_id))
