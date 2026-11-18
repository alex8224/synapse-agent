from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from synapse.runtime.daemon.external_session_pool import ExternalSessionPool
from synapse.runtime.service.external_agents import ExternalAgentCatalog

_AGENT = """
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get('method') == 'initialize':
        if 'check-launch-args' in sys.argv:
            assert '--dangerously-skip-permissions' in sys.argv
        result = {'protocolVersion': 1}
    elif msg.get('method') == 'session/new':
        result = {'sessionId': 'remote'}
    elif msg.get('method') == 'session/prompt':
        pending = msg['id']
        options = [dict(optionId='always', name='Always', kind='allow_always')]
        if msg['params']['prompt'][0]['text'] != 'no single approval':
            options.append(dict(optionId='once', name='Once', kind='allow_once'))
        print(json.dumps(dict(jsonrpc='2.0', id=99, method='session/request_permission',
            params=dict(sessionId='remote', toolCall=dict(toolCallId='tool'), options=options))),
            flush=True)
        continue
    elif msg.get('id') == 99:
        outcome = msg['result']['outcome']
        assert outcome.get('optionId') != 'always'
        result = dict(stopReason='end_turn' if outcome.get('optionId') == 'once' else 'cancelled')
        msg['id'] = pending
    else:
        continue
    print(json.dumps(dict(jsonrpc='2.0', id=msg['id'], result=result)), flush=True)
"""


def test_pool_coalesces_opens_and_closes_sessions(tmp_path: Path) -> None:
    config = {"agents": [{"id": "test", "name": "Test", "command": sys.executable,
                         "args": ["-u", "-c", _AGENT]}]}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    pool = ExternalSessionPool(ExternalAgentCatalog.from_state_dir(tmp_path), capacity=1)

    async def update(event: BaseModel) -> None:
        pytest.fail(f"Unexpected update: {event}")

    async def run() -> None:
        async with pool.lifetime():
            with pytest.raises(ValueError, match="not configured"):
                await pool.open(("project", "first"), "unknown", tmp_path, update)
            first, second = await asyncio.gather(
                pool.open(("project", "first"), "test", tmp_path, update),
                pool.open(("project", "first"), "test", tmp_path, update),
            )
            assert first is second
            with pytest.raises(RuntimeError, match="launch binding"):
                await pool.open(("project", "first"), "other", tmp_path, update)
            with pytest.raises(RuntimeError, match="launch binding"):
                await pool.open(("project", "first"), "test", tmp_path.parent, update)
            with pytest.raises(RuntimeError, match="remote session binding"):
                await pool.open(
                    ("project", "first"), "test", tmp_path, update, existing_session_id="other"
                )
            with pytest.raises(RuntimeError, match="capacity"):
                await pool.open(("project", "other"), "test", tmp_path, update)
        with pytest.raises(RuntimeError, match="no longer usable"):
            await first.prompt("closed")
        with pytest.raises(RuntimeError, match="pool is closed"):
            await pool.open(("project", "other"), "test", tmp_path, update)

    asyncio.run(run())


def test_open_caller_disconnect_does_not_interrupt_binding(tmp_path: Path) -> None:
    config = {"agents": [{"id": "test", "name": "Test", "command": sys.executable,
                         "args": ["-u", "-c", _AGENT]}]}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    pool = ExternalSessionPool(ExternalAgentCatalog.from_state_dir(tmp_path))

    async def run() -> None:
        binding_started = asyncio.Event()
        release = asyncio.Event()
        persisted = []

        async def update(event: BaseModel) -> None:
            pytest.fail(f"Unexpected update: {event}")

        async def before_ready(session):
            binding_started.set()
            await release.wait()
            persisted.append(session.created.session_id)

        async with pool.lifetime():
            key = ("project", "first")
            first = asyncio.create_task(
                pool.open(key, "test", tmp_path, update, before_ready=before_ready)
            )
            await asyncio.wait_for(binding_started.wait(), 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            restored = await asyncio.wait_for(pool.open(key, "test", tmp_path, update), 5)
            assert persisted == [restored.created.session_id]

    asyncio.run(run())


@pytest.mark.parametrize(
    ('mode', 'text', 'reason'),
    [
        ('manual', 'test', 'cancelled'),
        ('full_access', 'test', 'cancelled'),
        ('full_access', 'no single approval', 'cancelled'),
    ],
)
def test_pool_permissions(
    tmp_path: Path, mode: str, text: str, reason: str
) -> None:
    args = ["-u", "-c", _AGENT]
    if mode == "full_access":
        args += ["check-launch-args", "--dangerously-skip-permissions"]
    config = {"agents": [{
        "id": "test",
        "name": "Test",
        "command": sys.executable,
        "args": args,
        "permission_mode": mode,
    }]}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    pool = ExternalSessionPool(ExternalAgentCatalog.from_state_dir(tmp_path))

    async def update(event: BaseModel) -> None:
        pytest.fail(f"Unexpected update: {event}")

    async def run() -> None:
        async with pool.lifetime():
            session = await pool.open(("project", "first"), "test", tmp_path, update)
            response = await asyncio.wait_for(session.prompt(text), 5)
            assert response.stop_reason == reason

    asyncio.run(run())
