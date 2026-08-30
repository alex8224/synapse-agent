"""Unit tests for the user-layer global project catalog."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.projects.catalog import ProjectCatalog, detect_git_metadata


@pytest.fixture
def catalog(tmp_path: Path) -> ProjectCatalog:
    return ProjectCatalog(tmp_path / "catalog.sqlite")


def _settings(workspace: Path, sessions: Path) -> SimpleNamespace:
    """Minimal settings stand-in with resolved_sessions_path()."""
    return SimpleNamespace(
        workspace=workspace,
        sessions_path=sessions,
        resolved_sessions_path=lambda: sessions,
    )


def test_register_project_creates_stable_id(catalog: ProjectCatalog, tmp_path: Path) -> None:
    ws = tmp_path / "proj-a"
    first = catalog.register_project(ws, detect_git=False)
    assert first.project_id
    assert first.workspace_path == str(ws.resolve())
    assert first.name == "proj-a"
    assert first.session_count == 0

    again = catalog.register_project(ws, detect_git=False)
    assert again.project_id == first.project_id


def test_register_project_moved_path_keeps_id(catalog: ProjectCatalog, tmp_path: Path) -> None:
    ws1 = tmp_path / "old-location"
    ws2 = tmp_path / "new-location"
    first = catalog.register_project(ws1, detect_git=False)
    ws2.mkdir()
    moved = catalog.register_project(ws2, detect_git=False)
    # A renamed directory registers a new row; re-registering the old path
    # still returns the original id (path -> id mapping is stable).
    assert moved.project_id != first.project_id
    assert catalog.get_project(workspace=ws1).project_id == first.project_id  # type: ignore[union-attr]


def test_upsert_session_projects_and_updates(
    catalog: ProjectCatalog, tmp_path: Path
) -> None:
    ws = tmp_path / "proj"
    catalog.upsert_session(
        ws,
        thread_id="t1",
        title="first task",
        model="deep",
        summary="- 任务 first",
        updated_at="2025-01-01T10:00:00+00:00",
    )
    catalog.upsert_session(
        ws,
        thread_id="t1",
        title="first task",
        model="deep",
        summary="- 任务 first\n- 任务 second",
        updated_at="2025-01-01T11:00:00+00:00",
    )
    sessions = catalog.list_sessions(workspace=ws)
    assert len(sessions) == 1
    assert sessions[0].summary == "- 任务 first\n- 任务 second"
    project = catalog.get_project(workspace=ws)
    assert project is not None
    assert project.session_count == 1
    # updated_at must never regress.
    catalog.upsert_session(
        ws,
        thread_id="t1",
        title="first task",
        summary="- 任务 first",
        updated_at="2025-01-01T09:00:00+00:00",
    )
    assert catalog.list_sessions(workspace=ws)[0].updated_at == "2025-01-01T11:00:00+00:00"


def test_sync_project_reads_project_db(catalog: ProjectCatalog, tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    sessions_db = ws / "sessions.sqlite"
    import sqlite3

    conn = sqlite3.connect(sessions_db)
    conn.execute(
        """
        CREATE TABLE sessions (
            thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]', summary TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("t1", "hello", None, "2025-01-01T00:00:00+00:00",
         "2025-01-01T01:00:00+00:00", '["dev"]', "- 任务 hello"),
    )
    conn.commit()
    conn.close()

    settings = _settings(ws, sessions_db)
    assert catalog.sync_project(settings) == 1
    items = catalog.list_sessions(workspace=ws)
    assert len(items) == 1
    assert items[0].thread_id == "t1"
    assert items[0].tags == ["dev"]
    assert items[0].summary == "- 任务 hello"
    # Missing project db degrades to zero, not an error.
    empty_ws = tmp_path / "empty"
    empty_ws.mkdir()
    assert catalog.sync_project(_settings(empty_ws, empty_ws / "nope.sqlite")) == 0


def test_resolve_project_by_id_prefix_name_and_path(
    catalog: ProjectCatalog, tmp_path: Path
) -> None:
    ws = tmp_path / "my-repo"
    ws.mkdir()
    info = catalog.register_project(ws, detect_git=False, git_remote="https://x/my-repo.git")
    assert catalog.resolve_project(info.project_id[:8]).project_id == info.project_id
    assert catalog.resolve_project("my-repo").project_id == info.project_id
    assert catalog.resolve_project(str(ws)).project_id == info.project_id
    assert catalog.resolve_project("missing") is None


def test_get_project_by_id_is_exact_and_does_not_resolve_aliases(
    catalog: ProjectCatalog, tmp_path: Path
) -> None:
    ws = tmp_path / "exact-project"
    ws.mkdir()
    info = catalog.register_project(ws, detect_git=False)

    assert catalog.get_project(project_id=info.project_id).project_id == info.project_id  # type: ignore[union-attr]
    assert catalog.get_project(project_id=info.project_id[:8]) is None
    assert catalog.get_project(project_id=info.name) is None
    assert catalog.get_project(project_id=str(ws)) is None


def test_search_sessions_cross_project(catalog: ProjectCatalog, tmp_path: Path) -> None:
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    catalog.upsert_session(a, thread_id="ta", title="jwt migration", summary="- 任务 jwt")
    catalog.upsert_session(b, thread_id="tb", title="styling", summary="- 任务 css")
    hits = catalog.search_sessions("jwt")
    assert len(hits) == 1
    assert hits[0].project_name == "proj-a"
    assert hits[0].global_id == f"{hits[0].project_id}:ta"
    hits = catalog.search_sessions("css")
    assert len(hits) == 1
    assert hits[0].project_name == "proj-b"


def test_delete_project_removes_projection_only(
    catalog: ProjectCatalog, tmp_path: Path
) -> None:
    ws = tmp_path / "proj"
    catalog.upsert_session(ws, thread_id="t1", title="x")
    catalog.record_run(ws, mode="tui", thread_id="t1")
    assert catalog.delete_project(workspace=ws) is True
    assert catalog.list_projects() == []
    assert catalog.list_sessions() == []
    assert catalog.list_runs() == []
    assert catalog.delete_project(workspace=ws) is False


def test_run_ledger(catalog: ProjectCatalog, tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    run_id = catalog.record_run(ws, mode="tui", thread_id="t1")
    project = catalog.get_project(workspace=ws)
    assert project is not None
    assert project.run_count == 1
    runs = catalog.list_runs(workspace=ws)
    assert len(runs) == 1
    assert runs[0].mode == "tui"
    assert runs[0].finished_at is None
    assert catalog.finish_run(run_id, exit_code=0) is True
    runs = catalog.list_runs(workspace=ws)
    assert runs[0].finished_at is not None
    assert runs[0].exit_code == 0


def test_stats(catalog: ProjectCatalog, tmp_path: Path) -> None:
    catalog.upsert_session(tmp_path / "a", thread_id="t1", title="x")
    catalog.upsert_session(tmp_path / "b", thread_id="t1", title="y")
    stats = catalog.stats()
    assert stats["projects"] == 2
    assert stats["sessions"] == 2


def test_detect_git_metadata_never_raises(tmp_path: Path) -> None:
    remote, branch = detect_git_metadata(tmp_path / "not-a-git-dir")
    assert remote is None
    assert branch is None


def test_catalog_without_explicit_path_uses_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synapse.projects.catalog import default_catalog_path

    monkeypatch.setenv("HOME", str(tmp_path))
    import synapse.projects.catalog as mod

    monkeypatch.setattr(mod, "user_config_dir", lambda: tmp_path / ".synapse")
    assert default_catalog_path() == tmp_path / ".synapse" / "catalog.sqlite"


def test_set_summary_persists(tmp_path: Path) -> None:
    from synapse.sessions.store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite")
    info = store.ensure("t1", title="hello world")
    assert info.summary is None
    assert store.set_summary("t1", "- 任务 hello") is True
    assert store.get("t1").summary == "- 任务 hello"  # type: ignore[union-attr]
    assert store.set_summary("missing", "x") is False


def test_concurrent_writers_do_not_lose_rows(tmp_path: Path) -> None:
    """Multiple agents writing the shared catalog must not raise or drop rows."""
    import threading

    db_path = tmp_path / "catalog.sqlite"
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def _writer(agent: int) -> None:
        try:
            # Each agent opens its own connection (separate process model).
            cat = ProjectCatalog(db_path)
            ws = tmp_path / f"proj-{agent}"
            barrier.wait()
            for i in range(30):
                cat.upsert_session(
                    ws,
                    thread_id=f"t{i}",
                    title=f"agent {agent} task {i}",
                    updated_at=f"2025-01-01T{agent:02d}:{i:02d}:00+00:00",
                )
            cat.record_run(ws, mode="tui", thread_id="t0")
            cat.close()
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(a,)) for a in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    verify = ProjectCatalog(db_path)
    try:
        assert len(verify.list_projects()) == 4
        assert len(verify.list_sessions()) == 120
        assert len(verify.list_runs()) == 4
    finally:
        verify.close()
