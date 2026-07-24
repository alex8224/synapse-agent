"""Read-only Codex session picker for the Synapse TUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import ComposeResult

from synapse.ui.dialogs.base import DialogBase, OptionItem, SectionHeader


class CodexSessionListDialog(DialogBase):
    """List readable Codex sessions and return ``("codex-import", native_id)``."""

    _title_icon = "C"
    _title_keys = "up/down enter import · esc"

    def __init__(self, settings: Any, *, codex_home: Path | None = None) -> None:
        super().__init__()
        self._settings = settings
        self._codex_home = codex_home
        self._sessions: tuple[Any, ...] = ()
        self._warnings: tuple[str, ...] = ()
        try:
            from synapse.codex_history import CodexHistoryProjector
            from synapse.codex_sessions import CodexSessionScanner

            workspace = Path(getattr(settings, "workspace", Path.cwd()))
            result = CodexSessionScanner(codex_home).scan(
                workspace,
                include_rollout_fallback=True,
            )
            projector = CodexHistoryProjector()
            self._sessions = tuple(
                session
                for session in result.sessions
                if (
                    (snapshot := projector.project_path(session.rollout_path)).importable
                    and snapshot.messages
                )
            )
            skipped = len(result.sessions) - len(self._sessions)
            self._warnings = (*result.warnings, *(
                (f"{skipped} Codex session(s) have no importable visible text",)
                if skipped
                else ()
            ))
        except Exception:  # noqa: BLE001
            self._sessions = ()
            self._warnings = ("Codex session discovery failed",)

    @property
    def title_text(self) -> str:
        return "Import Codex Session"

    def compose_body(self) -> ComposeResult:
        if not self._sessions:
            yield SectionHeader("No Codex sessions with importable visible text")
            if self._warnings:
                yield SectionHeader(self._warnings[-1])
            return
        yield SectionHeader("Select a safe text snapshot to import")
        self._items = [
            OptionItem(
                key=session.native_id,
                label=session.title or "(untitled Codex session)",
                detail=f"{session.updated_at:%Y-%m-%d %H:%M}  {session.source}",
            )
            for session in self._sessions
        ]

    def on_mount(self) -> None:
        super().on_mount()
        if self._sessions:
            self.query_one("#dialog-body").set_options(self._items, mark="  ")

    def _on_apply(self) -> None:
        body = self.query_one("#dialog-body")
        key = body.selected_key
        self.dismiss(("codex-import", key) if key else None)

    def _on_selected(self, key: str | None) -> None:
        self.dismiss(("codex-import", key) if key else None)
