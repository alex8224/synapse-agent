"""P8 official SDK lifecycle and capability compliance checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import acp
import pytest
from acp._transport import memory_transport_pair
from acp.helpers import update_agent_message_text
from acp.schema import ClientCapabilities, Implementation

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.lifecycle import ACPSessionCatalog
from synapse.acp.server import build_agent_connection
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry


class _Runtime:
    async def wait_for_settlement(self, handle: Any) -> None:
        del handle


class _Manager:
    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime

    async def close_session(self, thread_id: str, *, cancel_active: bool) -> None:
        del thread_id, cancel_active

    async def shutdown(self) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        del session_id, kwargs
        self.updates.append(update)


async def _run_lifecycle(root: Path) -> None:
    catalog = ACPSessionCatalog(root / "catalog.sqlite")

    async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
        runtime = _Runtime()
        return ACPManagedSession(descriptor, _Manager(runtime), runtime)  # type: ignore[arg-type]

    agent = SynapseACPAgent(
        registry=ACPSessionRegistry(factory),
        catalog=catalog,
    )
    client = _Client()
    client_transport, agent_transport = memory_transport_pair()
    server_connection = build_agent_connection(
        agent,
        agent_transport,
        use_unstable_protocol=True,
    )
    server_task = asyncio.create_task(server_connection.listen())
    connection = acp.connect_to_agent(
        client, client_transport, use_unstable_protocol=True
    )
    try:
        initialized = await connection.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="p8-client", version="0.1"),
        )
        assert initialized.protocol_version == 1
        assert initialized.agent_capabilities is not None
        assert initialized.agent_capabilities.session_capabilities.list is not None

        with pytest.raises(acp.RequestError):
            await connection._conn.send_request("session/unknown", {})
        with pytest.raises(acp.RequestError):
            await connection.new_session(cwd="relative", mcp_servers=[])

        created = await connection.new_session(cwd=str(root), mcp_servers=[])
        listed = await connection.list_sessions(cwd=str(root))
        assert [item.session_id for item in listed.sessions] == [created.session_id]

        catalog.append_update(
            created.session_id,
            1,
            update_agent_message_text("restored"),
        )
        await connection.close_session(created.session_id)
        loaded = await connection.load_session(str(root), created.session_id)
        assert loaded.config_options is not None
        for _ in range(20):
            if len(client.updates) >= 2:
                break
            await asyncio.sleep(0)
        assert [item.session_update for item in client.updates] == [
            "available_commands_update",
            "agent_message_chunk",
        ]

        before_resume = len(client.updates)
        resumed = await connection.resume_session(created.session_id, str(root))
        assert resumed.config_options is not None
        assert len(client.updates) == before_resume

        await connection._conn.send_request(  # noqa: SLF001 - SDK has no delete wrapper
            "session/delete", {"sessionId": created.session_id}
        )
        assert (await connection.list_sessions(cwd=str(root))).sessions == []
    finally:
        await connection.close()
        await server_connection.close()
        await asyncio.wait_for(server_task, timeout=10)


def test_official_sdk_covers_session_lifecycle_and_resume_semantics(tmp_path: Path) -> None:
    asyncio.run(_run_lifecycle(tmp_path))


def test_initialize_declares_only_implemented_capabilities(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            runtime = _Runtime()
            return ACPManagedSession(descriptor, _Manager(runtime), runtime)  # type: ignore[arg-type]

        agent = SynapseACPAgent(registry=ACPSessionRegistry(factory), catalog=catalog)
        response = await agent.initialize(1)
        caps = response.agent_capabilities

        # Not advertised: rich media, auth, acp-backed MCP, unstable extras.
        assert caps.load_session is True
        assert caps.prompt_capabilities.image is False
        assert caps.prompt_capabilities.audio is False
        assert caps.prompt_capabilities.embedded_context is False
        assert response.auth_methods == []
        assert caps.mcp_capabilities.http is True
        assert caps.mcp_capabilities.sse is True
        assert caps.mcp_capabilities.acp is False
        # Session capabilities advertised are exactly the implemented set.
        assert caps.session_capabilities.list is not None
        assert caps.session_capabilities.delete is not None
        assert caps.session_capabilities.additional_directories is not None
        assert caps.session_capabilities.fork is not None
        assert caps.session_capabilities.resume is not None
        assert caps.session_capabilities.close is not None
        await agent.shutdown()

    asyncio.run(run())
