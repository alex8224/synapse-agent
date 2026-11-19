from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from synapse.runtime.daemon.external_runtime import ExternalRuntime
from synapse.runtime.daemon.external_session_pool import ExternalSessionPool
from synapse.runtime.service.errors import NotFoundError
from synapse.runtime.service.event_types import TurnEventKind
from synapse.runtime.service.external_agents import ExternalAgentCatalog
from synapse.runtime.service.session_management import CreateSessionCommand, SessionProjectContext
from synapse.runtime.service.session_metadata import (
    SessionMetadataService,
    read_external_session_binding,
)
from synapse.runtime.sessions.ref import SessionRef

_AGENT = '''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg['method']
    if method == 'initialize':
        result = dict(protocolVersion=1, agentCapabilities=dict(loadSession=True))
    elif method == 'session/new':
        result = dict(sessionId='peer')
    elif method == 'session/set_config_option':
        result = dict(configOptions=[dict(
            type='select', id='model', name='Model', category='model',
            currentValue=msg['params']['value'],
            options=[dict(value='a', name='Model A'), dict(value='b', name='Model B')],
        )])
    elif method == 'session/prompt':
        print(json.dumps(dict(jsonrpc='2.0', method='session/update', params=dict(
            sessionId='peer', update=dict(sessionUpdate='agent_message_chunk',
                                         content=dict(type='text', text='answer'))))), flush=True)
        result = dict(stopReason='end_turn')
    elif method == 'session/load':
        assert msg['params']['sessionId'] == 'peer'
        print(json.dumps(dict(jsonrpc='2.0', method='session/update', params=dict(
            sessionId='peer', update=dict(sessionUpdate='agent_message_chunk',
                                         content=dict(type='text', text='history'))))), flush=True)
        result = {}
    else:
        raise AssertionError(method)
    print(json.dumps(dict(jsonrpc='2.0',id=msg['id'],result=result)),flush=True)
'''


def test_runtime_initializes_and_restores_persisted_binding(tmp_path: Path) -> None:
    settings = SimpleNamespace(sessions_path=tmp_path / "sessions.sqlite")
    assert read_external_session_binding(settings, "external") is None
    assert not settings.sessions_path.exists()
    metadata = SessionMetadataService(
        lambda project_id: SessionProjectContext(project_id=project_id, settings=settings)
    )
    config = {"agents": [{"id": "test", "name": "Test", "command": sys.executable,
                         "args": ["-u", "-c", _AGENT]}]}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    ref = SessionRef(project_id="test-project", thread_id="external")
    replay: list[str] = []
    events = []

    async def live(event: BaseModel) -> None:
        events.append(event)

    async def history(event: BaseModel) -> None:
        replay.append(event.model_dump()["content"]["text"])

    async def run() -> None:
        async with ExternalSessionPool(catalog).lifetime() as pool:
            runtime = ExternalRuntime(pool)
            with pytest.raises(NotFoundError, match="binding not found"):
                await runtime.initialize(
                    SessionRef(project_id="test-project", thread_id="missing"),
                    settings, tmp_path, on_update=live, on_replay=history,
                )
            command = CreateSessionCommand(project_id=ref.project_id, thread_id=ref.thread_id)
            with pytest.raises(NotFoundError, match="not configured"):
                await runtime.create(metadata, command, "unknown")
            assert not settings.sessions_path.exists()
            assert (await runtime.create(metadata, command, "test")).created
            session = await runtime.initialize(
                ref, settings, tmp_path, on_update=live, on_replay=history
            )
            assert (await session.set_config("model", "b")).config_options[0].current_value == "b"
            response = await runtime.turn(
                ref, session, settings, turn_id="turn", text="hello", live=live
            )
            assert response.stop_reason == "end_turn"
            assert [event.kind for event in events] == [TurnEventKind.ANSWER_DELTA]
            assert events[0].payload.text == "answer"
            assert (tmp_path / "transcript.sqlite").is_file()
            assert read_external_session_binding(settings, "external") == ("test", "peer")
            assert not replay
        async with ExternalSessionPool(catalog).lifetime() as pool:
            loaded = await ExternalRuntime(pool).initialize(
                ref, settings, tmp_path, on_update=live, on_replay=history,
            )
            assert loaded.created.session_id == "peer"
            assert replay == ["history"]
            assert len(events) == 1

    asyncio.run(run())


def test_runtime_injects_enabled_synapse_mcp_servers(tmp_path: Path) -> None:
    agent_script = """
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg["method"]
    if method == "initialize":
        result = dict(protocolVersion=1, agentCapabilities=dict(loadSession=True))
    elif method == "session/new":
        servers = msg["params"]["mcpServers"]
        assert len(servers) == 1
        assert servers[0]["name"] == "local_tool"
        assert servers[0]["command"] == "python"
        assert servers[0]["args"] == ["-m", "demo_mcp"]
        result = dict(sessionId="peer-mcp")
    else:
        raise AssertionError(method)
    print(json.dumps(dict(jsonrpc="2.0", id=msg["id"], result=result)), flush=True)
"""
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        mcp_config_path=tmp_path / "missing-mcp.json",
        mcp_servers_json=json.dumps({
            "servers": [
                {"name": "local_tool", "command": "python", "args": ["-m", "demo_mcp"]},
                {"name": "disabled_tool", "command": "python", "enabled": False},
            ]
        }),
    )
    metadata = SessionMetadataService(
        lambda project_id: SessionProjectContext(project_id=project_id, settings=settings)
    )
    config = {
        "agents": [
            {
                "id": "test",
                "name": "Test",
                "command": sys.executable,
                "args": ["-u", "-c", agent_script],
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    ref = SessionRef(project_id="test-project", thread_id="external-mcp")

    async def noop(event: BaseModel) -> None:
        del event

    async def run() -> None:
        async with ExternalSessionPool(catalog).lifetime() as pool:
            runtime = ExternalRuntime(pool)
            command = CreateSessionCommand(project_id=ref.project_id, thread_id=ref.thread_id)
            assert (await runtime.create(metadata, command, "test")).created
            session = await runtime.initialize(
                ref, settings, tmp_path, on_update=noop, on_replay=noop
            )
            assert session.created.session_id == "peer-mcp"

    asyncio.run(run())


def test_multiple_sessions_bind_different_acp_instances_and_restore(tmp_path: Path) -> None:
    def make_agent(tag: str) -> str:
        return f"""
import json, sys
tag = {tag!r}
for line in sys.stdin:
    msg = json.loads(line)
    method = msg["method"]
    if method == "initialize":
        result = dict(protocolVersion=1, agentCapabilities=dict(loadSession=True))
    elif method == "session/new":
        result = dict(sessionId=f"peer-{{tag}}")
    elif method == "session/load":
        assert msg["params"]["sessionId"] == f"peer-{{tag}}"
        result = dict()
    elif method == "session/prompt":
        print(json.dumps(dict(jsonrpc="2.0", method="session/update", params=dict(
            sessionId=f"peer-{{tag}}",
            update=dict(sessionUpdate="agent_message_chunk",
                        content=dict(type="text", text=f"reply-{{tag}}"))))), flush=True)
        result = dict(stopReason="end_turn")
    else:
        raise AssertionError(method)
    print(json.dumps(dict(jsonrpc="2.0", id=msg["id"], result=result)), flush=True)
"""

    settings = SimpleNamespace(sessions_path=tmp_path / "sessions.sqlite")
    metadata = SessionMetadataService(
        lambda project_id: SessionProjectContext(project_id=project_id, settings=settings)
    )
    config = {
        "agents": [
            {
                "id": "agent-a",
                "name": "Agent A",
                "command": sys.executable,
                "args": ["-u", "-c", make_agent("a")],
            },
            {
                "id": "agent-b",
                "name": "Agent B",
                "command": sys.executable,
                "args": ["-u", "-c", make_agent("b")],
            },
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    ref_a = SessionRef(project_id="test-project", thread_id="thread-a")
    ref_b = SessionRef(project_id="test-project", thread_id="thread-b")

    async def run() -> None:
        async with ExternalSessionPool(catalog).lifetime() as pool:
            runtime = ExternalRuntime(pool)
            assert (
                await runtime.create(
                    metadata,
                    CreateSessionCommand(project_id=ref_a.project_id, thread_id=ref_a.thread_id),
                    "agent-a",
                )
            ).created
            assert (
                await runtime.create(
                    metadata,
                    CreateSessionCommand(project_id=ref_b.project_id, thread_id=ref_b.thread_id),
                    "agent-b",
                )
            ).created

            async def noop(event: BaseModel) -> None:
                del event

            sess_a = await runtime.initialize(
                ref_a, settings, tmp_path, on_update=noop, on_replay=noop
            )
            sess_b = await runtime.initialize(
                ref_b, settings, tmp_path, on_update=noop, on_replay=noop
            )
            assert sess_a.created.session_id == "peer-a"
            assert sess_b.created.session_id == "peer-b"

        assert read_external_session_binding(settings, "thread-a") == ("agent-a", "peer-a")
        assert read_external_session_binding(settings, "thread-b") == ("agent-b", "peer-b")

        # Simulate daemon restart with a fresh pool and runtime.
        async with ExternalSessionPool(catalog).lifetime() as pool:
            runtime = ExternalRuntime(pool)
            replies_a: list[str] = []
            replies_b: list[str] = []

            async def live_a(event: BaseModel) -> None:
                if event.kind is TurnEventKind.ANSWER_DELTA:
                    replies_a.append(event.payload.text)

            async def live_b(event: BaseModel) -> None:
                if event.kind is TurnEventKind.ANSWER_DELTA:
                    replies_b.append(event.payload.text)

            async def noop(event: BaseModel) -> None:
                del event

            restored_a = await runtime.initialize(
                ref_a, settings, tmp_path, on_update=live_a, on_replay=noop
            )
            restored_b = await runtime.initialize(
                ref_b, settings, tmp_path, on_update=live_b, on_replay=noop
            )
            assert restored_a.created.session_id == "peer-a"
            assert restored_b.created.session_id == "peer-b"

            await runtime.turn(
                ref_a, restored_a, settings, turn_id="t-a", text="hi a", live=live_a
            )
            await runtime.turn(
                ref_b, restored_b, settings, turn_id="t-b", text="hi b", live=live_b
            )
            assert replies_a == ["reply-a"]
            assert replies_b == ["reply-b"]

    asyncio.run(run())
