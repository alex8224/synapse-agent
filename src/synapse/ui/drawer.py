"""Floating project drawer: list projects, sessions, and runtime status.

Opened from the topbar workspace chrome (``≡``).  Renders as a two-level tree
over the live TUI (transparent modal layer, like the F4 dialogs, so the app
stays visible behind it):

- level 1: project directory (last workspace path segment)
- level 2: session titles, truncated to the drawer width

The current project's live runtime status is marked.  Selecting a session
dismisses with a switch request:

- same project  -> ``("switch", project_id, thread_id)`` (in-place)
- other project -> ``("switch_project", project_id, thread_id)`` (restart TUI)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.cells import cell_len
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static

# Drawer window is 44 cells: border (2) + padding (2) leaves 40 usable cells.
# Session rows spend 2 on the marker + 4 on the tree indent, so titles get at
# most 26 cells and the trailing status/date hint at most 9 (2 + 4 + 26 + 9 =
# 41, leaving one gap cell).  Titles are truncated manually so a long CJK word
# never reaches Textual's word-wrap path and clips the row.
_TITLE_MAX_CELLS = 26
_META_MAX_CELLS = 9
_DIR_MAX_CELLS = 30


def _dir_label(workspace_path: str) -> str:
    """Last path segment of a workspace (the tree's level-1 label)."""
    path = workspace_path.rstrip("/\\")
    last = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return last or workspace_path


def _truncate(text: str, max_cells: int) -> str:
    """Truncate ``text`` to at most ``max_cells`` display cells, adding "…"."""
    if max_cells <= 0:
        return ""
    if cell_len(text) <= max_cells:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        width = cell_len(ch)
        if used + width > max_cells - 1:  # reserve 1 cell for "…"
            break
        out.append(ch)
        used += width
    return "".join(out) + "\u2026"


@dataclass
class _Row:
    key: str
    label: str
    detail: str = ""
    meta: str = ""
    indent: str = ""


class ProjectDrawer(ModalScreen[Any]):
    """Left-docked floating panel with project/session navigation."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("up", "up", "Up", show=False, priority=True),
        Binding("down", "down", "Down", show=False, priority=True),
        Binding("enter", "open", "Open", show=False, priority=True),
        Binding("ctrl+n", "new_session", "New", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        current_project_id: str,
        current_thread_id: str,
        runtime_status: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._current_project_id = current_project_id
        self._current_thread_id = current_thread_id
        self._runtime_status = runtime_status or {}
        self._rows: list[_Row] = []
        self._selected = 0
        self._projects: list[Any] = []

    # -- data ---------------------------------------------------------------

    def _load(self) -> list[_Row]:
        from synapse.config import Settings
        from synapse.projects.catalog import ProjectCatalog

        try:
            settings = Settings(_env_file=None, theme="cursor-dark")
        except Exception:  # noqa: BLE001
            settings = None
        catalog = None
        rows: list[_Row] = []
        try:
            if settings is not None:
                catalog = ProjectCatalog(settings.resolved_catalog_path())
            projects = catalog.list_projects(limit=100) if catalog is not None else []
        except Exception:  # noqa: BLE001
            projects = []
        self._projects = list(projects)
        if not projects:
            rows.append(_Row("", "no projects registered yet", meta=""))
            return rows
        for project in projects:
            rows.append(
                _Row(
                    key=f"project:{project.project_id}",
                    label=_truncate(
                        _dir_label(project.workspace_path or project.name or ""),
                        _DIR_MAX_CELLS,
                    ),
                    detail=project.workspace_path,
                    meta=_truncate(
                        f"{project.session_count} sessions", _META_MAX_CELLS
                    ),
                    indent="",
                )
            )
            sessions = self._sessions_for(catalog, project.project_id)
            for session in sessions:
                thread_id = str(session.thread_id)
                status = self._runtime_status.get(thread_id)
                meta = f"[{status}]" if status else (session.updated_at[:10] or "")
                title = (session.title or "").strip() or thread_id[:8]
                rows.append(
                    _Row(
                        key=f"session:{project.project_id}:{thread_id}",
                        label=_truncate(title, _TITLE_MAX_CELLS),
                        detail=thread_id,
                        meta=_truncate(meta, _META_MAX_CELLS),
                        indent="    ",
                    )
                )
        return rows

    @staticmethod
    def _sessions_for(catalog: Any, project_id: str) -> list[Any]:
        if catalog is None:
            return []
        try:
            return catalog.list_sessions(project_id=project_id, limit=50)
        except Exception:  # noqa: BLE001
            return []

    # -- composition --------------------------------------------------------

    DEFAULT_CSS = """
    ProjectDrawer {
        align: left middle;
        /* Transparent modal layer (same approach as the F4 dialogs): the live
           TUI stays fully visible behind the floating drawer. */
        background: transparent;
    }
    ProjectDrawer > #drawer-window {
        width: 44;
        height: 100%;
        /* Translucent so the underlying TUI shows through the panel itself. */
        background: $theme-bg 85%;
        border-right: heavy $theme-user;
        padding: 1 1;
    }
    ProjectDrawer #drawer-title {
        text-style: bold;
        color: $theme-fg;
        padding: 0 0 1 0;
    }
    ProjectDrawer #drawer-body {
        color: $theme-fg;
        height: 1fr;
        overflow-y: auto;
    }
    """

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical

        with Vertical(id="drawer-window"):
            yield Static("≡  Projects / Sessions", id="drawer-title")
            yield Static("", id="drawer-body")

    def on_mount(self) -> None:
        self._rows = self._load()
        self._paint()

    def _paint(self) -> None:
        body = self.query_one("#drawer-body", Static)
        from rich.text import Text

        out = Text()
        current_project = self._current_project_id
        session_index = 0
        for row in self._rows:
            if row.key.startswith("project:"):
                prefix = (
                    "● " if row.key == f"project:{current_project}" else "○ "
                )
                line = Text()
                line.append(prefix, style="bold")
                line.append(row.label, style="bold")
                if row.meta:
                    line.append(f"  {row.meta}", style="dim")
                out.append("\n")
                out.append(line)
                out.append("\n")
            else:
                selected = session_index == self._selected
                mark = "▸" if selected else " "
                line = Text()
                line.append(mark + " ", style="bold cyan" if selected else "dim")
                line.append(row.indent, style="")
                line.append(row.label, style="")
                if row.meta:
                    line.append(f"  {row.meta}", style="dim")
                out.append(line)
                out.append("\n")
                session_index += 1
        body.update(out)

    # -- keyboard -----------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    def _session_rows(self) -> list[_Row]:
        return [r for r in self._rows if r.key.startswith("session:")]

    def action_up(self) -> None:
        sessions = self._session_rows()
        if sessions:
            self._selected = max(0, self._selected - 1)
            self._paint()

    def action_down(self) -> None:
        sessions = self._session_rows()
        if sessions:
            self._selected = min(len(sessions) - 1, self._selected + 1)
            self._paint()

    def action_open(self) -> None:
        sessions = self._session_rows()
        if not sessions or not (0 <= self._selected < len(sessions)):
            return
        row = sessions[self._selected]
        _, project_id, thread_id = row.key.split(":", 2)
        if project_id == self._current_project_id:
            self.dismiss(("switch", project_id, thread_id))
        else:
            self.dismiss(("switch_project", project_id, thread_id))

    def action_new_session(self) -> None:
        self.dismiss(("new_session", self._current_project_id, ""))
