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

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static, Tree

# Session titles are truncated manually so a long CJK word never reaches
# Textual's word-wrap path and clips the row. The Tree itself owns indentation,
# guides, selection, keyboard navigation, and scrolling.
_TITLE_MAX_CELLS = 26
_META_MAX_CELLS = 9
_PROJECT_META_MAX_CELLS = 14
_DIR_MAX_CELLS = 30
_MAX_DRAWER_SESSIONS = 1_000


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


@dataclass(frozen=True)
class _TreeItem:
    """Data attached to one interactive project/session tree node."""

    kind: str
    project_id: str
    thread_id: str = ""


class ProjectDrawer(ModalScreen[Any]):
    """Left-docked floating panel with project/session navigation."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("ctrl+n", "new_session", "New", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        current_project_id: str,
        current_thread_id: str,
        runtime_status: dict[str, str] | None = None,
        runtime_status_provider: Callable[[], dict[str, str]] | None = None,
        runtime_status_by_project_provider: (
            Callable[[], dict[str, dict[str, str]]] | None
        ) = None,
        catalog_path: Path | str | None = None,
    ) -> None:
        super().__init__()
        # ``Screen.DEFAULT_CSS`` may otherwise win the root background cascade
        # on non-ANSI (solid) themes and paint an opaque full-screen layer that
        # hides everything behind the drawer. Inline styles have the required
        # priority (same fix as ThemeDesignerScreen); the drawer window itself
        # stays opaque via ``#drawer-window``.
        self.styles.background = "transparent"
        self._current_project_id = current_project_id
        self._current_thread_id = current_thread_id
        self._runtime_status = runtime_status or {}
        self._runtime_status_provider = runtime_status_provider
        self._runtime_status_by_project_provider = runtime_status_by_project_provider
        self._runtime_status_by_project: dict[str, dict[str, str]] = {}
        self._catalog_path = catalog_path
        self._rows: list[_Row] = []
        self._selected = 0
        self._projects: list[Any] = []

    def _refresh_status_data(self) -> None:
        """Pull the latest live runtime status (thread and project views)."""
        provider = self._runtime_status_provider
        if provider is not None:
            try:
                self._runtime_status = dict(provider())
            except Exception:  # noqa: BLE001 - drawer chrome is best-effort
                pass
        by_provider = self._runtime_status_by_project_provider
        if by_provider is not None:
            try:
                self._runtime_status_by_project = dict(by_provider())
            except Exception:  # noqa: BLE001 - drawer chrome is best-effort
                pass

    # -- data ---------------------------------------------------------------

    def _load(self) -> list[_Row]:
        from synapse.projects.catalog import ProjectCatalog

        catalog = None
        rows: list[_Row] = []
        self._refresh_status_data()
        try:
            catalog_path = self._catalog_path
            if catalog_path is None:
                from synapse.config import Settings

                try:
                    settings = Settings(_env_file=None, theme="cursor-dark")
                except Exception:  # noqa: BLE001
                    settings = None
                if settings is not None:
                    catalog_path = settings.resolved_catalog_path()
            if catalog_path is not None:
                catalog = ProjectCatalog(catalog_path)
            projects = catalog.list_projects(limit=100) if catalog is not None else []
        except Exception:  # noqa: BLE001
            projects = []
        try:
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
                            f"{project.session_count} sessions", _PROJECT_META_MAX_CELLS
                        ),
                        indent="",
                    )
                )
                sessions = self._sessions_for(catalog, project.project_id)
                sessions = self._merge_live_sessions(
                    sessions, project_id=project.project_id
                )
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
        finally:
            close = getattr(catalog, "close", None)
            if callable(close):
                close()

    def _merge_live_sessions(
        self, sessions: list[Any], *, project_id: str | None = None
    ) -> list[Any]:
        """Keep every in-memory runtime visible and sort active work first."""
        scope = project_id or self._current_project_id
        statuses = self._runtime_status_by_project.get(scope, self._runtime_status)
        by_thread = {str(session.thread_id): session for session in sessions}
        for thread_id in statuses:
            if thread_id in by_thread:
                continue
            by_thread[thread_id] = type(
                "LiveSession",
                (),
                {
                    "thread_id": thread_id,
                    "title": thread_id[:8],
                    "updated_at": "",
                },
            )()
        active = {"queued", "starting", "running", "cancelling", "waiting_approval"}
        return sorted(
            by_thread.values(),
            key=lambda session: (
                str(statuses.get(str(session.thread_id), "")) not in active,
                str(session.thread_id) != self._current_thread_id,
            ),
        )

    @staticmethod
    def _sessions_for(catalog: Any, project_id: str) -> list[Any]:
        if catalog is None:
            return []
        try:
            return catalog.list_sessions(
                project_id=project_id,
                limit=_MAX_DRAWER_SESSIONS,
            )
        except Exception:  # noqa: BLE001
            return []

    # -- composition --------------------------------------------------------

    DEFAULT_CSS = """
    ProjectDrawer {
        align: left top;
        /* Keep the screen layer transparent: this is an overlay, not a new
           column in the existing TUI layout. */
        background: transparent;
        scrollbar-size: 0 0;
    }
    ProjectDrawer > #drawer-window {
        width: 52;
        height: 100%;
        min-width: 40;
        max-width: 64;
        /* Opaque window, transparent screen: the underlying TUI remains
           visible around the drawer, matching DialogBase/F4 behavior. */
        background: $theme-bg;
        border-right: solid $theme-user;
        padding: 1 0;
        layout: vertical;
    }
    ProjectDrawer #drawer-title {
        width: 1fr;
        height: 2;
        padding: 0 2;
        color: $theme-fg;
        background: $theme-top;
        text-style: bold;
    }
    ProjectDrawer #drawer-hint {
        width: 1fr;
        height: 1;
        padding: 0 2;
        color: $theme-muted;
        background: $theme-top;
    }
    ProjectDrawer #drawer-tree {
        width: 1fr;
        height: 1fr;
        padding: 1 1;
        color: $theme-fg;
        background: transparent;
        scrollbar-size: 1 1;
        scrollbar-background: $theme-bg;
        scrollbar-color: $theme-user;
        scrollbar-background-hover: $theme-bar;
        scrollbar-color-hover: $theme-user;
        scrollbar-background-active: $theme-bar;
        scrollbar-color-active: $theme-user;
    }
    ProjectDrawer #drawer-tree:focus > .tree--cursor {
        background: $theme-bar;
        color: $theme-fg;
        text-style: bold;
    }
    ProjectDrawer #drawer-tree > .tree--cursor {
        background: $theme-bar 75%;
    }
    ProjectDrawer #drawer-tree > .tree--highlight-line {
        background: $theme-bar 45%;
    }
    """

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical

        with Vertical(id="drawer-window"):
            yield Static("≡  Projects / Sessions", id="drawer-title")
            yield Static("Enter / click: open · Space / ▶: expand", id="drawer-hint")
            yield Tree("Projects", id="drawer-tree", data=None)

    def on_mount(self) -> None:
        self._rows = self._load()
        tree = self.query_one("#drawer-tree", Tree)
        tree.show_root = False
        tree.auto_expand = False
        tree.root.remove_children()
        self._tree_nodes: dict[str, Any] = {}
        self._populate_tree(tree)
        tree.focus()
        if self._current_thread_id:
            node = self._tree_nodes.get(
                f"session:{self._current_project_id}:{self._current_thread_id}"
            )
            if node is not None:
                tree.move_cursor(node)
        if (
            self._runtime_status_provider is not None
            or self._runtime_status_by_project_provider is not None
        ):
            self.set_interval(0.25, self._refresh_live_status)

    def _refresh_live_status(self) -> None:
        if (
            self._runtime_status_provider is None
            and self._runtime_status_by_project_provider is None
        ):
            return
        before = (self._runtime_status, self._runtime_status_by_project)
        self._refresh_status_data()
        if (self._runtime_status, self._runtime_status_by_project) == before:
            return
        selected_key = ""
        tree = self.query_one("#drawer-tree", Tree)
        cursor = tree.cursor_node
        item = cursor.data if cursor is not None else None
        if isinstance(item, _TreeItem):
            selected_key = (
                f"session:{item.project_id}:{item.thread_id}"
                if item.kind == "session"
                else f"project:{item.project_id}"
            )
        self._rows = self._load()
        tree.root.remove_children()
        self._tree_nodes = {}
        self._populate_tree(tree)
        target = self._tree_nodes.get(selected_key)
        if target is not None:
            tree.move_cursor(target)

    def _populate_tree(self, tree: Tree[Any]) -> None:
        """Build the two-level project/session tree from the loaded rows."""
        project_nodes: dict[str, Any] = {}
        sessions_by_project: dict[str, list[_Row]] = {}
        for row in self._rows:
            if row.key.startswith("session:"):
                _, project_id, _ = row.key.split(":", 2)
                sessions_by_project.setdefault(project_id, []).append(row)
        current_project = self._current_project_id
        for row in self._rows:
            if row.key.startswith("project:"):
                project_id = row.key.removeprefix("project:")
                prefix = "● " if project_id == current_project else "○ "
                label = Text()
                label.append(prefix, style="bold cyan" if project_id == current_project else "dim")
                label.append(row.label, style="bold")
                if row.meta:
                    label.append(f"  {row.meta}", style="dim")
                node = tree.root.add(
                    label,
                    _TreeItem("project", project_id),
                    expand=project_id == current_project,
                    allow_expand=bool(sessions_by_project.get(project_id)),
                )
                project_nodes[project_id] = node
                self._tree_nodes[row.key] = node
                continue
            if not row.key.startswith("session:"):
                continue
            _, project_id, thread_id = row.key.split(":", 2)
            project_node = project_nodes.get(project_id)
            if project_node is None:
                continue
            label = Text()
            label.append(_truncate(row.label, _TITLE_MAX_CELLS))
            if row.meta:
                label.append(f"  {row.meta}", style="dim")
            node = project_node.add_leaf(
                label,
                _TreeItem("session", project_id, thread_id),
            )
            self._tree_nodes[row.key] = node

        if not project_nodes:
            tree.root.add_leaf("No projects registered yet", _TreeItem("empty", ""))

    def on_tree_node_selected(self, event: Tree.NodeSelected[Any]) -> None:
        """Open a session or project when a tree node is clicked/entered."""
        event.stop()
        item = event.node.data
        if not isinstance(item, _TreeItem):
            return
        if item.kind == "session":
            self._dismiss_switch(item.project_id, item.thread_id)
        elif item.kind == "project":
            if item.project_id != self._current_project_id:
                # Selecting another project is an explicit project switch;
                # clicking its disclosure arrow still only expands it.
                self._dismiss_switch(item.project_id, "")
            else:
                # The active project row is a useful keyboard target too:
                # Enter toggles it instead of dismissing the current screen.
                event.node.toggle()

    def _dismiss_switch(self, project_id: str, thread_id: str) -> None:
        if project_id == self._current_project_id:
            self.dismiss(("switch", project_id, thread_id))
        else:
            self.dismiss(("switch_project", project_id, thread_id))

    # -- keyboard -----------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    def action_new_session(self) -> None:
        self.dismiss(("new_session", self._current_project_id, ""))

    def action_open(self) -> None:
        """Compatibility action for callers that used the old flat drawer."""
        tree = self.query_one("#drawer-tree", Tree) if self.is_mounted else None
        node = tree.cursor_node if tree is not None else None
        item = node.data if node is not None else None
        if isinstance(item, _TreeItem):
            if item.kind == "session":
                self._dismiss_switch(item.project_id, item.thread_id)
            elif item.kind == "project" and item.project_id != self._current_project_id:
                self._dismiss_switch(item.project_id, "")
            return

        # Keep the old unit-level contract useful before the screen is mounted.
        sessions = [r for r in self._rows if r.key.startswith("session:")]
        if not sessions or not (0 <= self._selected < len(sessions)):
            return
        _, project_id, thread_id = sessions[self._selected].key.split(":", 2)
        self._dismiss_switch(project_id, thread_id)
