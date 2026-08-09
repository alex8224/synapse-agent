"""Project drawer: data loading and switch request contracts."""

from __future__ import annotations

from typing import Any

from textual.app import App
from textual.widgets import Tree

from synapse.ui.drawer import ProjectDrawer


class _FakeProject:
    def __init__(self, project_id: str, name: str, workspace_path: str, session_count: int):
        self.project_id = project_id
        self.name = name
        self.workspace_path = workspace_path
        self.session_count = session_count


class _FakeSession:
    def __init__(self, thread_id: str, title: str, updated_at: str):
        self.thread_id = thread_id
        self.title = title
        self.updated_at = updated_at


class _FakeCatalog:
    def __init__(self, *a: Any, **k: Any) -> None:
        del a, k
        self.projects = [
            _FakeProject("p-1", "alpha", "/ws/a", 2),
            _FakeProject("p-2", "beta", "/ws/b", 1),
        ]
        self.sessions = {
            "p-1": [
                _FakeSession("t-a1", "session one", "2026-01-02T10:00:00"),
                _FakeSession("t-a2", "session two", "2026-01-03T10:00:00"),
            ],
            "p-2": [_FakeSession("t-b1", "session bee", "2026-01-04T10:00:00")],
        }

    def list_projects(self, limit: int = 100) -> list[Any]:
        del limit
        return self.projects

    def list_sessions(self, project_id: str, limit: int = 50) -> list[Any]:
        del limit
        return self.sessions.get(project_id, [])


def _drawer(
    monkeypatch: Any,
    *,
    runtime_status: dict[str, str] | None = None,
    extra_sessions: dict[str, list[Any]] | None = None,
) -> Any:
    from synapse.ui.drawer import ProjectDrawer

    catalog = _FakeCatalog()
    for project_id, sessions in (extra_sessions or {}).items():
        catalog.sessions.setdefault(project_id, []).extend(sessions)
    monkeypatch.setattr("synapse.projects.catalog.ProjectCatalog", lambda *a, **k: catalog)

    class _FakeSettings:
        def __init__(self, *a: Any, **k: Any) -> None:
            del a, k

        def resolved_catalog_path(self) -> str:
            return ":memory:"

    return ProjectDrawer(
        current_project_id="p-1",
        current_thread_id="t-a1",
        runtime_status=runtime_status,
        catalog_path=":memory:",
    )


def test_drawer_loads_projects_and_sessions(monkeypatch: Any) -> None:
    drawer = _drawer(monkeypatch)
    rows = drawer._load()
    project_keys = [r.key for r in rows if r.key.startswith("project:")]
    session_keys = [r.key for r in rows if r.key.startswith("session:")]
    assert project_keys == ["project:p-1", "project:p-2"]
    assert session_keys == [
        "session:p-1:t-a1",
        "session:p-1:t-a2",
        "session:p-2:t-b1",
    ]


def test_drawer_uses_last_directory_segment_as_project_label(
    monkeypatch: Any,
) -> None:
    drawer = _drawer(monkeypatch)
    rows = drawer._load()
    project_rows = [r for r in rows if r.key.startswith("project:")]
    # /ws/a -> "a", /ws/b -> "b" (last path segment, not project.name)
    assert [r.label for r in project_rows] == ["a", "b"]


def test_drawer_truncates_long_session_titles(monkeypatch: Any) -> None:
    long_title = "极长的会话标题" * 20
    drawer = _drawer(
        monkeypatch,
        extra_sessions={
            "p-2": [_FakeSession("t-long", long_title, "2026-01-05T10:00:00")]
        },
    )
    rows = drawer._load()
    row = next(r for r in rows if r.key == "session:p-2:t-long")
    assert len(row.label) < len(long_title)
    assert row.label.endswith("…")


def test_drawer_shows_live_sessions_first_without_separate_main_panel(
    monkeypatch: Any,
) -> None:
    drawer = _drawer(
        monkeypatch,
        runtime_status={"t-a2": "running", "live-only": "queued"},
    )

    rows = drawer._load()
    current_project_sessions = [
        row for row in rows if row.key.startswith("session:p-1:")
    ]

    assert [row.key for row in current_project_sessions[:2]] == [
        "session:p-1:t-a2",
        "session:p-1:live-only",
    ]
    assert [row.meta for row in current_project_sessions[:2]] == [
        "[running]",
        "[queued]",
    ]


def test_open_drawer_refreshes_live_runtime_status(monkeypatch: Any) -> None:
    import asyncio

    status = {"t-a2": "running"}

    class Host(App[None]):
        def get_css_variables(self) -> dict[str, str]:
            variables = super().get_css_variables()
            variables.update(
                {
                    "theme-bg": "#1a1b2e",
                    "theme-user": "#58a6ff",
                    "theme-fg": "#c0caf5",
                    "theme-top": "#1a1b2e",
                    "theme-muted": "#565f89",
                    "theme-bar": "#1f2335",
                }
            )
            return variables

        def on_mount(self) -> None:
            self.push_screen(
                ProjectDrawer(
                    current_project_id="p-1",
                    current_thread_id="t-a1",
                    runtime_status_provider=lambda: status,
                    catalog_path=":memory:",
                )
            )

    async def run() -> None:
        app = Host()
        with monkeypatch.context() as patch:
            patch.setattr("synapse.projects.catalog.ProjectCatalog", _FakeCatalog)
            async with app.run_test(size=(100, 20)) as pilot:
                await pilot.pause()
                drawer = app.screen
                assert isinstance(drawer, ProjectDrawer)
                first = drawer._tree_nodes["session:p-1:t-a2"]
                assert "[running]" in first.label.plain

                status["t-a2"] = "idle"
                drawer._refresh_live_status()
                await pilot.pause()

                refreshed = drawer._tree_nodes["session:p-1:t-a2"]
                assert "[idle]" in refreshed.label.plain
                await pilot.press("escape")

    asyncio.run(run())


def test_dir_label_last_segment() -> None:
    from synapse.ui.drawer import _dir_label

    assert _dir_label("/ws/a") == "a"
    assert _dir_label("/ws/a/") == "a"
    assert _dir_label(r"C:\ws\b") == "b"
    assert _dir_label("") == ""


def test_truncate_helper() -> None:
    from synapse.ui.drawer import _truncate

    assert _truncate("hello", 10) == "hello"
    assert _truncate("hello world", 8) == "hello w…"
    assert _truncate("", 5) == ""


def test_drawer_open_same_project_switches_in_place(monkeypatch: Any) -> None:
    drawer = _drawer(monkeypatch)
    drawer._rows = drawer._load()
    drawer._selected = 0  # session:p-1:t-a1
    result: list[Any] = []
    drawer.dismiss = lambda value: result.append(value)  # type: ignore[method-assign]
    drawer.action_open()
    assert result == [("switch", "p-1", "t-a1")]


def test_drawer_open_other_project_requests_restart(monkeypatch: Any) -> None:
    drawer = _drawer(monkeypatch)
    drawer._rows = drawer._load()
    drawer._selected = 2  # session:p-2:t-b1
    result: list[Any] = []
    drawer.dismiss = lambda value: result.append(value)  # type: ignore[method-assign]
    drawer.action_open()
    assert result == [("switch_project", "p-2", "t-b1")]


def test_drawer_new_session_requests_restart(monkeypatch: Any) -> None:
    drawer = _drawer(monkeypatch)
    result: list[Any] = []
    drawer.dismiss = lambda value: result.append(value)  # type: ignore[method-assign]
    drawer.action_new_session()
    assert result == [("new_session", "p-1", "")]


def test_drawer_tree_click_switches_session_and_project(monkeypatch: Any) -> None:
    import asyncio

    class Host(App[None]):
        def get_css_variables(self) -> dict[str, str]:
            variables = super().get_css_variables()
            variables.update(
                {
                    "theme-bg": "#1a1b2e",
                    "theme-user": "#58a6ff",
                    "theme-fg": "#c0caf5",
                    "theme-top": "#1a1b2e",
                    "theme-muted": "#565f89",
                    "theme-bar": "#1f2335",
                }
            )
            return variables

        def on_mount(self) -> None:
            self.push_screen(ProjectDrawer(current_project_id="p-1", current_thread_id="t-a1"))

    async def run() -> None:
        app = Host()
        with monkeypatch.context() as patch:
            patch.setattr("synapse.projects.catalog.ProjectCatalog", _FakeCatalog)
            patch.setattr(
                "synapse.config.Settings",
                type(
                    "FakeSettings",
                    (),
                    {
                        "__init__": lambda self, *a, **k: None,
                        "resolved_catalog_path": lambda self: ":memory:",
                    },
                ),
            )
            async with app.run_test(size=(100, 20)) as pilot:
                await pilot.pause()
                drawer = app.screen
                assert isinstance(drawer, ProjectDrawer)
                results: list[Any] = []
                drawer.dismiss = lambda value: results.append(value)  # type: ignore[method-assign]

                session_node = drawer._tree_nodes["session:p-1:t-a2"]
                drawer.on_tree_node_selected(Tree.NodeSelected(session_node))
                assert results == [("switch", "p-1", "t-a2")]

                results.clear()
                project_node = drawer._tree_nodes["project:p-2"]
                drawer.on_tree_node_selected(Tree.NodeSelected(project_node))
                assert results == [("switch_project", "p-2", "")]

                tree = drawer.query_one("#drawer-tree")
                tree.move_cursor(project_node)
                tree.action_toggle_node()
                assert project_node.is_expanded is True
                tree.action_toggle_node()
                assert project_node.is_expanded is False
                await pilot.press("escape")

    asyncio.run(run())


def test_drawer_tree_scrolls_large_session_lists(monkeypatch: Any) -> None:
    import asyncio

    catalog = _FakeCatalog()
    catalog.sessions["p-1"].extend(
        _FakeSession(f"t-{i}", f"session {i}", "2026-01-01T10:00:00")
        for i in range(40)
    )

    class Host(App[None]):
        def get_css_variables(self) -> dict[str, str]:
            variables = super().get_css_variables()
            variables.update(
                {
                    "theme-bg": "#1a1b2e",
                    "theme-user": "#58a6ff",
                    "theme-fg": "#c0caf5",
                    "theme-top": "#1a1b2e",
                    "theme-muted": "#565f89",
                    "theme-bar": "#1f2335",
                }
            )
            return variables

        def on_mount(self) -> None:
            self.push_screen(ProjectDrawer(current_project_id="p-1", current_thread_id="t-a1"))

    async def run() -> None:
        app = Host()
        with monkeypatch.context() as patch:
            patch.setattr("synapse.projects.catalog.ProjectCatalog", lambda *a, **k: catalog)
            patch.setattr(
                "synapse.config.Settings",
                type(
                    "FakeSettings",
                    (),
                    {
                        "__init__": lambda self, *a, **k: None,
                        "resolved_catalog_path": lambda self: ":memory:",
                    },
                ),
            )
            async with app.run_test(size=(100, 8)) as pilot:
                await pilot.pause()
                tree = app.screen.query_one("#drawer-tree")
                assert tree.max_scroll_y > 0
                tree.scroll_end(animate=False, immediate=True)
                assert tree.scroll_y == tree.max_scroll_y
                await pilot.press("escape")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Render smoke test: ProjectDrawer must mount and build an interactive tree.
# ---------------------------------------------------------------------------


def test_drawer_mounts_and_paints(monkeypatch: Any) -> None:
    import asyncio

    class Host(App[None]):
        def get_css_variables(self) -> dict[str, str]:
            variables = super().get_css_variables()
            variables.update(
                {
                    "theme-bg": "#1a1b2e",
                    "theme-user": "#58a6ff",
                    "theme-fg": "#c0caf5",
                    "theme-top": "#1a1b2e",
                    "theme-muted": "#565f89",
                    "theme-dim": "#9aa0a6",
                    "theme-bar": "#1f2335",
                }
            )
            return variables

        def on_mount(self) -> None:
            self.push_screen(
                ProjectDrawer(
                    current_project_id="p-1",
                    current_thread_id="t-a1",
                )
            )

    async def run() -> None:
        app = Host()
        with monkeypatch.context() as patch:
            patch.setattr("synapse.projects.catalog.ProjectCatalog", _FakeCatalog)
            patch.setattr(
                "synapse.config.Settings",
                type(
                    "FakeSettings",
                    (),
                    {
                        "__init__": lambda self, *a, **k: None,
                        "resolved_catalog_path": lambda self: ":memory:",
                    },
                ),
            )
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                assert isinstance(app.screen, ProjectDrawer)
                tree = app.screen.query_one("#drawer-tree")
                assert tree.root.children
                await pilot.press("escape")

    asyncio.run(run())
