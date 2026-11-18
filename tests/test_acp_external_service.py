from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from synapse.acp.external_service import DaemonExternalSessions
from synapse.runtime.daemon.external_runtime import ExternalRuntime
from synapse.runtime.daemon.external_session_pool import ExternalSessionPool
from synapse.runtime.service.external_agents import ExternalAgentCatalog
from synapse.runtime.service.session_management import CreateSessionCommand, SessionProjectContext
from synapse.runtime.service.session_metadata import SessionMetadataService
from synapse.runtime.sessions.ref import SessionRef

_AGENT = '''
import json, sys
state = {'model': 'a'}
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'initialize':
        result = {'protocolVersion': 1}
    elif method == 'session/new':
        result = {'sessionId': 'peer', 'configOptions': [dict(
            type='select', id='model', name='Model', category='model',
            currentValue=state['model'],
            options=[dict(value='a', name='Model A'), dict(value='b', name='Model B')])]}
    elif method == 'session/set_config_option':
        state['model'] = msg['params']['value']
        result = {'configOptions': [dict(
            type='select', id='model', name='Model', category='model',
            currentValue=state['model'],
            options=[dict(value='a', name='Model A'), dict(value='b', name='Model B')])]}
    elif method == 'session/prompt':
        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
            'sessionId': 'peer', 'update': {'sessionUpdate': 'agent_message_chunk',
                                            'content': {'type': 'text', 'text': 'hello'}}}}),
              flush=True)
        result = {'stopReason': 'end_turn'}
    elif method == 'session/cancel':
        result = {}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)
'''


def _setup(tmp_path: Path, *, mode: str = "manual"):
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite", workspace=tmp_path
    )
    config = {"agents": [{
        "id": "test",
        "name": "Test agent",
        "command": sys.executable,
        "args": ["-u", "-c", _AGENT],
        "permission_mode": mode,
    }]}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    pool = ExternalSessionPool(catalog)
    service = DaemonExternalSessions(ExternalRuntime(pool))
    metadata = SessionMetadataService(
        lambda project_id: SessionProjectContext(project_id=project_id, settings=settings)
    )
    ref = SessionRef(project_id="project", thread_id="external")
    return settings, service, metadata, ref


def test_external_session_surface_lists_configures_and_streams(tmp_path: Path) -> None:
    settings, service, metadata, ref = _setup(tmp_path)

    async def run() -> None:
        agents = service.list_agents()
        assert [(a.id, a.name, a.permission_mode) for a in agents] == [
            ("test", "Test agent", "manual")
        ]
        assert service.binding(settings, "external") is None
        await service.create(
            metadata, CreateSessionCommand(project_id="project", thread_id="external"), "test"
        )
        assert service.binding(settings, "external") == ("test", None)

        view = await service.initialize(ref, settings, tmp_path)
        assert (view.agent_id, view.remote_session_id, view.permission_mode) == (
            "test",
            "peer",
            "manual",
        )
        assert [(o.id, o.category, o.current_value) for o in view.options] == [
            ("model", "model", "a")
        ]
        assert [v.value for v in view.options[0].values] == ["a", "b"]

        changed = await service.set_config(ref, settings, tmp_path, "model", "b")
        assert changed.options[0].current_value == "b"
        reinitialized = await service.initialize(ref, settings, tmp_path)
        assert reinitialized.options[0].current_value == "b"

        broker = service.broker(ref)
        assert broker is not None
        received = []
        subscription = broker.subscribe(received.append)
        try:
            await service.submit(
                ref, settings, tmp_path, turn_id="turn", text="hi", publish=broker.emit
            )
            await asyncio.sleep(0)
            kinds = [envelope.event.kind.value for envelope in received]
            assert "answer_delta" in kinds
            assert kinds[-1] == "turn_completed"
        finally:
            subscription.close()

        assert service.binding(settings, "external") == ("test", "peer")
        await service.shutdown()
        assert service.broker(ref) is None

    asyncio.run(run())


def test_external_full_access_is_reported_not_enforced(tmp_path: Path) -> None:
    settings, service, _metadata, ref = _setup(tmp_path, mode="full_access")

    async def run() -> None:
        assert service.list_agents()[0].permission_mode == "full_access"
        await service.shutdown()

    asyncio.run(run())


def test_external_turn_failure_emits_terminal_and_close_releases_pool(tmp_path: Path) -> None:
    settings, service, metadata, ref = _setup(tmp_path)

    async def run() -> None:
        await service.create(
            metadata, CreateSessionCommand(project_id="project", thread_id="external"), "test"
        )
        await service.initialize(ref, settings, tmp_path)
        assert service._runtime.pool.session(("project", "external")) is not None

        broker = service.broker(ref)
        assert broker is not None
        received = []
        subscription = broker.subscribe(received.append)
        try:
            # Force turn failure by invalidating the live session before submit
            live_session = service._runtime.pool.session(("project", "external"))
            assert live_session is not None
            live_session.invalidate()
            try:
                await service.submit(
                    ref,
                    settings,
                    tmp_path,
                    turn_id="failed-turn",
                    text="hi",
                    publish=broker.emit,
                )
            except Exception:
                pass
            kinds = [envelope.event.kind.value for envelope in received]
            assert kinds == ["turn_failed"]
        finally:
            subscription.close()

        await service.close(ref)
        assert service.broker(ref) is None
        assert service._runtime.pool.session(("project", "external")) is None
        await service.shutdown()

    asyncio.run(run())
