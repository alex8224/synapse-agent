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
    assert [type(item) for item in without_caps] == [
        McpServerStdio,
        McpServerStdio,
        McpServerStdio,
    ]
    assert [item.name for item in without_caps] == ["fs", "remote_http", "remote_sse"]
    assert without_caps[0].command == "uvx"
    assert without_caps[0].args == ["mcp-fs", "/tmp"]
    assert [(item.name, item.value) for item in without_caps[0].env] == [("TOKEN", "secret")]
    assert without_caps[1].command == sys.executable
    assert without_caps[1].args == [
        "-m",
        "synapse.acp.mcp_stdio_proxy",
        "--transport",
        "streamable_http",
        "--url",
        "https://example.com/mcp",
    ]
    http_proxy_env = {item.name: item.value for item in without_caps[1].env}
    assert http_proxy_env["SYNAPSE_MCP_PROXY_HEADERS_JSON"] == '{"Authorization": "Bearer abc"}'
    assert "Bearer abc" not in " ".join(without_caps[1].args)
    assert without_caps[2].args == [
        "-m",
        "synapse.acp.mcp_stdio_proxy",
        "--transport",
        "sse",
        "--url",
        "https://example.com/sse",
    ]

    with_http = mcp_servers_to_acp(servers, McpCapabilities(http=True, sse=False))
    assert [type(item) for item in with_http] == [McpServerStdio, HttpMcpServer, McpServerStdio]
    assert with_http[1].url == "https://example.com/mcp"
    assert [(h.name, h.value) for h in with_http[1].headers] == [("Authorization", "Bearer abc")]
    assert with_http[2].name == "remote_sse"

    with_sse = mcp_servers_to_acp(servers, McpCapabilities(http=False, sse=True))
    assert [type(item) for item in with_sse] == [McpServerStdio, McpServerStdio, SseMcpServer]
    assert with_sse[1].name == "remote_http"
    assert with_sse[2].url == "https://example.com/sse"


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
        assert [s["name"] for s in servers] == ["fs", "remote_http", "remote_sse"]
        assert servers[0]["command"] == "uvx"
        assert servers[0]["env"] == [dict(name="KEY", value="val")]
        assert servers[1]["type"] == "http"
        assert servers[1]["url"] == "https://example.com/mcp"
        assert "synapse.acp.mcp_stdio_proxy" in servers[2]["args"]
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


def test_mcp_stdio_proxy_bridges_streamable_http(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.server
    import json
    import os
    import subprocess
    import threading

    seen_auth: list[str | None] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler convention
            seen_auth.append(self.headers.get("Authorization"))
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            method = payload.get("method")
            if method == "initialize":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "mock-http-mcp", "version": "1.0"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "tools/list":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "tools": [{"name": "search", "inputSchema": {"type": "object"}}]
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env["SYNAPSE_MCP_PROXY_HEADERS_JSON"] = '{"Authorization": "Bearer test-token"}'
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env.pop(key, None)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "synapse.acp.mcp_stdio_proxy",
                "--transport",
                "streamable_http",
                "--url",
                f"http://127.0.0.1:{port}/mcp",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
        )
        assert proc.stdin is not None and proc.stdout is not None
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()
        init_resp = json.loads(proc.stdout.readline())
        assert init_resp["id"] == 1
        assert init_resp["result"]["serverInfo"]["name"] == "mock-http-mcp"

        initialized_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write(json.dumps(initialized_notif) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        proc.stdin.flush()
        tools_resp = json.loads(proc.stdout.readline())
        assert tools_resp["id"] == 2
        assert [t["name"] for t in tools_resp["result"]["tools"]] == ["search"]

        proc.stdin.close()
        _stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, stderr
        assert seen_auth == ["Bearer test-token", "Bearer test-token", "Bearer test-token"]
    finally:
        server.shutdown()
        server.server_close()


def test_mcp_stdio_proxy_sanitizes_stdio_tool_schemas(tmp_path: Path) -> None:
    import json
    import os
    import subprocess

    child_script = tmp_path / "mock_cua.py"
    child_script.write_text(
        """import json, sys
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cua", "version": "1.0"},
            },
        }
        print(json.dumps(resp), flush=True)
    elif method == "tools/list":
        schema = {
            "type": "object",
            "properties": {
                "expect": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "element": {
                                "type": "object",
                                "properties": {
                                    "exists": {"type": "string", "enum": [""]},
                                    "visible": {"type": "boolean", "enum": [True, False]},
                                    "mode": {"type": "string", "enum": ["", "fast", "accurate"]},
                                },
                            }
                        },
                    },
                }
            },
        }
        resp = {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {"tools": [{"name": "cua_act", "inputSchema": schema}]},
        }
        print(json.dumps(resp), flush=True)
""",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["SYNAPSE_MCP_PROXY_STDIO_JSON"] = json.dumps(
        {"command": sys.executable, "args": ["-u", str(child_script)]}
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "synapse.acp.mcp_stdio_proxy", "--transport", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "gemini-cli", "version": "1.0"},
                },
            }
        )
        + "\n"
    )
    proc.stdin.flush()
    init_resp = json.loads(proc.stdout.readline())
    assert init_resp["id"] == 1

    proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
    proc.stdin.flush()
    tools_resp = json.loads(proc.stdout.readline())
    assert tools_resp["id"] == 2
    props = (
        tools_resp["result"]["tools"][0]["inputSchema"]["properties"]["expect"]["items"][
            "properties"
        ]["element"]["properties"]
    )
    assert props["exists"] == {"type": "string"}
    assert props["visible"] == {"type": "boolean"}
    assert props["mode"] == {"type": "string", "enum": ["fast", "accurate"]}

    proc.stdin.close()
    _stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode == 0, stderr


def test_gemini_external_acp_wraps_stdio_mcp_for_schema_sanitization() -> None:
    from acp.schema import Implementation, InitializeResponse

    from synapse.acp.external_client import _should_sanitize_stdio_mcp

    gemini_handshake = InitializeResponse(
        protocol_version=1,
        agent_info=Implementation(name="gemini-cli", version="1.0"),
    )
    claude_handshake = InitializeResponse(
        protocol_version=1,
        agent_info=Implementation(name="claude-code-acp", version="1.0"),
    )
    assert _should_sanitize_stdio_mcp(ExternalAgentSpec("gemini"), gemini_handshake) is True
    assert _should_sanitize_stdio_mcp(ExternalAgentSpec("npx", ("@foo/bar",)), gemini_handshake)
    assert _should_sanitize_stdio_mcp(ExternalAgentSpec("claude"), claude_handshake) is False

    wrapped = mcp_servers_to_acp(
        [McpServerConfig(name="cua", transport="stdio", command="cua-mcp", args=["--stdio"])],
        None,
        sanitize_stdio=True,
    )
    assert len(wrapped) == 1
    assert isinstance(wrapped[0], McpServerStdio)
    assert wrapped[0].command == sys.executable
    env_map = {item.name: item.value for item in wrapped[0].env}
    assert env_map["SYNAPSE_MCP_PROXY_STDIO_JSON"] == (
        '{"args": ["--stdio"], "command": "cua-mcp"}'
    )


def test_mcp_stdio_proxy_recovers_from_errors_and_retries_stale_connection() -> None:
    import http.server
    import json
    import subprocess
    import threading

    drop_once = {"dropped": False}

    class _FlakyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler convention
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            req_id = payload.get("id")
            method = payload.get("method")
            if method == "initialize":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "flaky-http-mcp", "version": "1.0"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if req_id == 2:
                err_body = b"upstream boom"
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
                return
            if req_id == 3 and not drop_once["dropped"]:
                drop_once["dropped"] = True
                self.close_connection = True
                return
            if req_id == 4:
                sse_payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"content": [{"type": "text", "text": "from-sse"}]},
                    }
                )
                body = f"event: message\ndata: {sse_payload}\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": f"ok-{req_id}"}]},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "synapse.acp.mcp_stdio_proxy",
                "--transport",
                "streamable_http",
                "--url",
                f"http://127.0.0.1:{port}/mcp",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["id"] == 1

        # HTTP 500 must return a JSON-RPC error instead of hanging or killing the proxy.
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}}) + "\n"
        )
        proc.stdin.flush()
        err_resp = json.loads(proc.stdout.readline())
        assert err_resp["id"] == 2
        assert "500" in err_resp["error"]["message"]

        # Abrupt server disconnect on first attempt must transparently retry once and succeed.
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}) + "\n"
        )
        proc.stdin.flush()
        retry_resp = json.loads(proc.stdout.readline())
        assert retry_resp["id"] == 3
        assert retry_resp["result"]["content"][0]["text"] == "ok-3"

        # Concurrent SSE + JSON requests must both complete without closure-capture loss.
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {}}) + "\n"
        )
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}}) + "\n"
        )
        proc.stdin.flush()
        both = [json.loads(proc.stdout.readline()), json.loads(proc.stdout.readline())]
        by_id = {item["id"]: item["result"]["content"][0]["text"] for item in both}
        assert by_id == {4: "from-sse", 5: "ok-5"}

        proc.stdin.close()
        _out, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, stderr
    finally:
        server.shutdown()
        server.server_close()


def test_external_acp_prompt_deadline_extends_on_updates(tmp_path: Path) -> None:
    agent_script = '''
import json, sys, time
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        result = dict(protocolVersion=1)
    elif method == "session/new":
        result = dict(sessionId="peer-stream")
    elif method == "session/prompt":
        for idx in range(3):
            time.sleep(0.2)
            print(json.dumps(dict(jsonrpc="2.0", method="session/update", params=dict(
                sessionId="peer-stream",
                update=dict(sessionUpdate="agent_message_chunk",
                            content=dict(type="text", text=f"chunk-{idx} "))))), flush=True)
        result = dict(stopReason="end_turn")
    else:
        raise AssertionError(method)
    print(json.dumps(dict(jsonrpc="2.0", id=msg["id"], result=result)), flush=True)
'''
    spec = ExternalAgentSpec(command=sys.executable, args=("-u", "-c", agent_script))
    seen: list[str] = []

    async def sink(update: object) -> None:
        content = getattr(update, "content", None)
        if content is not None and getattr(content, "text", None):
            seen.append(str(content.text))

    async def run() -> None:
        # Total stream time (~0.6s) exceeds prompt=0.35s, but each chunk arrives
        # within 0.2s so activity-based deadline extension keeps the session alive.
        timeouts = ExternalTimeouts(prompt=0.35)
        async with open_external_session(spec, tmp_path, sink, timeouts=timeouts) as session:
            resp = await session.prompt("stream")
            assert resp.stop_reason == "end_turn"

    asyncio.run(run())
    assert seen == ["chunk-0 ", "chunk-1 ", "chunk-2 "]


