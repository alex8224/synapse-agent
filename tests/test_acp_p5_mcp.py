"""ACP P5 MCP conversion and isolation tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from acp.schema import EnvVariable, HttpHeader, McpServerStdio

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.lifecycle import ACPSessionCatalog
from synapse.acp.mcp import ACPMCPError, mcp_server_configs_from_acp, merge_mcp_server_configs
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry
from synapse.integrations.mcp_client import McpServerConfig


class _Runtime:
    def __init__(self) -> None:
        self.agent = None

    async def wait_for_settlement(self, handle: Any) -> None:
        del handle


class _Manager:
    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime

    async def close_session(self, thread_id: str, *, cancel_active: bool) -> None:
        del thread_id, cancel_active

    async def shutdown(self) -> None:
        return None


def test_stdio_mcp_conversion_keeps_values_out_of_metadata_shape() -> None:
    server = McpServerStdio(
        name="tools",
        command="python",
        args=["server.py"],
        env=[EnvVariable(name="TOKEN", value="secret")],
    )
    configs = mcp_server_configs_from_acp([server])
    assert configs[0].name == "tools"
    assert configs[0].command == "python"
    assert configs[0].env == {"TOKEN": "secret"}


def test_http_and_sse_conversion_preserves_transport_without_logging() -> None:
    from acp.schema import HttpMcpServer, SseMcpServer

    http = HttpMcpServer(
        name="http",
        url="https://example.invalid/mcp",
        headers=[HttpHeader(name="Authorization", value="Bearer secret")],
        type="http",
    )
    sse = SseMcpServer(
        name="sse", url="https://example.invalid/sse", headers=[], type="sse"
    )
    configs = mcp_server_configs_from_acp([http, sse])
    assert [(item.name, item.transport) for item in configs] == [
        ("http", "streamable_http"),
        ("sse", "sse"),
    ]
    assert configs[0].headers == {"Authorization": "Bearer secret"}


def test_acp_backed_mcp_and_duplicate_names_fail_closed() -> None:
    from acp.schema import AcpMcpServer

    with pytest.raises(ACPMCPError, match="ACP-backed"):
        mcp_server_configs_from_acp(
            [AcpMcpServer(name="remote", server_id="zed", type="acp")]
        )
    server = McpServerStdio(name="same", command="python", args=[], env=[])
    with pytest.raises(ACPMCPError, match="duplicate"):
        mcp_server_configs_from_acp([server, server])


def test_merge_union_dedup_and_conflict() -> None:
    project = [
        McpServerConfig(name="proj", transport="stdio", command="python", args=["a.py"]),
        McpServerConfig(name="shared", transport="stdio", command="python", args=["s.py"]),
    ]
    client = [
        McpServerConfig(name="cli", transport="stdio", command="python", args=["c.py"]),
        McpServerConfig(name="shared", transport="stdio", command="python", args=["s.py"]),
    ]
    merged = merge_mcp_server_configs(project, client)
    assert {item.name for item in merged} == {"proj", "cli", "shared"}

    conflicting = [
        McpServerConfig(name="shared", transport="stdio", command="python", args=["other.py"])
    ]
    with pytest.raises(ACPMCPError, match="conflict"):
        merge_mcp_server_configs(project, conflicting)


def test_merge_with_empty_sources_returns_union() -> None:
    only_client = [McpServerConfig(name="cli", transport="stdio", command="python")]
    assert merge_mcp_server_configs(None, only_client) == only_client
    only_project = [McpServerConfig(name="proj", transport="stdio", command="python")]
    assert merge_mcp_server_configs(only_project, None) == only_project
    assert merge_mcp_server_configs(None, None) == []


def test_mcp_pool_release_is_session_scoped(tmp_path: Path, monkeypatch: Any) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
        released: list[str] = []

        class FakeRegistry:
            def release(self, key: str) -> None:
                released.append(key)

        monkeypatch.setattr(
            "synapse.integrations.mcp_client.get_mcp_pool_registry",
            lambda: FakeRegistry(),
        )

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            runtime = _Runtime()
            return ACPManagedSession(descriptor, _Manager(runtime), runtime)  # type: ignore[arg-type]

        agent = SynapseACPAgent(registry=ACPSessionRegistry(factory), catalog=catalog)
        await agent.initialize(1)
        stdio = [McpServerStdio(name="a", command="python", args=[], env=[])]
        first = await agent.new_session(str(tmp_path), mcp_servers=stdio)
        second = await agent.new_session(str(tmp_path), mcp_servers=stdio)

        await agent.close_session(first.session_id)
        assert released == [f"acp:{first.session_id}"]
        await agent.close_session(second.session_id)
        assert released == [f"acp:{first.session_id}", f"acp:{second.session_id}"]
        await agent.shutdown()

    asyncio.run(run())


def test_mcp_startup_failure_releases_pool_and_raises(
    tmp_path: Path, monkeypatch: Any
) -> None:
    async def run() -> None:
        from synapse.acp.sessions import make_runtime_session_factory

        acquired: list[str] = []
        released: list[str] = []

        class FakePool:
            def close(self) -> None:
                self.closed = True

        class FakeResult:
            tools: list[Any] = []
            warnings: list[str] = ["boom"]
            servers: list[str] = []
            tool_names: list[str] = []

        class FakeRegistry:
            def __init__(self) -> None:
                self.pool = FakePool()

            def acquire(self, key: str, *, servers: Any, enabled: bool = True) -> Any:
                del servers, enabled
                acquired.append(key)
                return self.pool, FakeResult()

            def release(self, key: str) -> None:
                released.append(key)
                self.pool.close()

        monkeypatch.setattr(
            "synapse.integrations.mcp_client.get_mcp_pool_registry",
            lambda: FakeRegistry(),
        )
        factory = make_runtime_session_factory(
            settings_factory=lambda cwd: object(),
            agent_factory=lambda settings, descriptor: object(),
        )
        descriptor = ACPSessionDescriptor(
            session_id="sess-1",
            thread_id="sess-1",
            cwd=tmp_path,
            mcp_servers=(McpServerStdio(name="a", command="python", args=[], env=[]),),
        )
        with pytest.raises(RuntimeError, match="MCP startup failed"):
            await factory(descriptor)
        assert acquired == ["acp:sess-1"]
        assert released == ["acp:sess-1"]

    asyncio.run(run())
