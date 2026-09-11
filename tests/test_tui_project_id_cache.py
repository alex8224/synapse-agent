"""TUI project-id memoization.

``CodingAgentApp._current_project_id`` sits on the chrome render path (bottombar
model / mcp / codex-usage / key-hints plus the topbar title) and is therefore
called several times per repaint. The uncached implementation opens, bootstraps
and closes the SQLite project catalog on every call, which dominated the TUI
thread while a turn was streaming.

The memo only ever holds a catalog row, because that is immutable for a
workspace path. These tests pin that contract, the deliberate non-caching of the
``project.json`` fallback, and the key-based invalidation on a project switch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.projects.identity import read_project_identity
from synapse.ui.tui import CodingAgentApp


class _Settings:
    """Minimal settings stand-in exposing only the catalog-path surface."""

    def __init__(self, catalog_path: Path) -> None:
        self.project_catalog_path = str(catalog_path)

    def resolved_catalog_path(self) -> Path:
        return Path(self.project_catalog_path)


def _make_app(workspace: Path, catalog_path: Path) -> CodingAgentApp:
    """Build an app shell without spinning up the Textual runtime."""
    app = CodingAgentApp.__new__(CodingAgentApp)
    app.settings = _Settings(catalog_path)
    app.project_root = workspace
    app._project_catalog = None
    app._project_id_cache = None
    return app


def _count_catalog_opens(monkeypatch: pytest.MonkeyPatch) -> list[ProjectCatalog]:
    """Record every ``ProjectCatalog`` construction performed by the app."""
    import synapse.projects.catalog as catalog_module

    opened: list[ProjectCatalog] = []
    real_catalog = catalog_module.ProjectCatalog

    def factory(path: object) -> ProjectCatalog:
        instance = real_catalog(path)  # type: ignore[arg-type]
        opened.append(instance)
        return instance

    monkeypatch.setattr(catalog_module, "ProjectCatalog", factory)
    return opened


def test_current_project_id_is_memoized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = tmp_path / "proj-a"
    ws.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    info = catalog.register_project(ws, detect_git=False)
    catalog.close()

    app = _make_app(ws, catalog_path)
    opened = _count_catalog_opens(monkeypatch)

    assert app._current_project_id() == info.project_id
    for _ in range(25):
        assert app._current_project_id() == info.project_id
    # One catalog open for the whole render burst, not one per call.
    assert len(opened) == 1


def test_project_json_fallback_is_not_pinned(tmp_path: Path) -> None:
    """A pre-registration id must yield to the catalog row that lands later.

    ``run_tui`` starts the catalog worker before ``app.run()``, so the first
    frames resolve the id from ``project.json``; the worker then registers the
    workspace under a different id. Nothing invalidates the memo in between, so
    the fallback result must never have been memoized.
    """
    ws = tmp_path / "proj-a"
    ws.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    # Empty catalog: the first lookup has to mint an id from project.json.
    ProjectCatalog(str(catalog_path)).close()

    app = _make_app(ws, catalog_path)
    minted = app._current_project_id()
    assert minted
    assert (ws / ".synapse" / "project.json").exists()

    # ``register_project`` never adopts project.json, so the catalog row gets a
    # different id than the one the fallback already handed out.
    catalog = ProjectCatalog(str(catalog_path))
    registered = catalog.register_project(ws, detect_git=False)
    assert registered.project_id != minted

    try:
        # The catalog is the authority, so the next lookup must follow it and
        # rewrite the stale project.json instead of leaving two ids for one
        # workspace.
        assert app._current_project_id() == registered.project_id
        assert read_project_identity(ws)["project_id"] == registered.project_id
    finally:
        catalog.close()


def test_current_project_id_follows_project_root(tmp_path: Path) -> None:
    ws_a = tmp_path / "proj-a"
    ws_b = tmp_path / "proj-b"
    ws_a.mkdir()
    ws_b.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    project_a = catalog.register_project(ws_a, detect_git=False)
    project_b = catalog.register_project(ws_b, detect_git=False)
    catalog.close()

    app = _make_app(ws_a, catalog_path)
    assert app._current_project_id() == project_a.project_id
    # An in-process project switch moves project_root; the memo key follows.
    app.project_root = ws_b
    assert app._current_project_id() == project_b.project_id
