"""P1 ACP transport integration tests using the official SDK connection."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import acp
from acp._transport import memory_transport_pair
from acp.helpers import text_block
from acp.schema import ClientCapabilities, Implementation

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry
from synapse.runtime.agent_loop import TurnResult, TurnStatus

ROOT = Path(__file__).parents[1]


class _Subscription:
    def close(self) -> None:
        return None


class _Runtime:
    def subscribe(self, callback: Any, *, after_sequence: int = 0) -> _Subscription:
        del callback, after_sequence
        return _Subscription()

    async def wait_for_settlement(self, handle: Any) -> None:
        del handle


class _Manager:
    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime

    async def submit(self, thread_id: str, message: Any) -> Any:
        del thread_id, message
        future: asyncio.Future[TurnResult] = asyncio.get_running_loop().create_future()
        future.set_result(
            TurnResult(
                turn_id="turn-transport",
                thread_id="sess-transport",
                status=TurnStatus.COMPLETED,
            )
        )

        class Handle:
            def __init__(self, value: asyncio.Future[TurnResult]) -> None:
                self.future = value

        return Handle(future)

    def cancel(self, thread_id: str, reason: str) -> bool:
        del thread_id, reason
        return True

    async def close_session(self, thread_id: str, *, cancel_active: bool) -> None:
        del thread_id, cancel_active

    async def shutdown(self) -> None:
        return None


class _Client:
    async def request_permission(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"permission must not be called in P1: {args!r} {kwargs!r}")

    async def session_update(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def write_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"filesystem must not be called in P1: {args!r} {kwargs!r}")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"filesystem must not be called in P1: {args!r} {kwargs!r}")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"terminal must not be called in P1: {args!r} {kwargs!r}")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"terminal must not be called in P1: {args!r} {kwargs!r}")

    async def release_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"terminal must not be called in P1: {args!r} {kwargs!r}")

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"terminal must not be called in P1: {args!r} {kwargs!r}")

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"terminal must not be called in P1: {args!r} {kwargs!r}")

    async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"elicitation must not be called in P1: {args!r} {kwargs!r}")

    async def complete_elicitation(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"elicitation must not be called in P1: {args!r} {kwargs!r}")


def _make_agent() -> SynapseACPAgent:
    async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
        runtime = _Runtime()
        return ACPManagedSession(descriptor, _Manager(runtime), runtime)  # type: ignore[arg-type]

    return SynapseACPAgent(registry=ACPSessionRegistry(factory))


async def _run_sdk_connection() -> None:
    agent = _make_agent()
    client_transport, agent_transport = memory_transport_pair()
    server_task = asyncio.create_task(acp.run_agent(agent, agent_transport))
    client_connection = acp.connect_to_agent(_Client(), client_transport)
    try:
        initialized = await asyncio.wait_for(
            client_connection.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="test-client", version="0.1"),
            ),
            timeout=10,
        )
        assert initialized.protocol_version == 1
        session = await asyncio.wait_for(
            client_connection.new_session(cwd=str(ROOT), mcp_servers=[]),
            timeout=10,
        )
        assert session.session_id.startswith("sess_")
    finally:
        await client_connection.close()
        await asyncio.wait_for(server_task, timeout=10)


def test_official_sdk_connection_completes_initialize_and_new_session() -> None:
    asyncio.run(_run_sdk_connection())


def test_official_sdk_subprocess_helper_runs_injected_agent_session() -> None:
    async def run() -> None:
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests"))),
        }
        child_code = (
            "import asyncio; "
            "from test_acp_p1_transport import _make_agent; "
            "from synapse.acp.server import run_server; "
            "asyncio.run(run_server(_make_agent()))"
        )
        async with acp.spawn_agent_process(
            _Client(),
            sys.executable,
            "-c",
            child_code,
            env=env,
            cwd=ROOT,
        ) as (connection, process):
            initialized = await asyncio.wait_for(
                connection.initialize(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="subprocess-client", version="0.1"),
                ),
                timeout=10,
            )
            assert initialized.protocol_version == 1
            session = await asyncio.wait_for(
                connection.new_session(cwd=str(ROOT), mcp_servers=[]),
                timeout=10,
            )
            response = await asyncio.wait_for(
                connection.prompt(session.session_id, [text_block("hello")]),
                timeout=10,
            )
            assert response.stop_reason == "end_turn"
            assert process.returncode is None

    asyncio.run(run())


def test_official_sdk_subprocess_cancel_notification_is_processed() -> None:
    async def run() -> None:
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests"))),
        }
        child_code = (
            "import asyncio; "
            "from test_acp_p1_transport import _make_agent; "
            "from synapse.acp.server import run_server; "
            "asyncio.run(run_server(_make_agent()))"
        )
        async with acp.spawn_agent_process(
            _Client(),
            sys.executable,
            "-c",
            child_code,
            env=env,
            cwd=ROOT,
        ) as (connection, _process):
            await asyncio.wait_for(
                connection.initialize(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="cancel-client", version="0.1"),
                ),
                timeout=10,
            )
            session = await asyncio.wait_for(
                connection.new_session(cwd=str(ROOT), mcp_servers=[]),
                timeout=10,
            )
            await asyncio.wait_for(connection.cancel(session.session_id), timeout=10)

    asyncio.run(run())


def test_stdio_entry_emits_only_json_rpc_on_initialize() -> None:
    async def run() -> tuple[int, bytes, bytes]:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "synapse.acp.server",
            cwd=ROOT,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
            b'{"protocolVersion":1,"clientCapabilities":{}}}\n'
        )
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        stderr = await process.stderr.read() if process.stderr is not None else b""
        return process.returncode or 0, line, stderr

    returncode, line, _stderr = asyncio.run(run())
    assert returncode == 0
    message = json.loads(line)
    assert message["jsonrpc"] == "2.0"
    assert message["id"] == 1
    assert message["result"]["protocolVersion"] == 1


def test_stdio_ignores_unknown_notification_without_response() -> None:
    async def run() -> tuple[int, list[bytes], bytes]:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "synapse.acp.server",
            cwd=ROOT,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
            b'{"protocolVersion":1,"clientCapabilities":{}}}\n'
        )
        await process.stdin.drain()
        first = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        # An unknown notification must be ignored without emitting a response.
        process.stdin.write(
            b'{"jsonrpc":"2.0","method":"unknown/notif","params":{}}\n'
        )
        # A follow-up request must still receive its own correlated response.
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":2,"method":"session/list","params":{}}\n'
        )
        await process.stdin.drain()
        second = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        stderr = await process.stderr.read() if process.stderr is not None else b""
        return process.returncode or 0, [first, second], stderr

    returncode, lines, _stderr = asyncio.run(run())
    assert returncode == 0
    assert json.loads(lines[0])["id"] == 1
    second = json.loads(lines[1])
    assert second["id"] == 2
    assert "result" in second
