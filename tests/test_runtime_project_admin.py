"""``runtime.project.register`` / ``runtime.fs.list``: the console "add project" pair.

``runtime.fs.list`` lets the browser walk the *host* filesystem (a browser cannot
resolve a host path itself) and ``runtime.project.register`` upserts the chosen
path into the user-layer catalog.  Both are catalog-scoped (no per-request
project position), so the ACL gate is a project-wide grant of the method's
capability; the filesystem access itself must stay bounded and read-only.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from synapse.runtime.service import (
    FS_LIST,
    PROJECT_REGISTER,
    AclAuthorizer,
    AclGrant,
    DaemonAuthorizer,
    DirectoryListing,
    InvalidRequestError,
    ListDirectoriesQuery,
    LocalAgentRuntimeService,
    PermissionDeniedError,
    Principal,
    ProjectListItem,
    RegisterProjectCommand,
    bind_access,
)
from synapse.runtime.service.routing import CatalogProjectRegistrar
from synapse.runtime.transport import protocol


@dataclass(frozen=True, slots=True)
class _Row:
    """A catalog-like row: the duck-typed shape the registrar reads."""

    project_id: str
    workspace_path: str
    name: str
    git_branch: str | None = "main"


class _FakeCatalog:
    def __init__(self) -> None:
        self.rows: dict[str, _Row] = {}
        self.calls = 0

    def register_project(self, workspace: str, **_kwargs: object) -> _Row:
        self.calls += 1
        existing = self.rows.get(workspace)
        if existing is not None:
            return existing
        row = _Row(
            project_id=f"id-{len(self.rows)}",
            workspace_path=workspace,
            name=Path(workspace).name,
        )
        self.rows[workspace] = row
        return row


def _no_manager(_project_id: str) -> None:
    raise AssertionError("this method must not resolve a manager")


def _registrar_service(catalog: _FakeCatalog) -> LocalAgentRuntimeService:
    return LocalAgentRuntimeService(
        _no_manager,  # type: ignore[arg-type]
        project_registrar=CatalogProjectRegistrar(catalog),
    )


def _plain_service() -> LocalAgentRuntimeService:
    return LocalAgentRuntimeService(_no_manager)  # type: ignore[arg-type]


def test_register_project_upserts_and_is_idempotent(tmp_path: Path) -> None:
    catalog = _FakeCatalog()
    service = _registrar_service(catalog)
    workspace = tmp_path / "proj"
    workspace.mkdir()
    command = RegisterProjectCommand(workspace_path=str(workspace))
    first = asyncio.run(service.register_project(command))
    second = asyncio.run(service.register_project(command))
    assert isinstance(first, ProjectListItem)
    assert first.project_id == second.project_id
    assert first.workspace_path == str(workspace.resolve())
    assert catalog.calls == 2


def test_register_project_rejects_missing_directory(tmp_path: Path) -> None:
    service = _registrar_service(_FakeCatalog())
    command = RegisterProjectCommand(workspace_path=str(tmp_path / "nope"))
    with pytest.raises(InvalidRequestError):
        asyncio.run(service.register_project(command))


def test_register_project_unavailable_without_registrar(tmp_path: Path) -> None:
    service = _plain_service()
    command = RegisterProjectCommand(workspace_path=str(tmp_path))
    with pytest.raises(InvalidRequestError):
        asyncio.run(service.register_project(command))


def test_list_directories_lists_only_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    service = _plain_service()
    listing = asyncio.run(service.list_directories(ListDirectoriesQuery(path=str(tmp_path))))
    assert isinstance(listing, DirectoryListing)
    assert listing.path == str(tmp_path.resolve())
    assert {entry.name for entry in listing.entries} == {"a", "b"}
    assert listing.truncated is False
    # ``roots`` are the platform's jump targets: drives on Windows, ``/`` on POSIX.
    assert isinstance(listing.roots, tuple)
    if os.name == "nt":
        assert listing.roots, "Windows must expose its drives"
    else:
        assert "/" in listing.roots


def test_list_directories_truncates_at_limit(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    service = _plain_service()
    listing = asyncio.run(
        service.list_directories(ListDirectoriesQuery(path=str(tmp_path), limit=2))
    )
    assert len(listing.entries) == 2
    assert listing.truncated is True


def test_list_directories_rejects_missing_path(tmp_path: Path) -> None:
    service = _plain_service()
    with pytest.raises(InvalidRequestError):
        asyncio.run(service.list_directories(ListDirectoriesQuery(path=str(tmp_path / "nope"))))


def test_fs_list_decoder_bounds_limit_and_path() -> None:
    dto = protocol.decode_params("runtime.fs.list", {})
    assert isinstance(dto, ListDirectoriesQuery)
    assert dto.path is None
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.fs.list", {"limit": 0})
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.fs.list", {"path": ""})
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.fs.list", {"path": 5})


def test_register_decoder_requires_workspace_path() -> None:
    dto = protocol.decode_params("runtime.project.register", {"workspace_path": "/tmp"})
    assert isinstance(dto, RegisterProjectCommand)
    assert dto.workspace_path == "/tmp"
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.project.register", {})
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.project.register", {"workspace_path": ""})


def test_register_requires_project_wide_grant(tmp_path: Path) -> None:
    delegate = _registrar_service(_FakeCatalog())
    principal = Principal(subject="user")
    workspace = tmp_path / "p"
    workspace.mkdir()
    command = RegisterProjectCommand(workspace_path=str(workspace))

    denied = bind_access(delegate, principal, AclAuthorizer([]))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(denied.register_project(command))

    grant = AclGrant(
        subject="user",
        project_id="p1",
        capabilities=frozenset({PROJECT_REGISTER}),
    )
    allowed = bind_access(delegate, principal, AclAuthorizer([grant]))
    assert isinstance(asyncio.run(allowed.register_project(command)), ProjectListItem)


def test_fs_list_requires_project_wide_grant(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    delegate = _plain_service()
    principal = Principal(subject="user")
    query = ListDirectoriesQuery(path=str(tmp_path))

    denied = bind_access(delegate, principal, AclAuthorizer([]))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(denied.list_directories(query))

    grant = AclGrant(subject="user", project_id="p1", capabilities=frozenset({FS_LIST}))
    allowed = bind_access(delegate, principal, AclAuthorizer([grant]))
    listing = asyncio.run(allowed.list_directories(query))
    assert {entry.name for entry in listing.entries} == {"sub"}


def test_daemon_authorizer_allows_both_methods(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    delegate = _registrar_service(_FakeCatalog())
    service = bind_access(delegate, Principal(subject="runtime-daemon"), DaemonAuthorizer())
    listing = asyncio.run(service.list_directories(ListDirectoriesQuery(path=str(tmp_path))))
    assert {entry.name for entry in listing.entries} == {"sub"}
    workspace = tmp_path / "sub"
    registered = asyncio.run(
        service.register_project(RegisterProjectCommand(workspace_path=str(workspace)))
    )
    assert registered.workspace_path == str(workspace.resolve())
