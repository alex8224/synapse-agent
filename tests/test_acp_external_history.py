import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from synapse.acp.external_history import ExternalTurnBridge, run_turn
from synapse.runtime.daemon.external_session_pool import ExternalSessionPool
from synapse.runtime.service.external_agents import ExternalAgentCatalog
from synapse.sessions.transcript_projection import TranscriptProjection

_AGENT = '''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'initialize':
        result = {'protocolVersion': 1}
    elif method == 'session/new':
        result = {'sessionId': 'peer'}
    elif method == 'session/prompt':
        def send(update):
            print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                'sessionId': 'peer', 'update': update}}), flush=True)
        send({'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'plan'}})
        send({'sessionUpdate': 'tool_call', 'toolCallId': 'raw', 'title': 'Write',
              'status': 'in_progress', 'rawInput': {'path': '/secret'}})
        send({'sessionUpdate': 'tool_call_update', 'toolCallId': 'raw', 'status': 'completed',
              'content': [{'type': 'content',
                           'content': {'type': 'text', 'text': 'wrote 42 bytes'}}]})
        send({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'done'}})
        result = {'stopReason': 'end_turn'}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)
'''


def test_external_turn_is_projected_into_transcript(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {"agents": [{"id": "test", "name": "Test", "command": sys.executable,
                         "args": ["-u", "-c", _AGENT]}]}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)

    async def run() -> None:
        bridge = ExternalTurnBridge("thread")
        async with ExternalSessionPool(catalog).lifetime() as pool:
            session = await pool.open(("project", "thread"), "test", tmp_path, bridge.feed)
            response = await run_turn(
                session, bridge, settings, turn_id="turn-1", text="hello"
            )
            assert response.stop_reason == "end_turn"

    asyncio.run(run())

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        page = projection.load_tail("thread", turns=10)
    finally:
        projection.close()
    events = page.events
    kinds = [event.kind for event in events]
    assert kinds == ["user", "thought", "tools", "answer"]
    assert "/secret" not in json.dumps([event.text for event in events])
    assert events[2].tool_results[0]["content"] == "wrote 42 bytes"
    assert events[2].tool_results[0]["status"] == "completed"
    assert events[-1].text == "done"


def test_external_turn_preserves_interleaved_thoughts_and_tools(tmp_path: Path) -> None:
    interleaved_agent = '''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'initialize':
        result = {'protocolVersion': 1}
    elif method == 'session/new':
        result = {'sessionId': 'peer'}
    elif method == 'session/prompt':
        def send(update):
            print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                'sessionId': 'peer', 'update': update}}), flush=True)
        send({'sessionUpdate': 'agent_thought_chunk',
              'content': {'type': 'text', 'text': 'step 1'}})
        send({'sessionUpdate': 'tool_call', 'toolCallId': 't1', 'title': 'Read',
              'status': 'in_progress'})
        send({'sessionUpdate': 'tool_call_update', 'toolCallId': 't1', 'status': 'completed',
              'content': [{'type': 'content', 'content': {'type': 'text', 'text': 'r1'}}]})
        send({'sessionUpdate': 'agent_thought_chunk',
              'content': {'type': 'text', 'text': 'step 2'}})
        send({'sessionUpdate': 'tool_call', 'toolCallId': 't2', 'title': 'Edit',
              'status': 'in_progress'})
        send({'sessionUpdate': 'tool_call_update', 'toolCallId': 't2', 'status': 'completed',
              'content': [{'type': 'content', 'content': {'type': 'text', 'text': 'r2'}}]})
        send({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'final'}})
        result = {'stopReason': 'end_turn'}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)
'''
    settings = SimpleNamespace(
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        workspace=tmp_path,
    )
    config = {
        "agents": [
            {
                "id": "test",
                "name": "Test",
                "command": sys.executable,
                "args": ["-u", "-c", interleaved_agent],
            }
        ]
    }
    (tmp_path / "external-acp-agents.json").write_text(json.dumps(config))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)

    async def run() -> None:
        bridge = ExternalTurnBridge("thread-interleaved")
        async with ExternalSessionPool(catalog).lifetime() as pool:
            session = await pool.open(
                ("project", "thread-interleaved"), "test", tmp_path, bridge.feed
            )
            response = await run_turn(
                session, bridge, settings, turn_id="turn-1", text="hello"
            )
            assert response.stop_reason == "end_turn"

    asyncio.run(run())

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        page = projection.load_tail("thread-interleaved", turns=10)
    finally:
        projection.close()
    events = page.events
    assert [event.kind for event in events] == [
        "user",
        "thought",
        "tools",
        "thought",
        "tools",
        "answer",
    ]
    assert events[1].text == "step 1"
    assert [c["name"] for c in events[2].tool_calls] == ["Read"]
    assert events[2].tool_results[0]["content"] == "r1"
    assert events[3].text == "step 2"
    assert [c["name"] for c in events[4].tool_calls] == ["Edit"]
    assert events[4].tool_results[0]["content"] == "r2"
    assert events[5].text == "final"
