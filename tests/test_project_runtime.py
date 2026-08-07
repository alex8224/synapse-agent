"""P6 ProjectRuntime / cross-project resource isolation contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.projects.identity import (
    ensure_project_identity,
    project_file_for,
    read_project_identity,
)
from synapse.runtime.projects.runtime import (
    ProjectRegistry,
    ProjectRuntime,
    config_digest,
    mcp_pool_key,
)
from synapse.runtime.sessions import (
    SessionResolutionError,
    parse_global_id,
    resolve_session_ref,
)


class _FakeCatalog:
    def __init__(self, projects: list[Any]) -> None:
        self.projects = projects

    def list_projects(self, limit: int) -> list[Any]:
        del limit
        return self.projects

    def list_sessions(self, project_id: str, limit: int) -> list[Any]:
        del limit
        return [
            SimpleNamespace(thread_id="t1"),
            SimpleNamespace(thread_id="t2"),
        ]


def _proj(project_id: str, name: str) -> Any:
    return SimpleNamespace(project_id=project_id, name=name)


# ---------------------------------------------------------------------------
# SessionRef parsing / resolution
# ---------------------------------------------------------------------------


def test_parse_global_id_splits_on_last_colon() -> None:
    ref = parse_global_id("proj-1:thread:with:colons")
    assert ref.project_id == "proj-1"
    assert ref.thread_id == "thread:with:colons"
    assert ref.global_id == "proj-1:thread:with:colons"


def test_parse_global_id_rejects_invalid() -> None:
    for bad in ("", "  ", "no-colon", ":thread", "proj:"):
        with pytest.raises(SessionResolutionError):
            parse_global_id(bad)


def test_resolve_session_ref_exact() -> None:
    catalog = _FakeCatalog([_proj("abcdef123456", "alpha")])
    ref = resolve_session_ref("abcdef123456:t1", catalog=catalog)
    assert ref.project_id == "abcdef123456"
    assert ref.thread_id == "t1"


def test_resolve_session_ref_unique_prefix() -> None:
    catalog = _FakeCatalog(
        [_proj("abcdef123456", "alpha"), _proj("abcdef999999", "beta")]
    )
    ref = resolve_session_ref("abcdef12:t2", catalog=catalog)
    assert ref.project_id == "abcdef123456"
    assert ref.thread_id == "t2"


def test_resolve_session_ref_ambiguous_project_rejected() -> None:
    catalog = _FakeCatalog(
        [_proj("abcd1111", "alpha"), _proj("abcd2222", "beta")]
    )
    with pytest.raises(SessionResolutionError):
        resolve_session_ref("abcd:t1", catalog=catalog)


def test_resolve_session_ref_unknown_project() -> None:
    catalog = _FakeCatalog([_proj("abcd1111", "alpha")])
    with pytest.raises(SessionResolutionError):
        resolve_session_ref("zzzz:t1", catalog=catalog)


def test_resolve_session_ref_verify_missing_thread() -> None:
    catalog = _FakeCatalog([_proj("abcd1111", "alpha")])
    with pytest.raises(SessionResolutionError):
        resolve_session_ref("abcd1111:missing", catalog=catalog, verify=True)


def test_session_ref_shorthand_project_only() -> None:
    catalog = _FakeCatalog([_proj("abcd1111", "alpha")])
    ref = resolve_session_ref("abcd", catalog=catalog)
    assert ref.project_id == "abcd1111"
    assert ref.thread_id == ""


# ---------------------------------------------------------------------------
# project.json stable identity
# ---------------------------------------------------------------------------


def test_ensure_project_identity_roundtrip(tmp_path: Path) -> None:
    pid = ensure_project_identity(tmp_path)
    assert len(pid) == 36  # uuid4
    data = read_project_identity(tmp_path)
    assert data is not None
    assert data["project_id"] == pid
    # Second call is idempotent.
    assert ensure_project_identity(tmp_path) == pid


def test_ensure_project_identity_prefers_existing(tmp_path: Path) -> None:
    file = project_file_for(tmp_path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"schema_version": 1, "project_id": "stable-id"}))
    assert ensure_project_identity(tmp_path, catalog_project_id="other") == "stable-id"


def test_ensure_project_identity_uses_catalog_id(tmp_path: Path) -> None:
    assert (
        ensure_project_identity(tmp_path, catalog_project_id="catalog-id") == "catalog-id"
    )
    assert read_project_identity(tmp_path)["project_id"] == "catalog-id"


# ---------------------------------------------------------------------------
# ProjectRuntime lazy lifecycle
# ---------------------------------------------------------------------------


def test_project_runtime_activate_opens_data_sources(tmp_path: Path, monkeypatch: Any) -> None:
    settings = SimpleNamespace(resolved_sessions_path=lambda: str(tmp_path / "s.sqlite"))
    runtime = ProjectRuntime(
        project_id="p1",
        workspace=tmp_path,
        settings=settings,
    )
    assert runtime.collectable() is True
    runtime.activate()
    assert runtime.session_store is not None
    assert runtime.transcript_projection is not None
    assert runtime._activated is True  # noqa: SLF001
    runtime.close()


def test_project_runtime_has_running_sessions_blocks_collect(tmp_path: Path) -> None:
    settings = SimpleNamespace(resolved_sessions_path=lambda: str(tmp_path / "s.sqlite"))
    runtime = ProjectRuntime(project_id="p1", workspace=tmp_path, settings=settings)
    runtime.activate()

    class _Running:
        def snapshot(self) -> Any:
            return SimpleNamespace(status=SimpleNamespace(value="running"))

    runtime.sessions["t1"] = _Running()
    assert runtime.has_running_sessions() is True
    assert runtime.collectable() is False
    runtime.close()


def test_project_registry_deduplicates(tmp_path: Path) -> None:
    registry = ProjectRegistry()
    settings = SimpleNamespace(resolved_sessions_path=lambda: str(tmp_path / "s.sqlite"))
    a = ProjectRuntime(project_id="p1", workspace=tmp_path, settings=settings)
    b = ProjectRuntime(project_id="p1", workspace=tmp_path, settings=settings)
    assert registry.register(a) is a
    assert registry.register(b) is a
    assert registry.get("p1") is a
    registry.drop("p1")
    assert registry.get("p1") is None


# ---------------------------------------------------------------------------
# MCP pool key helpers
# ---------------------------------------------------------------------------


def test_mcp_pool_key_and_digest() -> None:
    key = mcp_pool_key("proj-1", "digest-abc")
    assert key == "proj-1:digest-abc"
    assert config_digest({"a": 1}) == config_digest({"a": 1})
    assert config_digest({"a": 1}) != config_digest({"a": 2})


# ---------------------------------------------------------------------------
# P6-04: project .env as a private mapping (never mutates os.environ)
# ---------------------------------------------------------------------------


def test_project_env_mapping_reads_workspace_env(tmp_path: Path, monkeypatch: Any) -> None:
    from synapse.settings.schema import project_env_mapping

    monkeypatch.setattr(
        "synapse.settings.schema.find_dotenv",
        lambda root: (tmp_path / ".env") if (tmp_path / ".env").exists() else None,
    )
    monkeypatch.setattr("synapse.settings.schema.user_config_dir", lambda: tmp_path / "home")
    monkeypatch.setattr("synapse.settings.config_paths.user_config_dir", lambda: tmp_path / "home")
    (tmp_path / ".env").write_text("PRIVATE_A=1\nPRIVATE_B=two\n")
    mapping = project_env_mapping(tmp_path)
    assert mapping == {"PRIVATE_A": "1", "PRIVATE_B": "two"}


def test_project_env_mapping_absent_is_empty(tmp_path: Path, monkeypatch: Any) -> None:
    from synapse.settings.schema import project_env_mapping

    monkeypatch.setattr("synapse.settings.schema.find_dotenv", lambda root: None)
    assert project_env_mapping(tmp_path) == {}


# ---------------------------------------------------------------------------
# P6-05: goal tools / middleware accept an injected project service
# ---------------------------------------------------------------------------


def test_build_goal_tools_uses_injected_service() -> None:
    from synapse.goals.tools import build_goal_tools

    calls: list[str] = []

    class _Service:
        def get(self, thread_id: str) -> Any:
            calls.append(("get", thread_id))
            return None

    tools = build_goal_tools(service=_Service())
    get_tool = next(t for t in tools if t.name == "get_goal")
    runtime = SimpleNamespace(
        config={"configurable": {"thread_id": "tid-1"}}
    )
    text = get_tool.func(runtime)
    assert "no goal" in text
    assert calls == [("get", "tid-1")]


def test_build_goal_middleware_uses_injected_service() -> None:
    from synapse.goals.middleware import build_goal_middleware

    class _Service:
        def on_model_call_begin(self, thread_id: str) -> None:
            del thread_id

    middleware = build_goal_middleware(enabled=True, service=_Service())
    assert middleware is not None
    # Disabled path stays a no-op regardless of service.
    noop = build_goal_middleware(enabled=False, service=_Service())
    assert noop is not None


# ---------------------------------------------------------------------------
# P6-10: two workspaces stay isolated (config, stores, identity)
# ---------------------------------------------------------------------------


def test_two_workspaces_isolated_identity_and_stores(tmp_path: Path) -> None:
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()

    pid_a = ensure_project_identity(ws_a)
    pid_b = ensure_project_identity(ws_b)
    assert pid_a != pid_b
    assert (ws_a / ".synapse" / "project.json").exists()
    assert (ws_b / ".synapse" / "project.json").exists()

    from synapse.settings.config_paths import project_config_dir

    assert project_config_dir(ws_a) != project_config_dir(ws_b)


def test_project_runtime_isolated_sessions_map(tmp_path: Path) -> None:
    settings_a = SimpleNamespace(resolved_sessions_path=lambda: str(tmp_path / "a" / "s.sqlite"))
    settings_b = SimpleNamespace(resolved_sessions_path=lambda: str(tmp_path / "b" / "s.sqlite"))
    pa = ProjectRuntime(project_id="pa", workspace=tmp_path / "a", settings=settings_a)
    pb = ProjectRuntime(project_id="pb", workspace=tmp_path / "b", settings=settings_b)
    pa.sessions["t1"] = object()
    assert "t1" not in pb.sessions
    assert pa.ref.project_id == "pa"
    assert pb.ref.project_id == "pb"
    pa.close()
    pb.close()


# ---------------------------------------------------------------------------
# P7-01: global bootstrap loads user-layer config without touching cwd
# ---------------------------------------------------------------------------


def test_load_global_settings_skips_project_env(tmp_path: Path, monkeypatch: Any) -> None:
    from synapse.settings.schema import load_global_settings

    monkeypatch.setattr(
        "synapse.settings.config_paths.user_config_dir",
        lambda: tmp_path / "home" / ".synapse",
    )
    monkeypatch.setattr(
        "synapse.settings.config_paths.executable_config_dirs",
        lambda: [],
    )
    monkeypatch.setattr(
        "synapse.settings.schema.bootstrap_project_env",
        lambda root: (_ for _ in ()).throw(AssertionError("must not load env")),
    )
    settings = load_global_settings()
    assert settings is not None
    # No project .synapse directory is created in the cwd.
    assert not (tmp_path / ".synapse").exists()


# ---------------------------------------------------------------------------
# P7-08: catalog sync reconciles deleted / stale session projections
# ---------------------------------------------------------------------------


def test_catalog_sync_removes_stale_projections(tmp_path: Path, monkeypatch: Any) -> None:
    import sqlite3

    from synapse.projects.catalog import ProjectCatalog
    from synapse.settings.schema import load_project_settings

    ws = tmp_path / "ws"
    settings = load_project_settings(
        ws,
        theme="cursor-dark",
        sessions_path=str(ws / ".synapse" / "sessions.sqlite"),
    )
    assert settings.resolved_sessions_path() == (ws / ".synapse" / "sessions.sqlite").resolve()
    (ws / ".synapse").mkdir(parents=True, exist_ok=True)
    db_path = ws / ".synapse" / "sessions.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions (thread_id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "summary TEXT, updated_at TEXT, created_at TEXT, tags_json TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('keep', 'keep me', 'm', NULL, '2026-01-01', "
        "'2026-01-01', '[]')"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('gone', 'gone soon', 'm', NULL, '2026-01-01', "
        "'2026-01-01', '[]')"
    )
    conn.commit()
    conn.close()

    catalog = ProjectCatalog(tmp_path / "catalog.sqlite")
    assert catalog.sync_project(settings) == 2
    assert {s.thread_id for s in catalog.list_sessions(workspace=settings.workspace)} == {
        "keep",
        "gone",
    }

    # Delete one source row and re-sync: the stale projection must disappear.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM sessions WHERE thread_id = 'gone'")
    conn.commit()
    conn.close()
    assert catalog.sync_project(settings) == 1
    assert {s.thread_id for s in catalog.list_sessions(workspace=settings.workspace)} == {
        "keep"
    }
    catalog.close()


def test_catalog_sync_missing_source_clears_projections(tmp_path: Path) -> None:
    from synapse.projects.catalog import ProjectCatalog
    from synapse.settings.schema import load_project_settings

    ws = tmp_path / "ws"
    settings = load_project_settings(
        ws,
        theme="cursor-dark",
        sessions_path=str(ws / ".synapse" / "sessions.sqlite"),
    )
    catalog = ProjectCatalog(tmp_path / "catalog.sqlite")
    # No sessions.sqlite on disk: sync projects nothing and removes stale rows.
    assert catalog.sync_project(settings) == 0
    catalog.close()


def test_catalog_resolve_and_remove_session_projection(tmp_path: Path) -> None:
    import sqlite3

    from synapse.projects.catalog import ProjectCatalog
    from synapse.settings.schema import load_project_settings

    ws = tmp_path / "ws"
    settings = load_project_settings(
        ws,
        theme="cursor-dark",
        sessions_path=str(ws / ".synapse" / "sessions.sqlite"),
    )
    (ws / ".synapse").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ws / ".synapse" / "sessions.sqlite"))
    conn.execute(
        "CREATE TABLE sessions (thread_id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "summary TEXT, updated_at TEXT, created_at TEXT, tags_json TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('t-abc', 'title', 'm', NULL, '2026-01-01', "
        "'2026-01-01', '[]')"
    )
    conn.commit()
    conn.close()

    catalog = ProjectCatalog(tmp_path / "catalog.sqlite")
    catalog.sync_project(settings)
    project = catalog.get_project(workspace=ws)
    assert project is not None

    resolved = catalog.resolve_session(f"{project.project_id[:6]}:t-abc")
    assert resolved is not None
    assert resolved.thread_id == "t-abc"
    assert resolved.workspace_path == str(ws)

    assert (
        catalog.remove_session_projection(
            project_id=project.project_id, thread_id="t-abc"
        )
        is True
    )
    assert catalog.resolve_session(f"{project.project_id}:t-abc") is None
    catalog.close()



