from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from acp.schema import (
    HttpMcpServer,
    McpCapabilities,
    McpServerStdio,
    PermissionOption,
    SseMcpServer,
    ToolCallUpdate,
)
from pydantic import BaseModel

from synapse.acp.external_client import (
    ExternalAgentSpec,
    ExternalAgentTimeout,
    ExternalTimeouts,
    mcp_servers_to_acp,
    open_external_session,
    prompt_blocks_to_acp,
)
from synapse.content.multimodal import Attachment
from synapse.integrations.mcp_client import McpServerConfig

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


def test_mcp_servers_to_acp_filters_disabled_and_by_capabilities() -> None:
    servers = [
        McpServerConfig(name="off", transport="stdio", command="npx", enabled=False),
        McpServerConfig(
            name="fs",
            transport="stdio",
            command="uvx",
            args=["mcp-fs", "/tmp"],
            env={"TOKEN": "secret"},
        ),
        McpServerConfig(
            name="remote_http",
            transport="streamable_http",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer abc"},
        ),
        McpServerConfig(
            name="remote_sse",
            transport="sse",
            url="https://example.com/sse",
        ),
    ]
    without_caps = mcp_servers_to_acp(servers, None)
    assert len(without_caps) == 1
    assert isinstance(without_caps[0], McpServerStdio)
    assert without_caps[0].name == "fs"
    assert without_caps[0].command == "uvx"
    assert without_caps[0].args == ["mcp-fs", "/tmp"]
    assert [(item.name, item.value) for item in without_caps[0].env] == [("TOKEN", "secret")]

    with_http = mcp_servers_to_acp(servers, McpCapabilities(http=True, sse=False))
    assert [type(item) for item in with_http] == [McpServerStdio, HttpMcpServer]
    assert with_http[1].url == "https://example.com/mcp"
    assert [(h.name, h.value) for h in with_http[1].headers] == [("Authorization", "Bearer abc")]

    with_sse = mcp_servers_to_acp(servers, McpCapabilities(http=False, sse=True))
    assert [type(item) for item in with_sse] == [McpServerStdio, SseMcpServer]
    assert with_sse[1].url == "https://example.com/sse"


def test_external_acp_forwards_mcp_servers_on_new_and_load(tmp_path: Path) -> None:
    agent_script = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        result = dict(
            protocolVersion=1,
            agentCapabilities=dict(loadSession=True, mcpCapabilities=dict(http=True)),
        )
    elif method in ("session/new", "session/load"):
        servers = msg["params"]["mcpServers"]
        assert [s["name"] for s in servers] == ["fs", "remote_http"]
        assert servers[0]["command"] == "uvx"
        assert servers[0]["env"] == [dict(name="KEY", value="val")]
        assert servers[1]["type"] == "http"
        assert servers[1]["url"] == "https://example.com/mcp"
        result = dict(sessionId="peer-1") if method == "session/new" else {}
    else:
        raise AssertionError(method)
    print(json.dumps(dict(jsonrpc="2.0", id=msg["id"], result=result)), flush=True)
'''
    servers = [
        McpServerConfig(
            name="fs", transport="stdio", command="uvx", args=["mcp-fs"], env={"KEY": "val"}
        ),
        McpServerConfig(
            name="remote_http", transport="streamable_http", url="https://example.com/mcp"
        ),
        McpServerConfig(name="remote_sse", transport="sse", url="https://example.com/sse"),
    ]
    spec = ExternalAgentSpec(sys.executable, ("-c", agent_script))

    async def noop(event: BaseModel) -> None:
        del event

    async def run() -> None:
        async with open_external_session(spec, tmp_path, noop, mcp_servers=servers) as created:
            assert created.created.session_id == "peer-1"
        async with open_external_session(
            spec,
            tmp_path,
            noop,
            existing_session_id="peer-1",
            on_replay=noop,
            mcp_servers=servers,
        ) as loaded:
            assert loaded.created.session_id == "peer-1"

    asyncio.run(run())


def test_external_acp_inherits_system_proxy_env_without_leaking_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("SYNAPSE_TEST_SECRET", "do-not-leak")
    agent_script = r'''
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        assert os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:7890"
        assert "SYNAPSE_TEST_SECRET" not in os.environ
        result = dict(protocolVersion=1)
    elif method == "session/new":
        result = dict(sessionId="peer-proxy")
    else:
        raise AssertionError(method)
    print(json.dumps(dict(jsonrpc="2.0", id=msg["id"], result=result)), flush=True)
'''
    spec = ExternalAgentSpec(sys.executable, ("-c", agent_script))

    async def noop(event: BaseModel) -> None:
        del event

    async def run() -> None:
        async with open_external_session(spec, tmp_path, noop) as session:
            assert session.created.session_id == "peer-proxy"

    asyncio.run(run())


def test_prompt_blocks_to_acp_encodes_images_placeholders_and_image_only() -> None:
    import base64

    att1 = Attachment(id=1, name="a.png", mime="image/png", data=b"\x89PNG-one")
    att2 = Attachment(id=2, name="b.jpg", mime="image/jpg", data=b"\xff\xd8-two")

    # Plain text + attachments (web console style)
    plain = prompt_blocks_to_acp("describe", [att1, att2])
    assert [b.type for b in plain] == ["text", "image", "image"]
    assert plain[0].text == "describe"
    assert plain[1].mime_type == "image/png"
    assert base64.b64decode(plain[1].data) == b"\x89PNG-one"
    assert plain[2].mime_type == "image/jpeg"
    assert base64.b64decode(plain[2].data) == b"\xff\xd8-two"

    # Image-only turn includes fallback text block so strict ACP agents accept it
    image_only = prompt_blocks_to_acp("   ", [att1])
    assert [b.type for b in image_only] == ["text", "image"]
    assert image_only[0].text == "(see attached image)"
    assert base64.b64decode(image_only[1].data) == b"\x89PNG-one"

    # Placeholder interleaving + trailing unreferenced attachment
    interleaved = prompt_blocks_to_acp("compare [image#1] please", [att1, att2])
    assert [b.type for b in interleaved] == ["text", "image", "text", "image"]
    assert interleaved[0].text == "compare "
    assert base64.b64decode(interleaved[1].data) == b"\x89PNG-one"
    assert interleaved[2].text == " please"
    assert base64.b64decode(interleaved[3].data) == b"\xff\xd8-two"


def test_external_acp_prompt_sends_image_content_blocks(tmp_path: Path) -> None:
    import base64

    raw_png = b"\x89PNG\r\n\x1a\nfake-image-payload"
    expected_b64 = base64.standard_b64encode(raw_png).decode("ascii")
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
        result = dict(sessionId="peer-img")
    elif method == "session/prompt":
        blocks = msg["params"]["prompt"]
        assert len(blocks) == 2
        assert blocks[0] == dict(type="text", text="what is this?")
        assert blocks[1]["type"] == "image"
        assert blocks[1]["mimeType"] == "image/png"
        assert blocks[1]["data"] == {expected_b64!r}
        print(json.dumps(dict(jsonrpc="2.0", method="session/update", params=dict(
            sessionId="peer-img",
            update=dict(sessionUpdate="agent_message_chunk",
                        content=dict(type="text", text="saw image"))))), flush=True)
        result = dict(stopReason="end_turn")
    else:
        raise AssertionError(method)
    print(json.dumps(dict(jsonrpc="2.0", id=msg["id"], result=result)), flush=True)
'''
    spec = ExternalAgentSpec(sys.executable, ("-u", "-c", agent_script))
    updates: list[BaseModel] = []

    async def on_update(event: BaseModel) -> None:
        updates.append(event)

    async def run() -> None:
        async with open_external_session(spec, tmp_path, on_update) as session:
            assert session.supports_images is True
            response = await session.prompt(
                "what is this?",
                attachments=[Attachment(id=1, name="shot.png", mime="image/png", data=raw_png)],
            )
            assert response.stop_reason == "end_turn"
            assert [u.model_dump()["content"]["text"] for u in updates] == ["saw image"]

    asyncio.run(run())
