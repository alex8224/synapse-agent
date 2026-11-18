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
