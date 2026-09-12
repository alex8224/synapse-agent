"""Daemon composition-root tests for authentication vs. authorization assembly.

The daemon exposes two *typed* extension points at its composition root:

- ``authenticator_factory(token)`` -> a ``ConnectionAuthenticator`` that turns
  inbound headers into an already-authenticated :class:`Principal`;
- ``authorizer_factory(principal)`` -> the trusted policy snapshot for that
  principal, restricted to the two built-in strategies
  (:class:`AclAuthorizer` / :class:`DaemonAuthorizer`).

Both default to the stock deployment (exact single-token Bearer authenticator +
full-privilege daemon policy), and trust can only be supplied explicitly by the
server composition root -- never by a client, a wire parameter, or a
``runtime.protocol.negotiate`` declaration.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from websockets.asyncio.client import connect

from synapse.runtime.daemon.application import RuntimeDaemon
from synapse.runtime.daemon.auth import BearerTokenAuthenticator, ScopedConnectionAuthenticator
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.service import (
    SESSION_READ,
    AccessControlledAgentRuntimeService,
    AclAuthorizer,
    AclGrant,
    DaemonAuthorizer,
    GetSessionQuery,
    PermissionDeniedError,
    Principal,
    SessionView,
    SubmitTurnCommand,
    UsageView,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import RUNTIME_WIRE_VERSION

PROJECT = "project-a"
THREAD = "thread-a"
REF = SessionRef(PROJECT, THREAD)
DAEMON_SUBJECT = "runtime-daemon"


class SpyDelegate:
    """Runtime-service delegate that records every call it actually receives."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _record(self, name: str) -> str:
        self.calls.append(name)
        return name

    async def submit_turn(self, command: Any) -> Any:
        return self._record("submit_turn")

    async def open_session(self, command: Any) -> Any:
        return self._record("open_session")

    async def cancel_turn(self, command: Any) -> Any:
        return self._record("cancel_turn")

    async def steer_turn(self, command: Any) -> Any:
        return self._record("steer_turn")

    async def resume_turn(self, command: Any) -> Any:
        return self._record("resume_turn")

    async def pending_approval(self, query: Any) -> Any:
        return self._record("pending_approval")

    async def close_session(self, command: Any) -> Any:
        return self._record("close_session")

    async def get_session(self, query: Any) -> SessionView:
        self._record("get_session")
        return SessionView(
            project_id=PROJECT,
            thread_id=THREAD,
            status="idle",
            active_turn_id=None,
            latest_sequence=0,
            usage=UsageView(input_tokens=0, output_tokens=0, cache_tokens=0),
            last_error=None,
            last_activity_at="2025-01-01T00:00:00+00:00",
        )

    async def stat_artifact(self, query: Any) -> Any:
        return self._record("stat_artifact")

    async def list_artifacts(self, query: Any) -> Any:
        return self._record("list_artifacts")

    async def read_artifact(self, query: Any) -> Any:
        return self._record("read_artifact")

    async def read_events(self, query: Any) -> Any:
        return self._record("read_events")

    def watch_events(self, session: Any, **kwargs: Any) -> Any:
        del kwargs
        return self._record("watch_events")


GRANTS_BY_SUBJECT: dict[str, frozenset[str]] = {"alice": frozenset({SESSION_READ})}


def _policy_from_table(principal: Principal) -> AclAuthorizer:
    """Composition-root policy: only listed subjects get their listed grants."""
    capabilities = GRANTS_BY_SUBJECT.get(principal.subject)
    if capabilities is None:
        return AclAuthorizer([])
    return AclAuthorizer(
        [
            AclGrant(
                principal.subject,
                PROJECT,
                capabilities,
                frozenset({THREAD}),
            )
        ]
    )


class FixedSubjectAuthenticator:
    """Test-only authenticator: every connection becomes one fixed subject."""

    def __init__(self, subject: str) -> None:
        self.subject = subject

    async def __call__(self, headers: Mapping[str, str]) -> Principal:
        del headers
        return Principal(self.subject)


def _daemon(tmp_path: Path, delegate: Any, **kwargs: Any) -> RuntimeDaemon:
    return RuntimeDaemon(
        DaemonConfig(state_dir=tmp_path, port=0),
        service_factory=lambda principal: delegate,
        **kwargs,
    )


async def _call(ws: Any, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    await ws.send(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    )
    return json.loads(await ws.recv())


def _session_params() -> dict[str, Any]:
    return {"session": {"project_id": PROJECT, "thread_id": THREAD}}


def test_default_composition_binds_the_daemon_policy_unchanged(tmp_path: Path) -> None:
    delegate = SpyDelegate()
    daemon = _daemon(tmp_path, delegate)

    assert daemon._authenticator_factory is None
    assert daemon._authorizer_factory is None

    async def run() -> None:
        service = daemon._make_service(Principal(DAEMON_SUBJECT))
        assert isinstance(service, AccessControlledAgentRuntimeService)
        assert isinstance(service._authorizer, DaemonAuthorizer)
        view = await service.get_session(GetSessionQuery(REF))
        assert view.project_id == PROJECT
        # The daemon principal keeps every exact runtime scope.
        assert await service.submit_turn(SubmitTurnCommand(REF, "hello")) == "submit_turn"

    asyncio.run(run())
    assert delegate.calls == ["get_session", "submit_turn"]


def test_injected_authorizer_factory_scopes_privileges_per_subject(
    tmp_path: Path,
) -> None:
    delegate = SpyDelegate()
    daemon = _daemon(tmp_path, delegate, authorizer_factory=_policy_from_table)

    async def run() -> None:
        alice = daemon._make_service(Principal("alice"))
        assert isinstance(alice._authorizer, AclAuthorizer)
        assert (await alice.get_session(GetSessionQuery(REF))).thread_id == THREAD

        # A different authenticated subject has no grant at all.
        bob = daemon._make_service(Principal("bob"))
        with pytest.raises(PermissionDeniedError):
            await bob.get_session(GetSessionQuery(REF))

        # The right subject with an insufficient capability is denied too.
        with pytest.raises(PermissionDeniedError):
            await alice.submit_turn(SubmitTurnCommand(REF, "hello"))

        # The injected snapshot replaces the stock policy: even the daemon
        # subject only holds what the ACL grants.
        daemon_service = daemon._make_service(Principal(DAEMON_SUBJECT))
        with pytest.raises(PermissionDeniedError):
            await daemon_service.submit_turn(SubmitTurnCommand(REF, "hello"))

    asyncio.run(run())
    # Only the single authorized read ever reached the delegate.
    assert delegate.calls == ["get_session"]


def test_authorizer_factory_cannot_widen_the_closed_policy_set(tmp_path: Path) -> None:
    class AllowEverything:
        def authorize(self, principal: Any, capability: str, session: Any) -> None:
            return None

        def authorize_project(self, principal: Any, capability: str, project_id: str) -> None:
            return None

    delegate = SpyDelegate()
    daemon = _daemon(tmp_path, delegate, authorizer_factory=lambda principal: AllowEverything())

    with pytest.raises(TypeError):
        daemon._make_service(Principal("alice"))
    assert delegate.calls == []


def test_default_wire_deployment_keeps_bearer_and_full_daemon_policy(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        delegate = SpyDelegate()
        settings = SimpleNamespace(resolved_catalog_path=lambda: tmp_path / "catalog.sqlite")
        with patch(
            "synapse.runtime.daemon.application.load_global_settings",
            return_value=settings,
        ):
            daemon = _daemon(tmp_path, delegate)
            metadata = await daemon.start()
            try:
                # The stock deployment still authenticates with the exact bearer
                # authenticator; the scope decorator only records the trusted
                # project header on top of it.
                authenticator = daemon.server.authenticator
                assert isinstance(authenticator, ScopedConnectionAuthenticator)
                assert isinstance(authenticator.inner, BearerTokenAuthenticator)
                token = (tmp_path / "token").read_text(encoding="utf-8").strip()
                async with connect(
                    f"ws://127.0.0.1:{metadata['port']}",
                    additional_headers={"Authorization": f"Bearer {token}"},
                ) as ws:
                    response = await _call(ws, 1, "runtime.session.get", _session_params())
                    assert response["result"]["project_id"] == PROJECT
                    submitted = await _call(
                        ws,
                        2,
                        "runtime.turn.submit",
                        {**_session_params(), "text": "hello"},
                    )
                    assert submitted["result"] == "submit_turn"
            finally:
                await daemon.shutdown()

        assert delegate.calls == ["get_session", "submit_turn"]

    asyncio.run(run())


def test_wire_custom_authenticator_acl_scope_and_negotiate_grants_nothing(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        delegate = SpyDelegate()
        settings = SimpleNamespace(resolved_catalog_path=lambda: tmp_path / "catalog.sqlite")
        with patch(
            "synapse.runtime.daemon.application.load_global_settings",
            return_value=settings,
        ):
            daemon = _daemon(
                tmp_path,
                delegate,
                authenticator_factory=lambda token: FixedSubjectAuthenticator("alice"),
                authorizer_factory=_policy_from_table,
            )
            metadata = await daemon.start()
            try:
                async with connect(
                    f"ws://127.0.0.1:{metadata['port']}",
                    additional_headers={"Authorization": "Bearer replaced-by-factory"},
                ) as ws:
                    negotiated = await _call(
                        ws,
                        1,
                        "runtime.protocol.negotiate",
                        {"versions": [RUNTIME_WIRE_VERSION]},
                    )
                    assert negotiated["result"]["wire_version"] == RUNTIME_WIRE_VERSION
                    # Declared protocol features are never authorization grants.
                    assert "capabilities" in negotiated["result"]

                    allowed = await _call(ws, 2, "runtime.session.get", _session_params())
                    assert allowed["result"]["project_id"] == PROJECT

                    denied = await _call(
                        ws,
                        3,
                        "runtime.turn.submit",
                        {**_session_params(), "text": "hello"},
                    )
                    assert denied["error"]["data"]["service_code"] == "permission_denied"
            finally:
                await daemon.shutdown()

        # Negotiation and the denial both left the delegate untouched.
        assert delegate.calls == ["get_session"]

    asyncio.run(run())
