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


def test_local_service_submit_turn_forwards_attachment_refs_to_external_acp(
    tmp_path: Path,
) -> None:
    import base64

    from synapse.runtime.service.attachments import (
        AppendAttachmentChunkCommand,
        BeginAttachmentCommand,
        FinishAttachmentCommand,
    )
    from synapse.runtime.service.commands import SubmitTurnCommand
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.sessions.manager import RuntimeManager
    from synapse.sessions.transcript_projection import TranscriptProjection

    # Valid 1x1 red PNG
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    expected_b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    agent_script = f'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        result = dict(
            protocolVersion=1,
            agentCapabilities=dict(promptCapabilities=dict(image=True)),
        )
    elif method == "session/new":
        result = dict(sessionId="peer-att")
    elif method == "session/prompt":
        blocks = msg["params"]["prompt"]
        assert len(blocks) == 2, blocks
        assert blocks[0] == dict(type="text", text="check image")
        assert blocks[1]["type"] == "image"
        assert blocks[1]["mimeType"] == "image/png"
        assert blocks[1]["data"] == {expected_b64!r}
        print(json.dumps(dict(jsonrpc="2.0", method="session/update", params=dict(
            sessionId="peer-att",
            update=dict(sessionUpdate="agent_message_chunk",
                        content=dict(type="text", text="image received"))))), flush=True)
        result = dict(stopReason="end_turn")
    else:
        continue
    print(json.dumps(dict(jsonrpc="2.0", id=msg["id"], result=result)), flush=True)
'''
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {
        "agents": [
            {
                "id": "test",
                "name": "Test agent",
                "command": sys.executable,
                "args": ["-u", "-c", agent_script],
                "permission_mode": "manual",
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    pool = ExternalSessionPool(catalog)
    external_service = DaemonExternalSessions(ExternalRuntime(pool))
    ref = SessionRef(project_id="project", thread_id="external-img")
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=ref.project_id,
    )
    local_service = LocalAgentRuntimeService(
        lambda project: manager if project == ref.project_id else None,
        external_sessions=external_service,
    )

    async def run() -> None:
        try:
            await local_service.create_session(
                CreateSessionCommand(
                    project_id=ref.project_id,
                    thread_id=ref.thread_id,
                    external_agent_id="test",
                )
            )
            begin = await local_service.begin_attachment(
                BeginAttachmentCommand(ref, len(png_bytes), "image/png", "shot.png")
            )
            await local_service.append_attachment_chunk(
                AppendAttachmentChunkCommand(begin.ref, 0, expected_b64)
            )
            await local_service.finish_attachment(
                FinishAttachmentCommand(begin.ref, len(png_bytes), "image/png")
            )

            receipt = await local_service.submit_turn(
                SubmitTurnCommand(
                    ref,
                    "check image",
                    attachment_refs=(begin.ref.attachment_id,),
                )
            )
            assert receipt.accepted
            # Wait for the background external turn task to settle.
            if local_service._external_turns:
                await asyncio.gather(*list(local_service._external_turns))

            broker = external_service.broker(ref)
            assert broker is not None
            window = broker.read_after(0)
            kinds = [env.event.kind.value for env in window.events]
            assert "answer_delta" in kinds
            assert kinds[-1] == "turn_completed"

            projection = TranscriptProjection(tmp_path / "transcript.sqlite")
            try:
                page = projection.load_tail(ref.thread_id, turns=5)
            finally:
                projection.close()
            assert page.events[0].kind == "user"
            assert page.events[0].text == "check image"
            assert len(page.events[0].attachments) == 1
            assert page.events[0].attachments[0]["attachment_id"] == begin.ref.attachment_id
            assert page.events[0].attachments[0]["mime"] == "image/png"
            assert page.events[-1].kind == "answer"
            assert page.events[-1].text == "image received"
            assert expected_b64.encode("ascii") not in (tmp_path / "transcript.sqlite").read_bytes()
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())


def test_external_acp_steer_concurrent_prompt_merges_into_active_turn(
    tmp_path: Path,
) -> None:
    """agy-style peer accepts concurrent session/prompt and ends the first early."""
    import pytest

    from synapse.runtime.service.commands import SteerTurnCommand, SubmitTurnCommand
    from synapse.runtime.service.errors import NoActiveTurnError, TurnMismatchError
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.sessions.manager import RuntimeManager
    from synapse.sessions.transcript_projection import TranscriptProjection

    agent_script = '''
import json, sys, threading, queue

incoming = queue.Queue()

def reader():
    for line in sys.stdin:
        incoming.put(json.loads(line))
    incoming.put(None)

threading.Thread(target=reader, daemon=True).start()

def send(obj):
    print(json.dumps(obj), flush=True)

def update(text):
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "peer-concurrent",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    })

first_id = None
while True:
    msg = incoming.get()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "peer-concurrent"}})
    elif method == "session/prompt":
        text = msg["params"]["prompt"][0]["text"]
        if first_id is None:
            first_id = msg["id"]
            update(f"part1:{text}")
        else:
            # Mirror agy acp: first prompt resolves end_turn early, second carries on.
            send({"jsonrpc": "2.0", "id": first_id, "result": {"stopReason": "end_turn"}})
            update(f"part2:{text}")
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
            first_id = None
'''
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {
        "agents": [
            {
                "id": "agy-mock",
                "name": "Agy Mock",
                "command": sys.executable,
                "args": ["-u", "-c", agent_script],
                "permission_mode": "manual",
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    pool = ExternalSessionPool(catalog)
    external_service = DaemonExternalSessions(ExternalRuntime(pool))
    ref = SessionRef(project_id="project", thread_id="external-steer-concurrent")
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=ref.project_id,
    )
    local_service = LocalAgentRuntimeService(
        lambda project: manager if project == ref.project_id else None,
        external_sessions=external_service,
    )

    async def run() -> None:
        try:
            await local_service.create_session(
                CreateSessionCommand(
                    project_id=ref.project_id,
                    thread_id=ref.thread_id,
                    external_agent_id="agy-mock",
                )
            )
            # Before any turn is active, steer_turn rejects with NoActiveTurnError.
            with pytest.raises(NoActiveTurnError):
                await local_service.steer_turn(
                    SteerTurnCommand(ref, expected_turn_id="nonexistent", text="early")
                )

            receipt = await local_service.submit_turn(SubmitTurnCommand(ref, "initial"))
            broker = external_service.broker(ref)
            assert broker is not None

            # Wait until part1 delta arrives so we know prompt #1 is live.
            for _ in range(100):
                deltas = [
                    env.event.payload.text
                    for env in broker.read_after(0).events
                    if env.event.kind.value == "answer_delta"
                ]
                if "part1:initial" in "".join(deltas):
                    break
                await asyncio.sleep(0.02)
            assert "part1:initial" in "".join(deltas)

            # Wrong turn id raises TurnMismatchError.
            with pytest.raises(TurnMismatchError):
                await local_service.steer_turn(
                    SteerTurnCommand(ref, expected_turn_id="wrong-turn", text="nope")
                )

            # Steer the active turn; returns immediately without waiting for completion.
            steered = await local_service.steer_turn(
                SteerTurnCommand(ref, expected_turn_id=receipt.turn_id, text="pivot now")
            )
            assert steered.accepted is True
            assert steered.turn_id == receipt.turn_id
            assert steered.pending_count == 1

            if local_service._external_turns:
                await asyncio.gather(*list(local_service._external_turns))

            events = [env.event for env in broker.read_after(0).events]
            terminal_kinds = [
                e.kind.value
                for e in events
                if e.kind.value in {"turn_completed", "turn_cancelled", "turn_failed"}
            ]
            assert terminal_kinds == ["turn_completed"]
            combined_answer = "".join(
                e.payload.text for e in events if e.kind.value == "answer_delta"
            )
            assert combined_answer == "part1:initialpart2:pivot now"

            projection = TranscriptProjection(tmp_path / "transcript.sqlite")
            try:
                page = projection.load_tail(ref.thread_id, turns=5)
            finally:
                projection.close()
            assert [(e.kind, e.text) for e in page.events] == [
                ("user", "initial"),
                ("answer", "part1:initial"),
                ("user", "pivot now"),
                ("answer", "part2:pivot now"),
            ]
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())


def test_external_acp_steer_falls_back_to_client_queue_on_busy(tmp_path: Path) -> None:
    """Strict ACP peer rejecting concurrent session/prompt triggers queued delivery."""
    from synapse.runtime.service.commands import SteerTurnCommand, SubmitTurnCommand
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.sessions.manager import RuntimeManager
    from synapse.sessions.transcript_projection import TranscriptProjection

    agent_script = '''
import json, sys, threading, time, queue

incoming = queue.Queue()
busy = False
release = threading.Event()

def reader():
    for line in sys.stdin:
        incoming.put(json.loads(line))
    incoming.put(None)

threading.Thread(target=reader, daemon=True).start()

def send(obj):
    print(json.dumps(obj), flush=True)

def update(text):
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "peer-strict",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    })

def finish_first(req_id, text):
    global busy
    release.wait(timeout=5)
    time.sleep(0.15)
    update(f"done:{text}")
    send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
    busy = False

while True:
    msg = incoming.get()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "peer-strict"}})
    elif method == "session/prompt":
        text = msg["params"]["prompt"][0]["text"]
        if busy:
            send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32000, "message": "session is busy"},
            })
            # Let the first prompt complete after rejecting the concurrent steer.
            release.set()
        elif text == "first":
            busy = True
            update("start:first;")
            threading.Thread(target=finish_first, args=(msg["id"], text), daemon=True).start()
        else:
            update(f"seq:{text}")
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
'''
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {
        "agents": [
            {
                "id": "strict-mock",
                "name": "Strict Mock",
                "command": sys.executable,
                "args": ["-u", "-c", agent_script],
                "permission_mode": "manual",
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    pool = ExternalSessionPool(catalog)
    external_service = DaemonExternalSessions(ExternalRuntime(pool))
    ref = SessionRef(project_id="project", thread_id="external-steer-queue")
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=ref.project_id,
    )
    local_service = LocalAgentRuntimeService(
        lambda project: manager if project == ref.project_id else None,
        external_sessions=external_service,
    )

    async def run() -> None:
        try:
            await local_service.create_session(
                CreateSessionCommand(
                    project_id=ref.project_id,
                    thread_id=ref.thread_id,
                    external_agent_id="strict-mock",
                )
            )
            receipt = await local_service.submit_turn(SubmitTurnCommand(ref, "first"))
            broker = external_service.broker(ref)
            assert broker is not None

            for _ in range(100):
                deltas = [
                    env.event.payload.text
                    for env in broker.read_after(0).events
                    if env.event.kind.value == "answer_delta"
                ]
                if "start:first;" in "".join(deltas):
                    break
                await asyncio.sleep(0.02)

            s1 = await local_service.steer_turn(
                SteerTurnCommand(ref, expected_turn_id=receipt.turn_id, text="queued-1")
            )
            assert s1.accepted is True
            s2 = await local_service.steer_turn(
                SteerTurnCommand(ref, expected_turn_id=receipt.turn_id, text="queued-2")
            )
            assert s2.accepted is True

            if local_service._external_turns:
                await asyncio.gather(*list(local_service._external_turns))

            events = [env.event for env in broker.read_after(0).events]
            terminal_kinds = [
                e.kind.value
                for e in events
                if e.kind.value in {"turn_completed", "turn_cancelled", "turn_failed"}
            ]
            assert terminal_kinds == ["turn_completed"]
            combined_answer = "".join(
                e.payload.text for e in events if e.kind.value == "answer_delta"
            )
            assert combined_answer == "start:first;done:firstseq:queued-1seq:queued-2"

            projection = TranscriptProjection(tmp_path / "transcript.sqlite")
            try:
                page = projection.load_tail(ref.thread_id, turns=5)
            finally:
                projection.close()
            assert [(e.kind, e.text) for e in page.events] == [
                ("user", "first"),
                ("answer", "start:first;done:first"),
                ("user", "queued-1"),
                ("answer", "seq:queued-1"),
                ("user", "queued-2"),
                ("answer", "seq:queued-2"),
            ]
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())


def test_external_acp_cancel_clears_queued_steers(tmp_path: Path) -> None:
    """Cancelling an active turn drops queued steers instead of running them after cancel."""
    from synapse.runtime.service.commands import (
        CancelTurnCommand,
        SteerTurnCommand,
        SubmitTurnCommand,
    )
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.sessions.manager import RuntimeManager

    agent_script = '''
import json, sys, threading, queue

incoming = queue.Queue()
active_id = None

def reader():
    for line in sys.stdin:
        incoming.put(json.loads(line))
    incoming.put(None)

threading.Thread(target=reader, daemon=True).start()

def send(obj):
    print(json.dumps(obj), flush=True)

def update(text):
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "peer-cancel",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    })

while True:
    msg = incoming.get()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "peer-cancel"}})
    elif method == "session/prompt":
        text = msg["params"]["prompt"][0]["text"]
        if active_id is not None:
            send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32000, "message": "busy"},
            })
        else:
            active_id = msg["id"]
            update(f"running:{text}")
    elif method == "session/cancel":
        if active_id is not None:
            send({"jsonrpc": "2.0", "id": active_id, "result": {"stopReason": "cancelled"}})
            active_id = None
'''
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {
        "agents": [
            {
                "id": "cancel-mock",
                "name": "Cancel Mock",
                "command": sys.executable,
                "args": ["-u", "-c", agent_script],
                "permission_mode": "manual",
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    pool = ExternalSessionPool(catalog)
    external_service = DaemonExternalSessions(ExternalRuntime(pool))
    ref = SessionRef(project_id="project", thread_id="external-steer-cancel")
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=ref.project_id,
    )
    local_service = LocalAgentRuntimeService(
        lambda project: manager if project == ref.project_id else None,
        external_sessions=external_service,
    )

    async def run() -> None:
        try:
            await local_service.create_session(
                CreateSessionCommand(
                    project_id=ref.project_id,
                    thread_id=ref.thread_id,
                    external_agent_id="cancel-mock",
                )
            )
            receipt = await local_service.submit_turn(SubmitTurnCommand(ref, "long-task"))
            broker = external_service.broker(ref)
            assert broker is not None

            for _ in range(100):
                deltas = [
                    env.event.payload.text
                    for env in broker.read_after(0).events
                    if env.event.kind.value == "answer_delta"
                ]
                if "running:long-task" in "".join(deltas):
                    break
                await asyncio.sleep(0.02)

            await local_service.steer_turn(
                SteerTurnCommand(ref, expected_turn_id=receipt.turn_id, text="should-not-run")
            )
            # Give the busy rejection a moment to queue the steer.
            await asyncio.sleep(0.05)

            cancelled = await local_service.cancel_turn(
                CancelTurnCommand(ref, expected_turn_id=receipt.turn_id)
            )
            assert cancelled.cancellation_requested is True

            if local_service._external_turns:
                await asyncio.gather(*list(local_service._external_turns))

            events = [env.event for env in broker.read_after(0).events]
            terminal_kinds = [
                e.kind.value
                for e in events
                if e.kind.value in {"turn_completed", "turn_cancelled", "turn_failed"}
            ]
            assert terminal_kinds == ["turn_cancelled"]
            combined_answer = "".join(
                e.payload.text for e in events if e.kind.value == "answer_delta"
            )
            assert "should-not-run" not in combined_answer
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())


def test_open_session_rejects_external_with_wire_service_code_and_reconciles(
    tmp_path: Path,
) -> None:
    import pytest

    from synapse.runtime.service.commands import OpenSessionCommand, SubmitTurnCommand
    from synapse.runtime.service.errors import ExternalSessionError, InvalidRequestError
    from synapse.runtime.service.external_commands import InitializeExternalSessionCommand
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.service.recovery import ReconcileSessionQuery
    from synapse.runtime.sessions.manager import RuntimeManager
    from synapse.runtime.transport.protocol import service_error

    settings, external_service, _metadata, ref = _setup(tmp_path)
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=ref.project_id,
    )
    local_service = LocalAgentRuntimeService(
        lambda project: manager if project == ref.project_id else None,
        external_sessions=external_service,
    )

    async def run() -> None:
        try:
            await local_service.create_session(
                CreateSessionCommand(
                    project_id=ref.project_id,
                    thread_id=ref.thread_id,
                    external_agent_id="test",
                )
            )
            with pytest.raises(ExternalSessionError) as exc_info:
                await local_service.open_session(OpenSessionCommand(ref))
            assert isinstance(exc_info.value, InvalidRequestError)
            assert exc_info.value.code == "external_session"
            code, wire_message, service_code = service_error(exc_info.value)
            assert (code, wire_message, service_code) == (
                -32000,
                "runtime service error",
                "external_session",
            )

            init_result = await local_service.initialize_external_session(
                InitializeExternalSessionCommand(ref)
            )
            assert init_result.agent_id == "test"
            assert init_result.remote_session_id == "peer"

            await local_service.submit_turn(SubmitTurnCommand(ref, "hello"))
            if local_service._external_turns:
                await asyncio.gather(*list(local_service._external_turns))

            snapshot = await local_service.reconcile_session(ReconcileSessionQuery(ref))
            assert snapshot.live_latest_sequence > 0
            assert snapshot.active_turn_id is None
            assert snapshot.history_available is True
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())
