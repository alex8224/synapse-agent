"""P1 ACP transport integration tests using the official SDK connection."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import acp
from acp._transport import memory_transport_pair
from acp.helpers import text_block
from acp.schema import ClientCapabilities, Implementation

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.lifecycle import ACPSessionCatalog
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry
from synapse.runtime.sessions.ref import SessionRef
from tests.acp_service_fakes import IsolatedACPSettings

ROOT = Path(__file__).parents[1]


class _FakeWatch:
    """Offline event watch yielding exactly one terminal turn event."""

    def __init__(self, turn_id: str) -> None:
        self._turn_id = turn_id

    async def __aenter__(self) -> _FakeWatch:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        del exc
        return None

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        yield SimpleNamespace(
            thread_id="acp-p1",
            turn_id=self._turn_id,
            sequence=1,
            turn_sequence=1,
            version=1,
            kind="turn_completed",
            payload={"status": "completed", "final_text": ""},
        )


class _FakeRuntimeService:
    """Minimal offline ``AgentRuntimeService`` double for one completed turn.

    ``ACPManagedSession`` is service-based: turns run through
    ``execute_consumer_turn`` (get_session -> watch_events -> submit_turn ->
    terminal event -> get_session).  This fake never touches a model or the
    filesystem and completes synchronously.
    """

    _PROJECT = "acp-p1"

    def __init__(self) -> None:
        self._active_turn: str | None = None

    def _view(self) -> SimpleNamespace:
        return SimpleNamespace(
            project_id=self._PROJECT,
            thread_id="thread",
            status="idle",
            active_turn_id=self._active_turn,
            latest_sequence=0,
            usage=None,
            last_error=None,
        )

    async def get_session(self, query: Any) -> SimpleNamespace:
        del query
        return self._view()

    def watch_events(self, session: SessionRef, **kwargs: Any) -> _FakeWatch:
        del kwargs
        return _FakeWatch(f"turn-{session.thread_id}")

    async def submit_turn(self, command: Any) -> SimpleNamespace:
        turn_id = f"turn-{command.session.thread_id}"
        self._active_turn = turn_id
        return SimpleNamespace(
            command_id=command.command_id,
            session=command.session,
            turn_id=turn_id,
            accepted=True,
        )

    async def open_session(self, command: Any) -> SimpleNamespace:
        del command
        return SimpleNamespace(created=False, view=self._view())

    async def close_session(self, command: Any, **kwargs: Any) -> SimpleNamespace:
        del command, kwargs
        return SimpleNamespace(closed=True, active_turn_id=None, cancellation_requested=False)

    async def cancel_turn(self, command: Any) -> SimpleNamespace:
        self._active_turn = None
        return SimpleNamespace(turn_id=command.expected_turn_id, cancellation_requested=True)


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


def _make_agent(root: Path) -> SynapseACPAgent:
    async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
        service = _FakeRuntimeService()
        return ACPManagedSession(
            descriptor,
            service,  # type: ignore[arg-type]
            SessionRef(project_id=_FakeRuntimeService._PROJECT, thread_id=descriptor.thread_id),
        )

    return SynapseACPAgent(
        registry=ACPSessionRegistry(factory),
        catalog=ACPSessionCatalog(root / "catalog.sqlite"),
        settings_factory=IsolatedACPSettings,
    )


async def _run_sdk_connection(root: Path) -> None:
    agent = _make_agent(root)
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


def test_official_sdk_connection_completes_initialize_and_new_session(tmp_path: Path) -> None:
    asyncio.run(_run_sdk_connection(tmp_path))


def test_prompt_over_service_contract_completes_offline(tmp_path: Path) -> None:
    """Prompt drives ACPManagedSession over the AgentRuntimeService port.

    Regression: ``ACPManagedSession.submit`` runs through
    ``execute_consumer_turn`` (service.get_session -> watch_events ->
    submit_turn -> terminal event).  The injected session must therefore be a
    service-backed double, not the legacy manager/runtime contract; otherwise
    prompt raises an AttributeError that the ACP router reports as an
    "Internal error".
    """

    async def run() -> None:
        agent = _make_agent(tmp_path)
        await agent.initialize(1, client_capabilities=None, client_info=None)
        response = await agent.new_session(cwd=str(ROOT), mcp_servers=[])
        result = await agent.prompt(
            response.session_id, [text_block("hello")]
        )
        assert result.stop_reason == "end_turn"

    asyncio.run(run())


def test_official_sdk_subprocess_helper_runs_injected_agent_session(tmp_path: Path) -> None:
    async def run() -> None:
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (str(ROOT / "src"), str(ROOT / "tests"), str(ROOT))
            ),
            "SYNAPSE_TEST_ACP_ROOT": str(tmp_path),
        }
        child_code = (
            "import asyncio, os; "
            "from pathlib import Path; "
            "from test_acp_p1_transport import _make_agent; "
            "from synapse.acp.server import run_server; "
            "asyncio.run("
            "run_server(_make_agent(Path(os.environ['SYNAPSE_TEST_ACP_ROOT']))))"
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


def test_official_sdk_subprocess_cancel_notification_is_processed(tmp_path: Path) -> None:
    async def run() -> None:
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (str(ROOT / "src"), str(ROOT / "tests"), str(ROOT))
            ),
            "SYNAPSE_TEST_ACP_ROOT": str(tmp_path),
        }
        child_code = (
            "import asyncio, os; "
            "from pathlib import Path; "
            "from test_acp_p1_transport import _make_agent; "
            "from synapse.acp.server import run_server; "
            "asyncio.run("
            "run_server(_make_agent(Path(os.environ['SYNAPSE_TEST_ACP_ROOT']))))"
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
