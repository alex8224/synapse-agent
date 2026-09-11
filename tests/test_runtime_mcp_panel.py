"""End-to-end MCP panel contract: config -> attach -> discovered/loaded tools.

Drives the *daemon's* own wiring (`RuntimeDaemon._make_manager` ->
`LocalAgentRuntimeService.reload_mcp`) against a real stdio MCP server, so the
three console actions are covered end to end:

1. no server  -> attach every enabled server, persist nothing;
2. ``enabled`` -> persist the flag, reconnect;
3. ``include_tools`` -> persist the whitelist, reconnect, and report the tools
   that actually reached the agent's tool list.

The reported per-server state is what lets the web console stop presenting the
configured flag as "running".
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from synapse.config import load_settings
from synapse.runtime.daemon.application import RuntimeDaemon
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.service import LocalAgentRuntimeService
from synapse.runtime.service.commands import OpenSessionCommand, ReloadMcpCommand
from synapse.runtime.sessions.ref import SessionRef

#: Minimal stdio MCP server (the official SDK) exposing two tools.
SERVER_SOURCE = '''
from mcp.server.fastmcp import FastMCP

server = FastMCP("demo")


@server.tool()
def alpha(value: str) -> str:
    """First demo tool."""
    return f"alpha:{value}"


@server.tool()
def beta(value: str) -> str:
    """Second demo tool."""
    return f"beta:{value}"


if __name__ == "__main__":
    server.run()
'''


def _workspace(tmp_path: Path, monkeypatch: Any, *, include_tools: list[str]) -> Path:
    workspace = tmp_path / "proj"
    config_dir = workspace / ".synapse"
    config_dir.mkdir(parents=True)
    server_path = tmp_path / "demo_server.py"
    server_path.write_text(SERVER_SOURCE, encoding="utf-8")
    (config_dir / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "demo",
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(server_path)],
                        "enabled": True,
                        "include_tools": include_tools,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # The developer's own ~/.synapse/mcp.json must never leak into this test.
    monkeypatch.setattr(
        "synapse.settings.config_paths.user_config_dir",
        lambda: (tmp_path / "home" / ".synapse").resolve(),
    )
    monkeypatch.setattr("synapse.settings.config_paths.executable_config_dirs", lambda: [])
    return workspace


def test_reload_mcp_attaches_reports_and_persists_the_tool_whitelist(
    tmp_path: Path, monkeypatch: Any
) -> None:
    async def run() -> None:
        workspace = _workspace(tmp_path, monkeypatch, include_tools=["alpha"])
        settings = load_settings(
            workspace=workspace,
            model="openai:gpt-4.1",
            active_model="openai:gpt-4.1",
            enable_mcp=True,
            mcp_eager=False,
        )
        monkeypatch.setattr(
            "synapse.runtime.daemon.application.load_project_settings",
            lambda workspace=None: settings,
        )
        daemon = RuntimeDaemon(DaemonConfig(state_dir=tmp_path / "state"))
        manager = daemon._make_manager(  # noqa: SLF001 - the daemon's own wiring
            SimpleNamespace(project_id="p1", workspace=workspace)
        )
        service = LocalAgentRuntimeService(lambda project_id: manager)
        ref = SessionRef(project_id="p1", thread_id="t1")
        await service.open_session(OpenSessionCommand(ref))
        try:
            # 1) attach-all: no config write, real state reported.
            attached = await service.reload_mcp(ReloadMcpCommand(ref))
            assert attached.server is None
            assert attached.attached is True
            assert attached.active_servers == ("demo",)
            assert attached.tool_names == ("demo__alpha",)
            state = {entry.name: entry for entry in attached.servers}["demo"]
            assert state.enabled is True
            assert state.attached is True
            assert state.include_tools == ("alpha",)
            assert state.discovered == ("alpha", "beta")
            assert state.loaded == ("demo__alpha",)
            assert attached.warnings == ()

            # 1b) a second attach-all reuses the live connection: the reported
            #     state must still say "attached" (the rebuild path that compiles
            #     the pool's tools in used to derive its server list from the
            #     process-global active pool, which is never set here).
            again = await service.reload_mcp(ReloadMcpCommand(ref))
            assert again.attached is True
            assert again.active_servers == ("demo",)
            assert again.tool_names == ("demo__alpha",)
            again_state = {entry.name: entry for entry in again.servers}["demo"]
            assert again_state.attached is True
            assert again_state.loaded == ("demo__alpha",)

            # 2) whitelist write: persisted, then a fresh connection reports the
            #    newly selected tool instead of the cached one.
            saved = await service.reload_mcp(
                ReloadMcpCommand(ref, server="demo", include_tools=("beta",))
            )
            assert saved.enabled is None
            assert saved.tool_names == ("demo__beta",)
            state = {entry.name: entry for entry in saved.servers}["demo"]
            assert state.include_tools == ("beta",)
            assert state.loaded == ("demo__beta",)
            on_disk = json.loads(
                (workspace / ".synapse" / "mcp.json").read_text(encoding="utf-8")
            )
            assert on_disk["servers"][0]["include_tools"] == ["beta"]
            assert on_disk["servers"][0]["enabled"] is True

            # 3) clearing the whitelist loads every tool the server advertises.
            cleared = await service.reload_mcp(
                ReloadMcpCommand(ref, server="demo", include_tools=())
            )
            state = {entry.name: entry for entry in cleared.servers}["demo"]
            assert state.include_tools == ()
            assert state.loaded == ("demo__alpha", "demo__beta")
            on_disk = json.loads(
                (workspace / ".synapse" / "mcp.json").read_text(encoding="utf-8")
            )
            assert "include_tools" not in on_disk["servers"][0]

            # 4) disabling the server persists the flag and drops its tools.
            disabled = await service.reload_mcp(
                ReloadMcpCommand(ref, server="demo", enabled=False)
            )
            assert disabled.enabled is False
            assert disabled.attached is False
            assert disabled.tool_names == ()
            state = {entry.name: entry for entry in disabled.servers}["demo"]
            assert state.enabled is False
            assert state.attached is False
        finally:
            from synapse.integrations.mcp_client import close_all_mcp_pools

            close_all_mcp_pools()
            await manager.shutdown()

    asyncio.run(run())
