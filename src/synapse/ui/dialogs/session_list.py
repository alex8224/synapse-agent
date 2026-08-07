"""Session list dialog — invoked by F4, /switch, /session delete."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult

from synapse.ui.dialogs.base import DialogBase, OptionItem, SectionHeader


class SessionListDialog(DialogBase):
    """List sessions for switching, single deletion, or multi-select deletion.

    dismiss result:
      ("switch", [thread_id])   → TUI should call /switch
      ("delete", [thread_id])   → TUI should call /session delete (single)
      ("multi_delete", [...] )  → TUI should batch delete
    """

    _title_icon = "\u2261"  # ≡

    def __init__(
        self,
        settings: Any,
        *,
        current_thread: str,
        mode: str = "switch",
        runtime_status: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._current_thread = current_thread
        self._mode = mode  # "switch" | "delete" | "multi_delete"
        self._runtime_status = runtime_status or {}
        self._checkable = mode == "multi_delete"
        if self._checkable:
            self._title_keys = (
                "\u2191\u2193 space toggle \u00b7 ctrl+a all \u00b7 "
                "enter delete \u00b7 esc"
            )
        elif mode == "delete":
            self._title_keys = "\u2191\u2193 enter delete \u00b7 esc"
        try:
            from synapse.sessions.store import SessionStore

            with SessionStore(settings.resolved_sessions_path()) as store:
                self._sessions = store.list_nonempty(limit=50)
        except Exception:  # noqa: BLE001
            self._sessions = []

    @property
    def title_text(self) -> str:
        if self._checkable:
            return "Delete Sessions (multi-select)"
        return "Sessions" if self._mode == "switch" else "Delete Session"

    def compose_body(self) -> ComposeResult:
        if self._checkable:
            yield SectionHeader(
                "Select sessions to delete  \u00b7  "
                "Space=toggle  Ctrl+A=all  Enter=confirm"
            )
        elif self._mode == "switch":
            yield SectionHeader("Select a session")
        else:
            yield SectionHeader("Select a session to delete")
        items: list[OptionItem] = []
        for s in self._sessions:
            title = (s.title or "").strip() or s.thread_id[:8]
            detail = f"{s.updated_at[:16] or '?'}"
            status = self._runtime_status.get(s.thread_id)
            meta = f"[{status}]" if status else ""
            items.append(
                OptionItem(
                    key=s.thread_id,
                    label=title,
                    detail=detail,
                    meta=meta,
                    selected=(s.thread_id == self._current_thread),
                )
            )
        self._items = items

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        body.set_options(self._items, mark="  ", checkable=self._checkable)

    def _on_apply(self) -> None:
        body = self.query_one("#dialog-body")
        if self._checkable:
            keys = body.checked_keys
            if not keys:
                self.dismiss(None)
                return
            self.dismiss((self._mode, keys))
        else:
            key = body.selected_key
            if not key:
                self.dismiss(None)
                return
            self.dismiss((self._mode, [key]))

    def _on_selected(self, key: str | None) -> None:
        # In multi-select mode, Enter also confirms (apply).
        self._on_apply()