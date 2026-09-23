"""The daemon's per-project workflow assembly.

This is the piece that decides whether workflows exist at all for a project, and it degrades
to "unavailable" on any failure — which is why it needs a test of its own: a silent
degradation looked exactly like a working feature until a real daemon ran it.

The bug this guards against: ``resolved_sessions_path()`` is a *database file*, so treating
it as a directory put the workflow database inside a file path and every project silently
lost its workflow support.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from synapse.runtime.daemon.application import _build_workflow_service


class _Settings:
    """The slice of ``Settings`` this assembly uses, with its real path contract."""

    def __init__(self, workspace: Path, *, sessions_path: Path | None = None) -> None:
        self.workspace = str(workspace)
        self.sessions_path = None if sessions_path is None else str(sessions_path)
        self.checkpoint_path = workspace / ".synapse" / "checkpoints.sqlite"
        self.custom_agents_dirs: list[str] = []
        self.disable_builtin_subagents: list[str] = []

    def resolved_sessions_path(self) -> Path:
        """A database *file*, mirroring ``Settings.resolved_sessions_path``."""
        if self.sessions_path is not None:
            return Path(self.sessions_path).expanduser().resolve()
        return self.checkpoint_path.parent / "sessions.sqlite"


def settings_for(workspace: Path, *, sessions_path: Path | None = None) -> _Settings:
    return _Settings(workspace, sessions_path=sessions_path)


def descriptor_for(project_id: str, workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(project_id=project_id, workspace=str(workspace))


def test_workflow_support_is_assembled_for_a_real_project(tmp_path: Path) -> None:
    service = _build_workflow_service(
        descriptor=descriptor_for("p1", tmp_path),
        project_settings=settings_for(tmp_path),
    )
    assert service is not None, "the daemon must not silently lose workflow support"
    try:
        store_path = service.resources.store.path
        # A sibling of the session database, never a path *inside* it.
        assert store_path.parent == tmp_path / ".synapse" / "workflows"
        assert store_path.name == "p1.sqlite"
        assert store_path.is_file()
        # The role registry is resolved from the same merge the task compiler uses.
        assert "reviewer" in service.resources.roles
    finally:
        service.close_store()


def test_an_explicit_sessions_file_is_respected(tmp_path: Path) -> None:
    """A configured sessions path moves the workflow database with it."""
    sessions = tmp_path / "state" / "sessions.sqlite"
    sessions.parent.mkdir(parents=True, exist_ok=True)
    service = _build_workflow_service(
        descriptor=descriptor_for("p2", tmp_path),
        project_settings=settings_for(tmp_path, sessions_path=sessions),
    )
    assert service is not None
    try:
        assert service.resources.store.path == tmp_path / "state" / "workflows" / "p2.sqlite"
    finally:
        service.close_store()


def test_an_unusable_state_directory_degrades_instead_of_raising(tmp_path: Path) -> None:
    """A project whose state directory cannot be created keeps ordinary chat working."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    settings = settings_for(tmp_path, sessions_path=blocked / "sessions.sqlite")
    assert (
        _build_workflow_service(
            descriptor=descriptor_for("p3", tmp_path), project_settings=settings
        )
        is None
    )
