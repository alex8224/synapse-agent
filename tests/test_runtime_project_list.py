"""`runtime.project.list`: bounded, filtered, server-scoped project enumeration.

The gate this file protects is *visibility*: the project list is the first wire
method with no per-request project position, so the server (not the browser)
decides which registered projects a connection may see.  Three layers are
checked independently:

* the ACL layer derives an explicit visible set from the principal's grants;
* the connection scope narrows that set to one project and denies every other
  project for session methods too;
* the wire decoder never accepts a client-supplied visibility filter, and the
  trusted scope travels only over the host-private handshake header read after
  bearer authentication.

The enumeration itself must stay read-only: no manager, no agent, no session, and
no project registration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from synapse.runtime.daemon.application import _CONNECTION_PROJECT_SCOPE, RuntimeDaemon
from synapse.runtime.daemon.auth import (
    MAX_PROJECT_SCOPE_BYTES,
    PROJECT_SCOPE_HEADER,
    BearerTokenAuthenticator,
    ProjectScopeHeaderError,
    ScopedConnectionAuthenticator,
    read_project_scope_header,
)
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.service import (
    PROJECT_LIST,
    AccessControlledAgentRuntimeService,
    AclAuthorizer,
    AclGrant,
    DaemonAuthorizer,
    GetSessionQuery,
    InvalidRequestError,
    ListProjectsQuery,
    ListSessionsQuery,
    LocalAgentRuntimeService,
    PermissionDeniedError,
    Principal,
    ProjectScopeAuthorizer,
    SetProjectThinkingLevelCommand,
)
from synapse.runtime.service.routing import CatalogProjectListProvider
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import protocol

TOKEN = "daemon-token"
PROJECT_IDS = ("p0", "p1", "p2", "p3", "p4")


@dataclass(frozen=True, slots=True)
class _Row:
    """A catalog-like row: the duck-typed shape the provider reads."""

    project_id: str
    workspace_path: str
    name: str
    git_branch: str | None = "main"


class _FakeCatalog:
    def __init__(self, rows: list[_Row]) -> None:
        self.rows = rows
        self.calls = 0

    def list_projects(self, *, limit: int = 100) -> list[_Row]:
        self.calls += 1
        return self.rows[:limit]


def _rows(project_ids: tuple[str, ...] = PROJECT_IDS) -> list[_Row]:
    return [
        _Row(
            project_id=project_id,
            workspace_path=f"/w/{project_id}",
            name=project_id.upper(),
        )
        for project_id in project_ids
    ]


def _service(
    rows: list[_Row],
    *,
    manager_provider: object | None = None,
) -> tuple[LocalAgentRuntimeService, _FakeCatalog]:
    catalog = _FakeCatalog(rows)
    if manager_provider is None:

        def manager_provider(_project_id: str) -> None:  # noqa: ARG001 - never used
            raise AssertionError("project enumeration must not resolve a manager")

    service = LocalAgentRuntimeService(
        manager_provider,  # type: ignore[arg-type]
        project_list_provider=CatalogProjectListProvider(catalog),
    )
    return service, catalog


def _daemon_service(
    rows: list[_Row],
    *,
    authorizer: AclAuthorizer | DaemonAuthorizer | None = None,
    scope: str | None = None,
) -> AccessControlledAgentRuntimeService:
    daemon = RuntimeDaemon(DaemonConfig(state_dir=DaemonConfig().state_dir))
    daemon.catalog = _FakeCatalog(rows)
    daemon.router = None
    if authorizer is not None:
        daemon._authorizer_factory = lambda _principal: authorizer  # type: ignore[assignment]
    marker = _CONNECTION_PROJECT_SCOPE.set(scope)
    try:
        return daemon._make_service(Principal("runtime-daemon"))
    finally:
        _CONNECTION_PROJECT_SCOPE.reset(marker)


# --- pagination and the filter-before-pagination contract ---------------------


def test_pages_are_bounded_and_continuation_is_exact() -> None:
    service, _catalog = _service(_rows())
    first = asyncio.run(service.list_projects(ListProjectsQuery(limit=2)))
    second = asyncio.run(service.list_projects(ListProjectsQuery(limit=2, offset=2)))
    third = asyncio.run(service.list_projects(ListProjectsQuery(limit=2, offset=4)))

    assert [item.project_id for item in first.projects] == ["p0", "p1"]
    assert first.next_offset == 2
    assert first.total == 5
    assert [item.project_id for item in second.projects] == ["p2", "p3"]
    assert second.next_offset == 4
    assert [item.project_id for item in third.projects] == ["p4"]
    assert third.next_offset is None
    assert third.total == 5


def test_visibility_filter_is_applied_before_pagination() -> None:
    """A filtered page must describe the visible slice, not a slice of the catalog."""
    service, _catalog = _service(_rows())
    query = ListProjectsQuery(limit=1, visible_project_ids=("p1", "p3"))
    first = asyncio.run(service.list_projects(query))
    second = asyncio.run(service.list_projects(ListProjectsQuery(
        limit=1, offset=first.next_offset or 0, visible_project_ids=("p1", "p3")
    )))

    assert [item.project_id for item in first.projects] == ["p1"]
    assert first.total == 2
    assert first.next_offset == 1
    assert [item.project_id for item in second.projects] == ["p3"]
    assert second.next_offset is None


def test_an_unknown_visible_project_id_is_hidden_not_reported() -> None:
    service, _catalog = _service(_rows())
    page = asyncio.run(
        service.list_projects(ListProjectsQuery(visible_project_ids=("p0", "does-not-exist")))
    )
    assert [item.project_id for item in page.projects] == ["p0"]
    assert page.total == 1


def test_the_page_carries_identity_only() -> None:
    service, _catalog = _service(_rows(("p0",)))
    item = asyncio.run(service.list_projects(ListProjectsQuery())).projects[0]
    assert item.project_id == "p0"
    assert item.workspace_name == "P0"
    assert item.git_branch == "main"
    assert item.workspace_path == "/w/p0"
    # No aggregate catalog business data leaks into the wire DTO.
    assert not hasattr(item, "session_count")
    assert not hasattr(item, "last_active_at")


def test_enumeration_never_builds_a_manager_agent_or_session() -> None:
    # ``_service`` installs a manager provider that fails the test if it is used.
    service, catalog = _service(_rows())
    page = asyncio.run(service.list_projects(ListProjectsQuery()))
    assert len(page.projects) == 5
    assert catalog.calls == 1


def test_a_delegate_without_a_provider_reports_the_feature_as_unavailable() -> None:
    service = LocalAgentRuntimeService(lambda _project_id: None)
    with pytest.raises(InvalidRequestError):
        asyncio.run(service.list_projects(ListProjectsQuery()))


# --- ACL visibility -----------------------------------------------------------


def test_acl_visibility_limits_the_list_to_granted_projects() -> None:
    service, _catalog = _service(_rows())
    authorizer = AclAuthorizer(
        [
            AclGrant("alice", "p1", frozenset({PROJECT_LIST})),
            AclGrant("alice", "p3", frozenset({PROJECT_LIST})),
        ]
    )
    alice = AccessControlledAgentRuntimeService(service, Principal("alice"), authorizer)
    page = asyncio.run(alice.list_projects(ListProjectsQuery()))
    assert [item.project_id for item in page.projects] == ["p1", "p3"]


def test_a_principal_without_the_capability_is_denied_outright() -> None:
    service, _catalog = _service(_rows())
    authorizer = AclAuthorizer([AclGrant("alice", "p1", frozenset({PROJECT_LIST}))])
    bob = AccessControlledAgentRuntimeService(service, Principal("bob"), authorizer)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(bob.list_projects(ListProjectsQuery()))


def test_a_thread_scoped_grant_never_authorizes_project_enumeration() -> None:
    service, _catalog = _service(_rows())
    authorizer = AclAuthorizer(
        [AclGrant("alice", "p1", frozenset({PROJECT_LIST}), frozenset({"t1"}))]
    )
    alice = AccessControlledAgentRuntimeService(service, Principal("alice"), authorizer)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(alice.list_projects(ListProjectsQuery()))


def test_the_wrapper_can_only_narrow_a_caller_supplied_visibility_filter() -> None:
    """A narrowing filter is honoured; it can never widen the ACL set."""
    service, _catalog = _service(_rows())
    authorizer = AclAuthorizer(
        [
            AclGrant("alice", "p1", frozenset({PROJECT_LIST})),
            AclGrant("alice", "p3", frozenset({PROJECT_LIST})),
        ]
    )
    alice = AccessControlledAgentRuntimeService(service, Principal("alice"), authorizer)
    narrowed = asyncio.run(
        alice.list_projects(ListProjectsQuery(visible_project_ids=("p3", "p4")))
    )
    assert [item.project_id for item in narrowed.projects] == ["p3"]


# --- the trusted connection scope --------------------------------------------


def test_the_connection_scope_narrows_the_list_to_one_project() -> None:
    scoped = _daemon_service(_rows(), scope="p2")
    page = asyncio.run(scoped.list_projects(ListProjectsQuery()))
    assert [item.project_id for item in page.projects] == ["p2"]
    assert page.total == 1


def test_the_connection_scope_denies_every_other_project() -> None:
    scoped = _daemon_service(_rows(), scope="p2")
    with pytest.raises(PermissionDeniedError):
        asyncio.run(scoped.get_session(GetSessionQuery(SessionRef("p1", "t1"))))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(scoped.list_sessions(ListSessionsQuery("p1")))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            scoped.set_project_thinking_level(
                SetProjectThinkingLevelCommand(project_id="p1", level="low")
            )
        )


def test_without_a_scope_the_daemon_keeps_its_own_visibility() -> None:
    unscoped = _daemon_service(_rows())
    page = asyncio.run(unscoped.list_projects(ListProjectsQuery()))
    assert [item.project_id for item in page.projects] == list(PROJECT_IDS)


def test_the_scope_wrapper_never_widens_the_underlying_acl() -> None:
    """A scope over an empty ACL still denies: it is subtractive, not a grant."""
    service, _catalog = _service(_rows())
    empty = AclAuthorizer([])
    scoped = AccessControlledAgentRuntimeService(
        service, Principal("alice"), ProjectScopeAuthorizer(empty, "p1")
    )
    with pytest.raises(PermissionDeniedError):
        asyncio.run(scoped.list_projects(ListProjectsQuery()))


def test_the_scope_authorizer_rejects_a_nested_wrapper() -> None:
    with pytest.raises(TypeError):
        ProjectScopeAuthorizer(ProjectScopeAuthorizer(DaemonAuthorizer(), "p1"), "p1")  # type: ignore[arg-type]


def test_the_acl_wrapper_rejects_an_unknown_authorizer() -> None:
    service, _catalog = _service(_rows())

    class _Impostor:
        def authorize(self, *_args: object) -> None: ...

        def authorize_project(self, *_args: object) -> None: ...

        def visible_project_ids(self, *_args: object) -> frozenset[str]:
            return frozenset()

    with pytest.raises(TypeError):
        AccessControlledAgentRuntimeService(service, Principal("alice"), _Impostor())  # type: ignore[arg-type]


# --- the wire boundary --------------------------------------------------------


def test_the_wire_decoder_rejects_a_client_supplied_visibility_filter() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params(
            "runtime.project.list", {"visible_project_ids": ["p1"]}
        )


def test_the_wire_decoder_bounds_pagination() -> None:
    assert protocol.decode_params("runtime.project.list", {}).limit == 50
    assert protocol.decode_params("runtime.project.list", {"limit": 1, "offset": 0}).offset == 0
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.project.list", {"limit": 0})
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.project.list", {"limit": 101})
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.project.list", {"offset": -1})


def test_the_wire_method_is_dispatched_through_the_service_port() -> None:
    service, _catalog = _service(_rows())
    result = asyncio.run(protocol.dispatch(service, "runtime.project.list", {"limit": 1}))
    assert [item.project_id for item in result.projects] == ["p0"]


def test_negotiate_cannot_declare_a_project_scope() -> None:
    """A browser cannot self-report a (widened) scope during the handshake."""
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params(
            "runtime.protocol.negotiate",
            {"versions": ["1"], "project_scope": "p9"},
        )


# --- the trusted handshake header --------------------------------------------


def test_the_scope_header_is_read_only_after_bearer_authentication() -> None:
    authenticator = ScopedConnectionAuthenticator(
        BearerTokenAuthenticator(TOKEN), _CONNECTION_PROJECT_SCOPE
    )

    async def scenario() -> tuple[str | None, str | None]:
        await authenticator(
            {"authorization": f"Bearer {TOKEN}", PROJECT_SCOPE_HEADER: "p1"}
        )
        accepted = _CONNECTION_PROJECT_SCOPE.get()
        with pytest.raises(ValueError):
            await authenticator(
                {"authorization": "Bearer wrong", PROJECT_SCOPE_HEADER: "p2"}
            )
        return accepted, _CONNECTION_PROJECT_SCOPE.get()

    accepted, after_rejection = asyncio.run(scenario())
    assert accepted == "p1"
    # A rejected handshake never rebinds the scope of an accepted connection.
    assert after_rejection == "p1"


def test_an_absent_scope_header_keeps_the_daemon_visibility() -> None:
    """Only a *missing* header means "no scope"; it is not an error."""
    authenticator = ScopedConnectionAuthenticator(
        BearerTokenAuthenticator(TOKEN), _CONNECTION_PROJECT_SCOPE
    )

    async def scenario() -> str | None:
        marker = _CONNECTION_PROJECT_SCOPE.set("p9")
        try:
            await authenticator({"authorization": f"Bearer {TOKEN}"})
            return _CONNECTION_PROJECT_SCOPE.get()
        finally:
            _CONNECTION_PROJECT_SCOPE.reset(marker)

    assert asyncio.run(scenario()) is None


def test_a_present_but_unusable_scope_header_refuses_the_handshake() -> None:
    """A malformed narrowing hint must never degrade into the wider default."""
    authenticator = ScopedConnectionAuthenticator(
        BearerTokenAuthenticator(TOKEN), _CONNECTION_PROJECT_SCOPE
    )
    unusable: tuple[tuple[str, object], ...] = (
        ("empty", ""),
        ("over the byte cap", "x" * (MAX_PROJECT_SCOPE_BYTES + 1)),
        ("nul byte", "bad\x00id"),
        ("newline", "p1\np2"),
        ("padded", " p1 "),
        ("not a string", 7),
        ("no project id", "   "),
    )

    async def scenario() -> list[str | None]:
        results: list[str | None] = []
        for name, value in unusable:
            with pytest.raises(ProjectScopeHeaderError) as caught:
                await authenticator(
                    {"authorization": f"Bearer {TOKEN}", PROJECT_SCOPE_HEADER: value}
                )
            # The rejection never echoes the offending value.
            if isinstance(value, str) and value:
                assert value not in str(caught.value), name
            assert str(caught.value).startswith("scope header"), name
            results.append(_CONNECTION_PROJECT_SCOPE.get())
        return results

    # The connection is refused and no scope is bound: never "no scope = full
    # visibility" for a header the trusted host did send.
    assert asyncio.run(scenario()) == [None] * len(unusable)


def test_a_scope_header_repeated_under_two_casings_refuses_the_handshake() -> None:
    """Two spellings of one header is a smuggling shape, not a scope."""
    headers = {
        PROJECT_SCOPE_HEADER: "p1",
        PROJECT_SCOPE_HEADER.lower(): "p2",
    }
    with pytest.raises(ProjectScopeHeaderError):
        read_project_scope_header(headers)


def test_the_scope_reader_reports_the_single_usable_header_value() -> None:
    assert read_project_scope_header({}) is None
    assert read_project_scope_header({"authorization": "Bearer x"}) is None
    assert read_project_scope_header({PROJECT_SCOPE_HEADER.lower(): "p1"}) == "p1"
    assert read_project_scope_header({PROJECT_SCOPE_HEADER: "x" * MAX_PROJECT_SCOPE_BYTES}) == (
        "x" * MAX_PROJECT_SCOPE_BYTES
    )
