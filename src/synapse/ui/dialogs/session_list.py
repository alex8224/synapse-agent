"""Session list dialog — invoked by /switch, /session delete."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult

from synapse.ui.dialogs.base import DialogBase, OptionItem, SectionHeader


class SessionListDialog(DialogBase):
    """List sessions for switching or deletion.

    dismiss result:
      ("switch", thread_id)  → TUI should call /switch
      ("delete", thread_id)  → TUI should call /session delete
    """

    _title_icon = "≡"

    def __init__(
        self, settings: Any, *, current_thread: str, mode: str = "switch"
    ) -> None:
        super().__init__()
        self._settings = settings
        self._current_thread = current_thread
        self._mode = mode  # "switch" | "delete"
        if mode == "delete":
            self._title_keys = "↑↓ enter delete · esc"
        try:
            from synapse.sessions import SessionStore

            store = SessionStore(settings.resolved_sessions_path())
            self._sessions = store.list_nonempty(limit=50)
        except Exception:  # noqa: BLE001
            self._sessions = []

    @property
    def title_text(self) -> str:
        return "Sessions" if self._mode == "switch" else "Delete Session"

    def compose_body(self) -> ComposeResult:
        yield SectionHeader(
            "Select a session"
            if self._mode == "switch"
            else "Select a session to delete"
        )
        items: list[OptionItem] = []
        for s in self._sessions:
            title = (s.title or "").strip() or s.thread_id[:8]
            detail = f"{s.updated_at[:16] or '?'}"
            items.append(
                OptionItem(
                    key=s.thread_id,
                    label=title,
                    detail=detail,
                    selected=(s.thread_id == self._current_thread),
                )
            )
        self._items = items

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        body.set_options(self._items, mark="  ")

    def _on_apply(self) -> None:
        body = self.query_one("#dialog-body")
        key = body.selected_key
        if not key:
            self.dismiss(None)
            return
        self.dismiss((self._mode, key))

    def _on_selected(self, key: str | None) -> None:
        if key:
            self._on_apply()
        else:
            self.dismiss(None)