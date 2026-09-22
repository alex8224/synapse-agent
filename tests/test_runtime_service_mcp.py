"""Unit tests for the runtime MCP server CRUD and maintenance service surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.runtime.service import (
    AclAuthorizer,
    AclGrant,
    LocalAgentRuntimeService,
    PermissionDeniedError,
    Principal,
    bind_access,
)
from synapse.runtime.service.access import MCP_READ, MCP_WRITE
from synapse.runtime.service.mcp_management import (
    DeleteMcpServerCommand,
    ListMcpServersQuery,
    McpServerListResult,
    SaveMcpServerCommand,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import dispatch

REF = SessionRef(project_id="test-proj", thread_id="thread-1")
PRINCIPAL = Principal("user-test")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_mcp_env(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    synapse_dir = ws / ".synapse"
    synapse_dir.mkdir()
    mcp_file = synapse_dir / "mcp.json"
    initial_data = {
        "servers": [
            {
                "name": "fetch",
                "transport": "stdio",
                "command": "uvx",
                "args": ["mcp-server-fetch"],
                "enabled": True,
                "tool_prefix": "fetch_",
                "include_tools": ["fetch_url"],
            },
            {
                "name": "github",
                "transport": "sse",
                "url": "https://api.github.com/mcp",
                "enabled": False,
            },
        ]
    }
    mcp_file.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

    settings = SimpleNamespace(
        project_root=ws,
        mcp_config_path=mcp_file,
        mcp_servers_json=None,
        enable_mcp=True,
    )

    class FakeAgent:
        _coding_mcp_servers = ("fetch",)
        _coding_mcp_warnings = ("fetch: test warning",)
        _coding_mcp_server_states = (
            {
                "name": "fetch",
                "enabled": True,
                "attached": True,
                "discovered": ("fetch_url", "fetch_html"),
                "loaded": ("fetch_url",),
                "include_tools": ("fetch_url",),
            },
            {
                "name": "github",
                "enabled": False,
                "attached": False,
                "discovered": (),
                "loaded": (),
                "include_tools": (),
            },
        )

    agent = FakeAgent()
    fake_session = SimpleNamespace(
        ref=REF,
        agent=agent,
        settings=settings,
    )

    class FakeManager(RuntimeManager):
        def __init__(self):
            self.project_id = "test-proj"
            self.settings = settings
            self.workspace = ws

        def get_session_ref(self, ref):
            return fake_session

        def build_mcp_rebinding(self, ref, server, enabled, include_tools):
            return agent, settings

        async def rebind_session_ref(self, ref, new_agent, new_settings):
            return None

    mgr = FakeManager()
    service = LocalAgentRuntimeService(lambda pid: mgr if pid == "test-proj" else None)
    return SimpleNamespace(service=service, mcp_file=mcp_file, workspace=ws, settings=settings)


def test_list_mcp_servers(fake_mcp_env):
    res = run(fake_mcp_env.service.list_mcp_servers(ListMcpServersQuery(session=REF)))
    assert isinstance(res, McpServerListResult)
    assert res.mcp_enabled is True
    assert len(res.servers) == 2

    by_name = {s.name: s for s in res.servers}
    fetch = by_name["fetch"]
    assert fetch.transport == "stdio"
    assert fetch.command == "uvx"
    assert fetch.args == ("mcp-server-fetch",)
    assert fetch.enabled is True
    assert fetch.attached is True
    assert fetch.discovered == ("fetch_url", "fetch_html")
    assert fetch.loaded == ("fetch_url",)
    assert fetch.include_tools == ("fetch_url",)

    github = by_name["github"]
    assert github.transport == "sse"
    assert github.url == "https://api.github.com/mcp"
    assert github.enabled is False
    assert github.attached is False


def test_save_mcp_server_new(fake_mcp_env):
    cmd = SaveMcpServerCommand(
        session=REF,
        server={
            "name": "custom_stdio",
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "custom_mcp"],
            "enabled": True,
            "tool_prefix": "custom_",
        },
    )
    res = run(fake_mcp_env.service.save_mcp_server(cmd))
    assert any(s.name == "custom_stdio" for s in res.servers)

    # Verify persisted to disk
    disk = json.loads(fake_mcp_env.mcp_file.read_text(encoding="utf-8"))
    disk_servers = {s["name"]: s for s in disk["servers"]}
    assert "custom_stdio" in disk_servers
    assert disk_servers["custom_stdio"]["command"] == "python"
    assert disk_servers["custom_stdio"]["args"] == ["-m", "custom_mcp"]


def test_save_mcp_server_update_and_rename(fake_mcp_env):
    cmd = SaveMcpServerCommand(
        session=REF,
        original_name="github",
        server={
            "name": "github_renamed",
            "transport": "sse",
            "url": "https://api.github.com/mcp/v2",
            "enabled": True,
        },
    )
    res = run(fake_mcp_env.service.save_mcp_server(cmd))
    names = [s.name for s in res.servers]
    assert "github_renamed" in names
    assert "github" not in names

    disk = json.loads(fake_mcp_env.mcp_file.read_text(encoding="utf-8"))
    disk_names = [s["name"] for s in disk["servers"]]
    assert "github_renamed" in disk_names
    assert "github" not in disk_names


def test_delete_mcp_server(fake_mcp_env):
    cmd = DeleteMcpServerCommand(session=REF, name="fetch")
    res = run(fake_mcp_env.service.delete_mcp_server(cmd))
    assert not any(s.name == "fetch" for s in res.servers)

    disk = json.loads(fake_mcp_env.mcp_file.read_text(encoding="utf-8"))
    disk_names = [s["name"] for s in disk["servers"]]
    assert "fetch" not in disk_names


def test_mcp_acl_enforcement(fake_mcp_env):
    # With only MCP_READ grant
    auth_read = AclAuthorizer(
        grants=(AclGrant(PRINCIPAL.subject, "test-proj", frozenset((MCP_READ,))),)
    )
    sec_service_read = bind_access(fake_mcp_env.service, PRINCIPAL, auth_read)

    res = run(sec_service_read.list_mcp_servers(ListMcpServersQuery(session=REF)))
    assert len(res.servers) == 2

    # Save should fail with PermissionDeniedError
    with pytest.raises(PermissionDeniedError):
        run(
            sec_service_read.save_mcp_server(
                SaveMcpServerCommand(
                    session=REF,
                    server={"name": "test", "transport": "stdio"},
                )
            )
        )

    # With MCP_WRITE grant
    auth_write = AclAuthorizer(
        grants=(AclGrant(PRINCIPAL.subject, "test-proj", frozenset((MCP_WRITE,))),)
    )
    sec_service_write = bind_access(fake_mcp_env.service, PRINCIPAL, auth_write)

    res2 = run(
        sec_service_write.save_mcp_server(
            SaveMcpServerCommand(
                session=REF,
                server={"name": "test", "transport": "stdio"},
            )
        )
    )
    assert any(s.name == "test" for s in res2.servers)


def test_mcp_wire_protocol(fake_mcp_env):
    auth = AclAuthorizer(
        grants=(AclGrant(PRINCIPAL.subject, "test-proj", frozenset((MCP_READ, MCP_WRITE))),)
    )
    sec_service = bind_access(fake_mcp_env.service, PRINCIPAL, auth)

    # 1. runtime.mcp.list
    list_params = {"session": {"project_id": "test-proj", "thread_id": "thread-1"}}
    resp = run(dispatch(sec_service, "runtime.mcp.list", list_params))
    assert len(resp.servers) == 2

    # 2. runtime.mcp.save
    save_params = {
        "session": {"project_id": "test-proj", "thread_id": "thread-1"},
        "server": {"name": "wire_mcp", "transport": "stdio", "command": "echo"},
    }
    resp2 = run(dispatch(sec_service, "runtime.mcp.save", save_params))
    assert any(s.name == "wire_mcp" for s in resp2.servers)

    # 3. runtime.mcp.delete
    del_params = {
        "session": {"project_id": "test-proj", "thread_id": "thread-1"},
        "name": "wire_mcp",
    }
    resp3 = run(dispatch(sec_service, "runtime.mcp.delete", del_params))
    assert not any(s.name == "wire_mcp" for s in resp3.servers)
