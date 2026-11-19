from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel

from synapse.acp.external_client import ExternalTimeouts
from synapse.runtime.daemon import external_session_pool as pool_module
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


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, ExternalTimeouts().create),
        ({"create_timeout_s": 120}, 120.0),
    ],
)
def test_pool_applies_the_configured_create_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: dict, expected: float
) -> None:
    """Only the peer's own `session/new` budget is widened, per agent.

    A real 120s deadline cannot be waited out in a test, so the launch is
    intercepted: what matters is that the entry's value — and the default when
    it is absent — is what `open_external_session` is handed.
    """
    row = {
        "id": "test",
        "name": "Test",
        "command": sys.executable,
        "args": ["-u", "-c", _AGENT],
        **extra,
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps({"agents": [row]}))
    pool = ExternalSessionPool(ExternalAgentCatalog.from_state_dir(tmp_path))
    seen: dict[str, ExternalTimeouts] = {}

    @asynccontextmanager
    async def fake_open(spec, cwd, on_update, **kwargs):
        seen["timeouts"] = kwargs["timeouts"]
        raise RuntimeError("stop after capturing the timeouts")
        yield  # pragma: no cover - unreachable, keeps the contextmanager shape

    monkeypatch.setattr(pool_module, "open_external_session", fake_open)

    async def update(event: BaseModel) -> None:
        pytest.fail(f"Unexpected update: {event}")

    async def run() -> None:
        async with pool.lifetime():
            with pytest.raises(RuntimeError, match="capturing the timeouts"):
                await pool.open(("project", "first"), "test", tmp_path, update)

    asyncio.run(run())
    assert seen["timeouts"].create == expected


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


def test_pool_evicts_idle_session_and_frees_capacity(tmp_path: Path) -> None:
    config = {
        "agents": [
            {
                "id": "test",
                "name": "Test",
                "command": sys.executable,
                "args": ["-u", "-c", _AGENT],
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    pool = ExternalSessionPool(
        ExternalAgentCatalog.from_state_dir(tmp_path),
        capacity=1,
        idle_timeout_s=0.15,
    )

    async def update(event: BaseModel) -> None:
        pytest.fail(f"Unexpected update: {event}")

    async def run() -> None:
        async with pool.lifetime():
            first = await pool.open(("project", "first"), "test", tmp_path, update)
            assert first.usable
            # Wait until idle eviction tears down the first subprocess.
            for _ in range(50):
                if pool.session(("project", "first")) is None:
                    break
                await asyncio.sleep(0.03)
            assert pool.session(("project", "first")) is None
            assert not first.usable
            # Capacity slot is now free for another session.
            second = await pool.open(("project", "second"), "test", tmp_path, update)
            assert second.usable

    asyncio.run(run())


def test_pool_defers_eviction_while_session_is_busy(tmp_path: Path) -> None:
    config = {
        "agents": [
            {
                "id": "test",
                "name": "Test",
                "command": sys.executable,
                "args": ["-u", "-c", _AGENT],
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    pool = ExternalSessionPool(
        ExternalAgentCatalog.from_state_dir(tmp_path),
        capacity=1,
        idle_timeout_s=0.12,
    )
    busy = True
    pool.set_busy_probe(lambda _key: busy)

    async def update(event: BaseModel) -> None:
        pytest.fail(f"Unexpected update: {event}")

    async def run() -> None:
        nonlocal busy
        async with pool.lifetime():
            session = await pool.open(("project", "first"), "test", tmp_path, update)
            await asyncio.sleep(0.25)
            assert pool.session(("project", "first")) is session
            assert session.usable
            busy = False
            for _ in range(50):
                if pool.session(("project", "first")) is None:
                    break
                await asyncio.sleep(0.03)
            assert pool.session(("project", "first")) is None
            assert not session.usable

    asyncio.run(run())
