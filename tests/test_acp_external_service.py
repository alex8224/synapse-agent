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


def test_deliver_turn_returns_the_peer_answer_for_collaboration(tmp_path: Path) -> None:
    """A collaboration delivery reads the peer's own answer as its reply."""
    settings, service, metadata, ref = _setup(tmp_path)

    async def run() -> None:
        await service.create(
            metadata, CreateSessionCommand(project_id="project", thread_id="external"), "test"
        )
        stop_reason, answer = await service.deliver_turn(
            ref, settings, tmp_path, turn_id="delivery", text="协作交办"
        )
        assert stop_reason == "end_turn"
        assert answer == "hello"
        # The delivery turn is a normal turn for anyone watching the session.
        broker = service.broker(ref)
        assert broker is not None
        kinds = [envelope.event.kind.value for envelope in broker.read_after(0).events]
        assert "turn_completed" in kinds
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


def test_external_acp_captures_unsolicited_background_wakeup_updates(
    tmp_path: Path,
) -> None:
    """Autonomous updates pushed after end_turn (e.g. agy background wakeup) are not dropped."""
    from synapse.runtime.service.commands import SubmitTurnCommand
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.sessions.manager import RuntimeManager
    from synapse.sessions.transcript_projection import TranscriptProjection

    agent_script = '''
import json, sys, threading, time

def send(obj):
    print(json.dumps(obj), flush=True)

def update(payload):
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "peer-bg", "update": payload},
    })

def background_wakeup():
    time.sleep(0.08)
    update({
        "sessionUpdate": "tool_call",
        "toolCallId": "bg-tool-1",
        "title": "pytest",
        "status": "in_progress",
    })
    # Stay silent longer than idle_s (0.05s) while tool is running to verify
    # the unsolicited turn does not settle prematurely mid-tool.
    time.sleep(0.12)
    update({
        "sessionUpdate": "tool_call_update",
        "toolCallId": "bg-tool-1",
        "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": "171 passed"}}],
    })
    update({
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "All tests passed!"},
    })

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "peer-bg"}})
    elif method == "session/prompt":
        update({
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Running tests in background..."},
        })
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
        threading.Thread(target=background_wakeup, daemon=True).start()
'''
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {
        "agents": [
            {
                "id": "bg-mock",
                "name": "BG Mock",
                "command": sys.executable,
                "args": ["-u", "-c", agent_script],
                "permission_mode": "manual",
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    pool = ExternalSessionPool(catalog)
    runtime = ExternalRuntime(pool)
    external_service = DaemonExternalSessions(runtime)
    ref = SessionRef(project_id="project", thread_id="external-bg-wakeup")
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
                    external_agent_id="bg-mock",
                )
            )
            receipt = await local_service.submit_turn(SubmitTurnCommand(ref, "run tests"))
            if local_service._external_turns:
                await asyncio.gather(*list(local_service._external_turns))

            # Speed up idle debounce for deterministic unit testing.
            bridge = runtime.bridges[(ref.project_id, ref.thread_id)]
            bridge._unsolicited_idle_s = 0.05

            broker = external_service.broker(ref)
            assert broker is not None

            # Wait for the unsolicited continuation turn to stream and auto-settle.
            for _ in range(100):
                events = [env.event for env in broker.read_after(0).events]
                completed = [e for e in events if e.kind.value == "turn_completed"]
                if len(completed) >= 2:
                    break
                await asyncio.sleep(0.03)

            events = [env.event for env in broker.read_after(0).events]
            completed = [e for e in events if e.kind.value == "turn_completed"]
            assert len(completed) == 2
            assert completed[0].turn_id == receipt.turn_id
            unsolicited_turn_id = completed[1].turn_id
            assert unsolicited_turn_id != receipt.turn_id

            unsolicited_events = [e for e in events if e.turn_id == unsolicited_turn_id]
            unsolicited_kinds = [e.kind.value for e in unsolicited_events]
            assert unsolicited_kinds == [
                "activity_started",
                "tool_started",
                "tool_finished",
                "answer_delta",
                "turn_completed",
            ]

            projection = TranscriptProjection(tmp_path / "transcript.sqlite")
            try:
                page = projection.load_tail(ref.thread_id, turns=10)
            finally:
                projection.close()
            # Unsolicited continuation must not insert a fake empty user bubble.
            assert [e.kind for e in page.events] == ["user", "answer", "tools", "answer"]
            assert page.events[0].text == "run tests"
            assert page.events[1].text == "Running tests in background..."
            assert page.events[2].tool_results[0]["content"] == "171 passed"
            assert page.events[3].text == "All tests passed!"
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())


def test_external_acp_interactive_approval_roundtrip(tmp_path: Path) -> None:
    """Manual-mode permission requests bridge through APPROVAL_REQUIRED and resume_turn."""
    from synapse.runtime.service.commands import (
        ApprovalDecision,
        ResumeTurnCommand,
        SubmitTurnCommand,
    )
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.service.queries import PendingApprovalQuery
    from synapse.runtime.sessions.manager import RuntimeManager

    agent_script = '''
import json, sys

pending_prompt_id = None
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}}
        print(json.dumps(resp), flush=True)
    elif method == "session/new":
        resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "peer-perm"}}
        print(json.dumps(resp), flush=True)
    elif method == "session/prompt":
        pending_prompt_id = msg["id"]
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "session/request_permission",
            "params": {
                "sessionId": "peer-perm",
                "toolCall": {"toolCallId": "tc-1", "title": "Run shell", "kind": "execute"},
                "options": [
                    {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }), flush=True)
    elif msg.get("id") == 99:
        outcome = msg["result"]["outcome"]
        approved = outcome.get("optionId") == "once"
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "peer-perm",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "approved-ok" if approved else "denied"},
                },
            },
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": pending_prompt_id,
            "result": {
                "stopReason": "end_turn",
                "usage": {
                    "totalTokens": 165,
                    "inputTokens": 120,
                    "outputTokens": 45,
                    "cachedReadTokens": 30,
                },
            },
        }), flush=True)
'''
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {
        "agents": [
            {
                "id": "perm-mock",
                "name": "Perm Mock",
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
    ref = SessionRef(project_id="project", thread_id="external-perm")
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
                    external_agent_id="perm-mock",
                )
            )
            receipt = await local_service.submit_turn(SubmitTurnCommand(ref, "run it"))
            broker = external_service.broker(ref)
            assert broker is not None

            for _ in range(100):
                kinds = [env.event.kind.value for env in broker.read_after(0).events]
                if "approval_required" in kinds:
                    break
                await asyncio.sleep(0.02)
            assert "approval_required" in kinds

            pending = await local_service.pending_approval(
                PendingApprovalQuery(ref, expected_turn_id=receipt.turn_id)
            )
            assert pending.turn_id == receipt.turn_id
            assert pending.actions[0].name == "Run shell"

            resumed = await local_service.resume_turn(
                ResumeTurnCommand(
                    ref,
                    expected_turn_id=receipt.turn_id,
                    decisions=(ApprovalDecision("allow_once"),),
                )
            )
            assert resumed.turn_id == receipt.turn_id

            if local_service._external_turns:
                await asyncio.gather(*list(local_service._external_turns))

            events = [env.event for env in broker.read_after(0).events]
            answer = "".join(e.payload.text for e in events if e.kind.value == "answer_delta")
            assert answer == "approved-ok"
            usage_events = [e for e in events if e.kind.value == "usage_updated"]
            assert len(usage_events) == 1
            assert usage_events[0].payload.turn_input == 120
            assert usage_events[0].payload.turn_output == 45
            assert usage_events[0].payload.turn_cache == 30
            terminal = [e for e in events if e.kind.value == "turn_completed"][-1]
            assert terminal.payload.input_tokens == 120
            assert terminal.payload.output_tokens == 45
            assert terminal.payload.cache_tokens == 30
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())


def test_external_acp_close_fork_legacy_modes_and_stderr_redaction(tmp_path: Path) -> None:
    """Covers close_session, fork guard, legacy modes, available_commands, and stderr redaction."""
    import pytest

    from synapse.runtime.service.commands import CloseSessionCommand
    from synapse.runtime.service.errors import InvalidRequestError
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.service.session_management import ForkSessionCommand
    from synapse.runtime.sessions.manager import RuntimeManager

    agent_script = '''
import json, sys

print("auth Bearer supersecrettoken12345 api_key=mysecretkey99", file=sys.stderr, flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}}
        print(json.dumps(resp), flush=True)
    elif method == "session/new":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {
                "sessionId": "peer-modes",
                "modes": {
                    "currentModeId": "code",
                    "availableModes": [
                        {"id": "code", "name": "Code"},
                        {"id": "architect", "name": "Architect"},
                    ],
                },
            },
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "peer-modes",
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [{"name": "review", "description": "Review diff"}],
                },
            },
        }), flush=True)
    elif method == "session/set_mode":
        assert msg["params"]["modeId"] == "architect"
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}), flush=True)
'''
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {
        "agents": [
            {
                "id": "modes-mock",
                "name": "Modes Mock",
                "command": sys.executable,
                "args": ["-u", "-c", agent_script],
                "permission_mode": "manual",
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    pool = ExternalSessionPool(catalog)
    runtime = ExternalRuntime(pool)
    external_service = DaemonExternalSessions(runtime)
    ref = SessionRef(project_id="project", thread_id="external-modes")
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
                    external_agent_id="modes-mock",
                )
            )
            view = await external_service.initialize(ref, settings, tmp_path)
            assert len(view.options) == 1
            assert view.options[0].id == "mode"
            assert view.options[0].current_value == "code"

            updated = await external_service.set_config(
                ref, settings, tmp_path, "mode", "architect"
            )
            assert updated.options[0].current_value == "architect"

            live_session = pool.session((ref.project_id, ref.thread_id))
            assert live_session is not None
            for _ in range(50):
                if live_session.stderr_tail and live_session.available_commands:
                    break
                await asyncio.sleep(0.02)
            assert any("[REDACTED]" in line for line in live_session.stderr_tail)
            assert all("supersecrettoken12345" not in line for line in live_session.stderr_tail)
            assert all("mysecretkey99" not in line for line in live_session.stderr_tail)
            assert [c.name for c in live_session.available_commands] == ["review"]

            # Forking an external ACP session is explicitly rejected.
            with pytest.raises(InvalidRequestError, match="External ACP"):
                await local_service.fork_session(ForkSessionCommand(source=ref))

            # Closing an external ACP session terminates its live subprocess.
            closed = await local_service.close_session(CloseSessionCommand(ref))
            assert closed.closed is True
            assert pool.session((ref.project_id, ref.thread_id)) is None
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


def test_local_service_reload_mcp_toggles_and_reloads_external_acp_session(
    tmp_path: Path,
) -> None:
    from synapse.runtime.service.commands import ReloadMcpCommand
    from synapse.runtime.service.external_commands import InitializeExternalSessionCommand
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.service.mcp_management import ListMcpServersQuery
    from synapse.runtime.sessions.manager import RuntimeManager

    log_file = tmp_path / "acp_mcp_log.jsonl"
    agent_script = f'''
import json, sys
log_path = {str(log_file)!r}
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        result = dict(protocolVersion=1, agentCapabilities=dict(loadSession=True))
    elif method == "session/new":
        names = [srv["name"] for srv in msg["params"].get("mcpServers", [])]
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{"op": "new", "mcp": names}}) + "\\n")
        result = dict(sessionId="peer-mcp-1")
    elif method == "session/load":
        names = [srv["name"] for srv in msg["params"].get("mcpServers", [])]
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{"op": "load", "mcp": names}}) + "\\n")
        result = dict()
    else:
        continue
    print(json.dumps(dict(jsonrpc="2.0", id=msg["id"], result=result)), flush=True)
'''
    mcp_file = tmp_path / "mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "cdp",
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-m", "cdp_mcp"],
                        "enabled": True,
                    },
                    {
                        "name": "cua",
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-m", "cua_mcp"],
                        "enabled": False,
                    },
                    {
                        "name": "stitch",
                        "transport": "streamable_http",
                        "url": "https://example.com/mcp",
                        "enabled": False,
                    },
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
        mcp_config_path=mcp_file,
        mcp_servers_json=None,
        enable_mcp=True,
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
    ref = SessionRef(project_id="project", thread_id="external-mcp-reload")
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
            await local_service.initialize_external_session(InitializeExternalSessionCommand(ref))
            broker_before = external_service.broker(ref)
            assert broker_before is not None

            # 1) Inspecting MCP state via reload_mcp(server=None) must NOT restart the process.
            initial = await local_service.reload_mcp(ReloadMcpCommand(ref))
            assert initial.attached is True
            assert initial.active_servers == ("cdp",)
            assert initial.warnings == ()
            by_name = {srv.name: srv for srv in initial.servers}
            assert by_name["cdp"].enabled is True and by_name["cdp"].attached is True
            assert by_name["cua"].enabled is False and by_name["cua"].attached is False
            log_entries = [
                json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()
            ]
            assert log_entries == [{"op": "new", "mcp": ["cdp"]}]

            # 2) Enabling a stdio server persists mcp.json and reloads via session/load
            #    while keeping the event broker intact.
            enabled = await local_service.reload_mcp(
                ReloadMcpCommand(ref, server="cua", enabled=True)
            )
            assert enabled.enabled is True
            assert enabled.attached is True
            assert enabled.active_servers == ("cdp", "cua")
            assert external_service.broker(ref) is broker_before
            listed = await local_service.list_mcp_servers(ListMcpServersQuery(session=ref))
            listed_by_name = {srv.name: srv for srv in listed.servers}
            assert listed_by_name["cua"].enabled is True and listed_by_name["cua"].attached is True

            # 3) Disabling cdp persists mcp.json and drops it on session/load.
            disabled = await local_service.reload_mcp(
                ReloadMcpCommand(ref, server="cdp", enabled=False)
            )
            assert disabled.enabled is False
            assert disabled.attached is False
            assert disabled.active_servers == ("cua",)
            disk = json.loads(mcp_file.read_text(encoding="utf-8"))
            disk_enabled = {srv["name"]: srv["enabled"] for srv in disk["servers"]}
            assert disk_enabled == {"cdp": False, "cua": True, "stitch": False}

            # 4) Enabling an HTTP server when peer does not advertise http MCP persists the flag
            #    and bridges it via stdio on session/load with no capability warning.
            http_res = await local_service.reload_mcp(
                ReloadMcpCommand(ref, server="stitch", enabled=True)
            )
            assert http_res.enabled is True
            assert http_res.attached is True
            assert http_res.active_servers == ("cua", "stitch")
            assert http_res.warnings == ()

            log_entries = [
                json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()
            ]
            assert log_entries == [
                {"op": "new", "mcp": ["cdp"]},
                {"op": "load", "mcp": ["cdp", "cua"]},
                {"op": "load", "mcp": ["cua"]},
                {"op": "load", "mcp": ["cua", "stitch"]},
            ]
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())


def test_local_service_reload_mcp_preserves_live_session_without_load_session(
    tmp_path: Path,
) -> None:
    from synapse.runtime.service.commands import ReloadMcpCommand
    from synapse.runtime.service.external_commands import InitializeExternalSessionCommand
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.sessions.manager import RuntimeManager

    mcp_file = tmp_path / "mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "cdp",
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-m", "cdp_mcp"],
                        "enabled": True,
                    },
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        sessions_path=tmp_path / "sessions.sqlite",
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
        mcp_config_path=mcp_file,
        mcp_servers_json=None,
        enable_mcp=True,
    )
    config = {
        "agents": [
            {
                "id": "test",
                "name": "Test agent",
                "command": sys.executable,
                "args": ["-u", "-c", _AGENT],
                "permission_mode": "manual",
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    pool = ExternalSessionPool(catalog)
    external_service = DaemonExternalSessions(ExternalRuntime(pool))
    ref = SessionRef(project_id="project", thread_id="external-mcp-no-load")
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
            await local_service.initialize_external_session(InitializeExternalSessionCommand(ref))
            live_before = pool.session((ref.project_id, ref.thread_id))
            assert live_before is not None

            result = await local_service.reload_mcp(
                ReloadMcpCommand(ref, server="cdp", enabled=False)
            )
            # Config persisted, live session preserved, truthful attached + warning reported.
            disk = json.loads(mcp_file.read_text(encoding="utf-8"))
            assert disk["servers"][0]["enabled"] is False
            assert pool.session((ref.project_id, ref.thread_id)) is live_before
            assert result.enabled is False
            assert result.active_servers == ("cdp",)
            assert len(result.warnings) == 1
            assert "新建会话" in result.warnings[0]
        finally:
            await external_service.shutdown()
            await manager.shutdown()

    asyncio.run(run())
