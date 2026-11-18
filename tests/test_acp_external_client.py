from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from acp.schema import PermissionOption, ToolCallUpdate
from pydantic import BaseModel

from synapse.acp.external_client import (
    ExternalAgentSpec,
    ExternalAgentTimeout,
    ExternalTimeouts,
    open_external_session,
)

_FAKE = r'''
import json, os, sys

def send(**fields):
    print(json.dumps(dict(jsonrpc='2.0', **fields)), flush=True)

def reply(result):
    send(id=msg['id'], result=result)

def options(value):
    return [dict(type='select', id='model', name='Model', category='model',
                 currentValue=value, options=[dict(value=x,name=x) for x in ('a','b')])]

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'initialize':
        caps = dict(loadSession=sys.argv[1] == 'load')
        reply(dict(protocolVersion=1, agentCapabilities=caps))
    elif method == 'session/load' and sys.argv[1] == 'load':
        assert msg['params']['sessionId'] == 'test'
        send(method='session/update', params=dict(sessionId='test', update=dict(
             sessionUpdate='agent_message_chunk',content=dict(type='text',text='history'))))
        reply(dict(configOptions=options('b')))
    elif method == 'session/new' and sys.argv[1] != 'hang':
        assert os.environ['HTTPS_PROXY'] == 'http://localhost:7990'
        assert 'SYNAPSE_TEST_PRIVATE' not in os.environ
        assert msg['params']['mcpServers'] == []
        reply(dict(sessionId='test', configOptions=options('a')))
    elif method == 'session/set_config_option':
        reply(dict(configOptions=options(msg['params']['value'])))
    elif method == 'session/prompt' and sys.argv[1] == 'permission':
        pending = msg['id']
        send(id=99, method='session/request_permission', params=dict(
            sessionId='test', toolCall=dict(toolCallId='tool-1'), options=[
                dict(optionId='allow', name='Allow', kind='allow_once')]))
    elif msg.get('id') == 99:
        selected = msg['result']['outcome']['outcome'] == 'selected'
        send(id=pending, result=dict(stopReason='end_turn' if selected else 'cancelled'))
    elif method == 'session/prompt':
        send(method='session/update', params=dict(sessionId='other', update=dict(
             sessionUpdate='agent_message_chunk',content=dict(type='text',text='foreign'))))
        send(method='session/update', params=dict(sessionId='test', update=dict(
             sessionUpdate='agent_message_chunk',content=dict(type='text',text='visible'))))
        pending = msg['id']
    elif method == 'session/cancel' and sys.argv[1] != 'ignore-cancel':
        send(id=pending,result=dict(stopReason='cancelled'))
'''


def _spec(mode: str) -> ExternalAgentSpec:
    return ExternalAgentSpec(
        sys.executable,
        ("-u", "-c", _FAKE, mode),
        {"https_proxy": "http://localhost:7990", "SYNAPSE_TEST_PRIVATE": "not exported"},
    )


def test_external_acp_transport_config_updates_and_cancel(tmp_path: Path) -> None:
    async def run() -> None:
        updates: list[BaseModel] = []
        received = asyncio.Event()

        async def update(event: BaseModel) -> None:
            updates.append(event)
            received.set()

        async with open_external_session(_spec("normal"), tmp_path, update) as session:
            assert session.handshake.protocol_version == 1
            assert session.created.session_id == "test"
            changed = await session.set_config("model", "b")
            assert changed.config_options[0].current_value == "b"
            assert not await session.cancel()
            async with asyncio.TaskGroup() as tasks:
                prompt = tasks.create_task(session.prompt("hello"))
                await asyncio.wait_for(received.wait(), 5)
                assert await session.cancel()
            assert prompt.result().stop_reason == "cancelled"
            assert [m.model_dump()["content"]["text"] for m in updates] == ["visible"]
        with pytest.raises(RuntimeError, match="no longer usable"):
            await session.prompt("closed")

    asyncio.run(run())


def test_external_acp_load_delivers_history_only_to_replay_sink(tmp_path: Path) -> None:
    async def run() -> None:
        replay: list[BaseModel] = []

        async def live(event: BaseModel) -> None:
            pytest.fail(f'replay leaked to the live sink: {event}')

        async def history(event: BaseModel) -> None:
            replay.append(event)

        async with open_external_session(
            _spec('load'), tmp_path, live, existing_session_id='test', on_replay=history
        ) as session:
            assert session.created.session_id == 'test'
            assert session.created.config_options[0].current_value == 'b'
            assert [m.model_dump()['content']['text'] for m in replay] == ['history']

        with pytest.raises(RuntimeError, match='does not advertise session/load'):
            async with open_external_session(
                _spec('normal'), tmp_path, live, existing_session_id='test', on_replay=history
            ):
                pytest.fail('unsupported load must not fall back to session/new')

    asyncio.run(run())


@pytest.mark.parametrize(
    ('decision', 'expected'), [(None, 'cancelled'), ('allow', 'end_turn'), ('bad', 'cancelled')]
)
def test_external_acp_permission_requires_a_valid_decision(
    tmp_path: Path, decision: str | None, expected: str
) -> None:
    async def run() -> None:
        async def update(event: BaseModel) -> None:
            pytest.fail(f'unexpected event: {event}')

        async def permission(tool: ToolCallUpdate, options: list[PermissionOption]) -> str | None:
            assert tool.tool_call_id == 'tool-1'
            assert [option.option_id for option in options] == ['allow']
            return decision

        async with open_external_session(
            _spec('permission'),
            tmp_path,
            update,
            on_permission=permission if decision is not None else None,
        ) as session:
            response = await asyncio.wait_for(session.prompt('test permission'), 5)
            assert response.stop_reason == expected

    asyncio.run(run())


def test_external_acp_bounds_cancel_settlement(tmp_path: Path) -> None:
    async def run() -> None:
        received = asyncio.Event()

        async def update(_event: BaseModel) -> None:
            received.set()

        async with open_external_session(
            _spec("ignore-cancel"), tmp_path, update,
            timeouts=ExternalTimeouts(prompt=8, cancel_settle=0.2),
        ) as session:
            task = asyncio.create_task(session.prompt("hello"))
            try:
                await asyncio.wait_for(received.wait(), 5)
                assert await session.cancel()
                assert not await session.cancel()
                with pytest.raises(ExternalAgentTimeout, match="cancel settlement"):
                    await asyncio.wait_for(task, 2)
                assert not session.usable
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_external_acp_reports_creation_timeout(tmp_path: Path) -> None:
    async def run() -> None:
        async def update(event: BaseModel) -> None:
            pytest.fail(f"unexpected event: {event}")

        with pytest.raises(ExternalAgentTimeout, match="session/new"):
            async with open_external_session(
                _spec("hang"), tmp_path, update, timeouts=ExternalTimeouts(create=0.2)
            ):
                pytest.fail("opening a session should have timed out")

    asyncio.run(run())
