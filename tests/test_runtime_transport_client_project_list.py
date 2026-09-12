"""`runtime.project.list` on the typed WebSocket client.

The client half of the project-enumeration contract: a bounded typed request
(``ListProjectsQuery``) becomes a bounded typed page (``ProjectListPage``), and
the server-computed ``visible_project_ids`` set never travels on the wire — not
even when a caller sets one locally.

The daemon-side visibility rules (ACL, connection scope, the wire decoder that
rejects a client-supplied filter) are covered by ``test_runtime_project_list.py``;
this file covers the client's own request shape and strict decoding.
"""

from __future__ import annotations

# The wire-shaped cases stay readable beside their assertions.
# ruff: noqa: E501
import asyncio
import json

import pytest

from synapse.runtime.service import (
    ListProjectsQuery,
    LocalAgentRuntimeService,
    ProjectListItem,
    ProjectListPage,
)
from synapse.runtime.service.access import Principal
from synapse.runtime.service.routing import CatalogProjectListProvider
from synapse.runtime.transport import (
    CAPABILITIES,
    RuntimeWebSocketClient,
    RuntimeWebSocketServer,
)
from synapse.runtime.transport.client import ProtocolTransportError


def page(*, next_offset: int | None = 2, total: int = 2) -> dict[str, object]:
    return {
        "projects": [
            {
                "project_id": "p0",
                "workspace_name": "P0",
                "git_branch": "main",
                "workspace_path": "/w/p0",
            },
            {
                "project_id": "p1",
                "workspace_name": None,
                "git_branch": None,
                "workspace_path": "/w/p1",
            },
        ],
        "next_offset": next_offset,
        "total": total,
    }


class _FakeConnection:
    """Minimal wire double: answers the handshake, then one canned result."""

    def __init__(self, result: object) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, object]] = []
        self.closed = False
        self._result = result

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            response: object = {
                "wire_version": "1",
                "supported_versions": ["1"],
                "capabilities": CAPABILITIES,
            }
        else:
            response = self._result
        await self.inbox.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "meta": {"wire_version": "1"},
                    "result": response,
                }
            )
        )

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True

    def business_frames(self) -> list[dict[str, object]]:
        return [
            frame
            for frame in self.frames
            if frame["method"] != "runtime.protocol.negotiate"
        ]


def _client(connection: _FakeConnection) -> RuntimeWebSocketClient:
    return RuntimeWebSocketClient(
        "ws://loopback", connect_factory=lambda *a, **k: connection
    )


def test_list_projects_decodes_a_typed_page_and_sends_pagination_only() -> None:
    async def run() -> None:
        connection = _FakeConnection(page(next_offset=2))
        result = await _client(connection).list_projects(
            ListProjectsQuery(limit=7, offset=3)
        )

        assert isinstance(result, ProjectListPage)
        assert [item.project_id for item in result.projects] == ["p0", "p1"]
        assert result.projects[0] == ProjectListItem("p0", "P0", "main", "/w/p0")
        assert result.projects[1].workspace_name is None
        assert result.projects[1].git_branch is None
        assert result.next_offset == 2 and result.total == 2

        frames = connection.business_frames()
        assert [frame["method"] for frame in frames] == ["runtime.project.list"]
        assert frames[0]["params"] == {"limit": 7, "offset": 3}

    asyncio.run(run())


def test_list_projects_sends_the_query_defaults_when_omitted() -> None:
    async def run() -> None:
        connection = _FakeConnection(page(next_offset=None))
        result = await _client(connection).list_projects(ListProjectsQuery())
        assert result.next_offset is None and result.total == 2
        assert connection.business_frames()[0]["params"] == {"limit": 50, "offset": 0}

    asyncio.run(run())


def test_a_client_supplied_visibility_set_never_reaches_the_wire() -> None:
    """The visible set is server-computed; the client cannot smuggle one in."""

    async def run() -> None:
        connection = _FakeConnection(page())
        await _client(connection).list_projects(
            ListProjectsQuery(visible_project_ids=("p0",))
        )
        params = connection.business_frames()[0]["params"]
        assert set(params) == {"limit", "offset"}
        assert "visible_project_ids" not in params

    asyncio.run(run())


def test_list_projects_rejects_a_foreign_query_object() -> None:
    async def run() -> None:
        connection = _FakeConnection(page())
        with pytest.raises(ValueError):
            await _client(connection).list_projects(object())  # type: ignore[arg-type]
        # The type check happens before any connect or frame.
        assert connection.frames == []

    asyncio.run(run())


BAD_PAGES: list[dict[str, object]] = [
    {"projects": [], "total": 0},
    {"projects": [], "next_offset": None, "total": -1},
    {"projects": [], "next_offset": -1, "total": 0},
    {"projects": [], "next_offset": None, "total": True},
    {"projects": {}, "next_offset": None, "total": 0},
    {
        "projects": [
            {"project_id": "p0", "workspace_name": None, "git_branch": None}
        ],
        "next_offset": None,
        "total": 1,
    },
    {
        "projects": [
            {
                "project_id": "",
                "workspace_name": None,
                "git_branch": None,
                "workspace_path": "/w/p0",
            }
        ],
        "next_offset": None,
        "total": 1,
    },
    {
        "projects": [
            {
                "project_id": "p0",
                "workspace_name": 3,
                "git_branch": None,
                "workspace_path": "/w/p0",
            }
        ],
        "next_offset": None,
        "total": 1,
    },
    {
        "projects": [
            {
                "project_id": "p0",
                "workspace_name": None,
                "git_branch": None,
                "workspace_path": "",
            }
        ],
        "next_offset": None,
        "total": 1,
    },
]


@pytest.mark.parametrize("bad", BAD_PAGES)
def test_a_malformed_project_page_is_a_protocol_failure(bad: dict[str, object]) -> None:
    async def run() -> None:
        connection = _FakeConnection(bad)
        transport = _client(connection)
        with pytest.raises(ProtocolTransportError):
            await transport.list_projects(ListProjectsQuery())
        # A result that violates its DTO shape fences the generation.
        assert connection.closed or transport._connection is None

    asyncio.run(run())


class _Row:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.workspace_path = f"/w/{project_id}"
        self.name = project_id.upper()
        self.git_branch = "main"


class _Catalog:
    def list_projects(self, *, limit: int = 100) -> list[_Row]:
        return [_Row("p0"), _Row("p1")][:limit]


def test_project_enumeration_round_trips_over_the_loopback_server() -> None:
    """The typed client call works against the real server + real service."""

    async def run() -> None:
        async def auth(headers: object) -> Principal:
            values = {str(key).lower(): value for key, value in dict(headers).items()}
            if values.get("authorization") != "Bearer good":
                raise ValueError("unauthorized")
            return Principal("loopback")

        service = LocalAgentRuntimeService(
            lambda _project_id: None,
            project_list_provider=CatalogProjectListProvider(_Catalog()),
        )
        server = RuntimeWebSocketServer(auth, lambda _principal: service, port=0)
        await server.start()
        try:
            port = server.bound_addresses[0][1]
            client = RuntimeWebSocketClient(
                f"ws://127.0.0.1:{port}", bearer_token="good"
            )
            first = await client.list_projects(ListProjectsQuery(limit=1))
            assert isinstance(first, ProjectListPage)
            assert [item.project_id for item in first.projects] == ["p0"]
            assert first.next_offset == 1 and first.total == 2

            second = await client.list_projects(
                ListProjectsQuery(limit=1, offset=first.next_offset or 0)
            )
            assert [item.project_id for item in second.projects] == ["p1"]
            assert second.next_offset is None
            await client.close()
        finally:
            await server.close()

    asyncio.run(run())
