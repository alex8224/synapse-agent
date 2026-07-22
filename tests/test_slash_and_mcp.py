"""Tests for slash commands, transcript export, and MCP transports."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from synapse.mcp_client import load_mcp_server_configs
from synapse.sessions import SessionStore
from synapse.slash_cmds import handle_slash
from synapse.transcript import (
    export_transcript_json,
    export_transcript_markdown,
    load_messages_from_checkpointer,
    message_to_export_dict,
)


class _FakeSettings:
    def __init__(self, tmp_path: Path):
        self.model = "openai:demo"
        self.active_model = None
        self.enable_thinking = True
        self.reasoning_effort = "high"
        self.openai_base_url = None
        self.max_concurrency = 4
        self.enable_mcp = True
        self.mcp_config_path = None
        self.mcp_servers_json = None
        self.checkpoint_backend = "memory"
        self.checkpoint_path = tmp_path / "ck.sqlite"
        self._sessions = tmp_path / "sessions.sqlite"
        self.models_config_path = None
        self.models_json = None
        self.openai_api_key = None
        self.anthropic_api_key = None
        self.parallel_tool_calls = True

    def resolved_sessions_path(self) -> Path:
        return self._sessions


def test_mcp_config_parses_stdio_and_remote(tmp_path: Path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "local",
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-m", "x"],
                        "enabled": True,
                    },
                    {
                        "name": "sse1",
                        "transport": "sse",
                        "url": "http://127.0.0.1:9/sse",
                        "enabled": True,
                    },
                    {
                        "name": "http1",
                        "transport": "streamable_http",
                        "url": "http://127.0.0.1:9/mcp",
                        "headers": {"Authorization": "Bearer t"},
                        "enabled": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    servers = load_mcp_server_configs(path=path)
    assert [s.name for s in servers] == ["local", "sse1", "http1"]
    assert servers[0].transport == "stdio"
    assert servers[0].command == "python"
    assert servers[1].transport == "sse"
    assert servers[1].url.endswith("/sse")
    assert servers[2].transport == "streamable_http"
    assert servers[2].headers["Authorization"] == "Bearer t"
    assert servers[2].enabled is False


def test_slash_session_management(tmp_path: Path):
    settings = _FakeSettings(tmp_path)
    agent = SimpleNamespace(_coding_model_profile="openai:demo", _coding_checkpointer=None)

    r = handle_slash(
        "/new",
        settings=settings,
        agent=agent,
        thread_id="old123",
        project_root=tmp_path,
    )
    assert r.handled
    assert r.thread_id and r.thread_id != "old123"
    tid = r.thread_id

    r = handle_slash(
        "/rename My Session Title",
        settings=settings,
        agent=agent,
        thread_id=tid,
        project_root=tmp_path,
    )
    assert r.handled and not r.error

    r = handle_slash(
        "/sessions",
        settings=settings,
        agent=agent,
        thread_id=tid,
        project_root=tmp_path,
    )
    assert r.handled
    assert any(tid in line for line in r.lines)

    r = handle_slash(
        "/session search My Session",
        settings=settings,
        agent=agent,
        thread_id=tid,
        project_root=tmp_path,
    )
    assert r.handled
    assert any("My Session" in line or tid in line for line in r.lines)

    r = handle_slash(
        "/session show",
        settings=settings,
        agent=agent,
        thread_id=tid,
        project_root=tmp_path,
    )
    assert r.handled
    assert any("current session" in line for line in r.lines)

    # create another session then delete inactive one
    other = handle_slash(
        "/new",
        settings=settings,
        agent=agent,
        thread_id=tid,
        project_root=tmp_path,
    ).thread_id
    assert other
    r = handle_slash(
        f"/session delete {tid}",
        settings=settings,
        agent=agent,
        thread_id=other,
        project_root=tmp_path,
    )
    assert r.handled and not r.error
    store = SessionStore(settings.resolved_sessions_path())
    assert store.get(tid) is None


def test_slash_mcp_list_and_help(tmp_path: Path):
    settings = _FakeSettings(tmp_path)
    settings.mcp_servers_json = json.dumps(
        [
            {
                "name": "remote",
                "transport": "sse",
                "url": "http://127.0.0.1:9/sse",
                "enabled": True,
            },
            {
                "name": "local",
                "transport": "stdio",
                "command": "echo",
                "args": ["x"],
                "enabled": False,
            },
        ]
    )
    agent = SimpleNamespace(_coding_model_profile="openai:demo", _coding_checkpointer=None)
    r = handle_slash(
        "/mcp list",
        settings=settings,
        agent=agent,
        thread_id="t1",
        project_root=tmp_path,
    )
    assert r.handled
    blob = "\n".join(r.lines)
    assert "transport=sse" in blob
    assert "transport=stdio" in blob
    assert "remote" in blob

    r = handle_slash(
        "/mcp config",
        settings=settings,
        agent=agent,
        thread_id="t1",
        project_root=tmp_path,
    )
    assert r.handled
    assert any("stdio" in line and "streamable_http" in line for line in r.lines)

    r = handle_slash(
        "/help",
        settings=settings,
        agent=agent,
        thread_id="t1",
        project_root=tmp_path,
    )
    assert r.handled
    assert any("/session" in line for line in r.lines)
    assert any("/mcp" in line for line in r.lines)


def test_slash_export_includes_transcript(tmp_path: Path):
    settings = _FakeSettings(tmp_path)
    store = SessionStore(settings.resolved_sessions_path())
    store.ensure("abc", title="demo", model="openai:demo")

    class Msg:
        def __init__(self, role, content):
            self.type = role
            self.content = content
            self.id = role
            self.name = None

    class FakeCP:
        def get_tuple(self, config):
            return SimpleNamespace(
                checkpoint={
                    "channel_values": {
                        "messages": [
                            Msg("human", "hello"),
                            Msg("ai", "world"),
                        ]
                    }
                }
            )

    agent = SimpleNamespace(_coding_checkpointer=FakeCP(), get_state=None)
    out = tmp_path / "out.md"
    r = handle_slash(
        f"/export md {out}",
        settings=settings,
        agent=agent,
        thread_id="abc",
        project_root=tmp_path,
    )
    assert r.handled and not r.error
    assert r.notice and "exported" in r.notice
    # Must not dump transcript body into slash lines / TUI.
    assert all("hello" not in line and "world" not in line for line in r.lines)
    text = out.read_text(encoding="utf-8")
    assert "hello" in text
    assert "world" in text
    assert "### 1. human" in text


def test_slash_export_defaults_to_file(tmp_path: Path):
    settings = _FakeSettings(tmp_path)
    store = SessionStore(settings.resolved_sessions_path())
    store.ensure("tid1", title="demo", model="openai:demo")

    class Msg:
        type = "human"
        content = "only-in-file"
        id = "1"
        name = None

    class FakeCP:
        def get_tuple(self, config):
            return SimpleNamespace(
                checkpoint={"channel_values": {"messages": [Msg()]}}
            )

    agent = SimpleNamespace(_coding_checkpointer=FakeCP(), get_state=None)
    r = handle_slash(
        "/export",
        settings=settings,
        agent=agent,
        thread_id="tid1",
        project_root=tmp_path,
    )
    assert r.handled and not r.error
    assert r.notice and "exported md ->" in r.notice
    assert all("only-in-file" not in line for line in r.lines)
    default = tmp_path / "exports" / "tid1.md"
    assert default.is_file()
    assert "only-in-file" in default.read_text(encoding="utf-8")


def test_transcript_helpers():
    class Msg:
        type = "ai"
        content = [{"type": "text", "text": "hi"}]
        id = "1"
        name = None

    d = message_to_export_dict(Msg())
    assert d["role"] == "ai"
    assert "hi" in d["content"]

    md = export_transcript_markdown(
        thread_id="t",
        title="T",
        messages=[Msg()],
    )
    assert "Transcript" in md
    js = export_transcript_json(thread_id="t", messages=[Msg()])
    assert js["messages"][0]["content"] == "hi"

    class CP:
        def get_tuple(self, config):
            return SimpleNamespace(
                checkpoint={"channel_values": {"messages": [Msg()]}}
            )

    msgs = load_messages_from_checkpointer(CP(), "tid")
    assert len(msgs) == 1


def test_mcp_pool_reuses_session_for_calls():
    """Pool should call the same live session without reopening per tool call."""
    from synapse.mcp_client import McpServerConfig, McpSessionPool

    class FakeSession:
        def __init__(self):
            self.calls = 0

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="ping",
                        description="ping",
                        inputSchema={"type": "object", "properties": {}},
                    )
                ]
            )

        async def call_tool(self, name, arguments=None):
            self.calls += 1
            return SimpleNamespace(
                content=[SimpleNamespace(text=f"pong-{self.calls}")],
                isError=False,
            )

    pool = McpSessionPool()
    fake = FakeSession()

    async def fake_open(server):
        from synapse.mcp_client import _LiveServer

        live = _LiveServer(
            config=server,
            session=fake,
            transport_cm=SimpleNamespace(__aexit__=lambda *a, **k: None),
            session_cm=SimpleNamespace(__aexit__=lambda *a, **k: None),
        )
        # Mirror real _open_one: register before tool calls reuse session.
        pool._servers[server.name] = live
        return live, None

    try:
        with patch.object(pool, "_open_one", side_effect=fake_open):
            result = pool.load(
                [
                    McpServerConfig(
                        name="demo",
                        transport="stdio",
                        command="x",
                        enabled=True,
                    )
                ]
            )
        assert result.servers == ["demo"]
        assert len(result.tools) == 1
        out1 = result.tools[0].invoke({})
        out2 = result.tools[0].invoke({})
        assert out1 == "pong-1"
        assert out2 == "pong-2"
        assert fake.calls == 2
    finally:
        pool.close()
    # close() clears tools to prevent stale references to stopped event loop.
    assert pool.tools == []
    assert pool.tool_names == []


def test_mcp_open_http_and_stdio_branches_selected():
    from synapse.mcp_client import McpServerConfig, McpSessionPool

    pool = McpSessionPool()
    calls: list[str] = []

    async def open_stdio(server):
        calls.append("stdio")
        raise RuntimeError("stop-stdio")

    async def open_http(server):
        calls.append(server.transport)
        raise RuntimeError("stop-http")

    try:
        with (
            patch.object(pool, "_open_stdio", side_effect=open_stdio),
            patch.object(pool, "_open_http", side_effect=open_http),
        ):
            r = pool.load(
                [
                    McpServerConfig(name="a", transport="stdio", command="c"),
                    McpServerConfig(
                        name="b", transport="sse", url="http://x/sse"
                    ),
                    McpServerConfig(
                        name="c",
                        transport="streamable_http",
                        url="http://x/mcp",
                    ),
                ]
            )
        assert "stdio" in calls
        assert "sse" in calls
        assert "streamable_http" in calls
        assert len(r.warnings) == 3
    finally:
        pool.close()


# ---------------------------------------------------------------------------
# Rebuild strategy: MCP pool reuse + thinking fast path
# ---------------------------------------------------------------------------


def _capture_build(monkeypatch):
    """Patch slash_cmds.build_coding_agent to capture kwargs."""
    from synapse import slash_cmds

    calls: list[dict] = []

    def fake_build(settings, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(**{k: v for k, v in kwargs.items()})

    monkeypatch.setattr(slash_cmds, "build_coding_agent", fake_build)
    return calls


def test_rebuild_agent_reuses_mcp_pool_tools(tmp_path, monkeypatch):
    """Attached agent + live pool -> reuse tools, no reconnect."""
    from synapse import slash_cmds

    settings = _FakeSettings(tmp_path)
    tool = SimpleNamespace(name="ping")
    pool = SimpleNamespace(tools=[tool], tool_names=["ping"])
    monkeypatch.setattr(slash_cmds, "get_active_mcp_pool", lambda: pool)
    calls = _capture_build(monkeypatch)

    model = object()
    agent = SimpleNamespace(
        _coding_checkpointer="cp",
        _coding_model=model,
        _coding_model_registry="reg",
        _coding_mcp_attached=True,
    )
    slash_cmds._rebuild_agent(
        settings, project_root=tmp_path, model_name=None, agent=agent
    )
    assert calls
    kw = calls[0]
    assert kw["load_mcp"] is False
    assert kw["mcp_tools"] == [tool]
    assert kw["model"] is model
    assert kw["checkpointer"] == "cp"


def test_rebuild_agent_defers_mcp_when_not_attached(tmp_path, monkeypatch):
    """Deferred-at-startup agent keeps MCP deferred on rebuild (fast path)."""
    from synapse import slash_cmds

    settings = _FakeSettings(tmp_path)
    monkeypatch.setattr(slash_cmds, "get_active_mcp_pool", lambda: None)
    calls = _capture_build(monkeypatch)

    agent = SimpleNamespace(
        _coding_checkpointer=None,
        _coding_model=None,
        _coding_model_registry=None,
        _coding_mcp_attached=False,
    )
    slash_cmds._rebuild_agent(
        settings, project_root=tmp_path, model_name="openai:demo", agent=agent
    )
    assert calls
    kw = calls[0]
    assert kw["load_mcp"] is False
    assert kw["mcp_tools"] is None


def test_mcp_reload_forces_reconnect(tmp_path, monkeypatch):
    """/mcp reload must bypass pool reuse and force load_mcp=True."""
    from synapse import slash_cmds

    settings = _FakeSettings(tmp_path)
    pool = SimpleNamespace(tools=[SimpleNamespace(name="t")], tool_names=["t"])
    monkeypatch.setattr(slash_cmds, "get_active_mcp_pool", lambda: pool)
    calls = _capture_build(monkeypatch)

    agent = SimpleNamespace(
        _coding_checkpointer=None,
        _coding_model=None,
        _coding_model_registry=None,
        _coding_mcp_attached=True,
    )
    r = handle_slash(
        "/mcp reload",
        settings=settings,
        agent=agent,
        thread_id="t1",
        project_root=tmp_path,
    )
    assert r.handled and not r.error
    assert calls
    kw = calls[0]
    assert kw["load_mcp"] is True
    assert kw["mcp_tools"] is None


def test_apply_thinking_inplace_copies_attrs(monkeypatch):
    """In-place thinking update copies attrs from a fresh same-type model."""
    import synapse.models_registry as reg_mod
    from synapse.slash_cmds import _apply_thinking_inplace

    class FakeModel:
        def __init__(self, effort, body):
            self.reasoning_effort = effort
            self.extra_body = body

    live = FakeModel("high", {"old": 1})
    fresh = FakeModel("low", {"new": 2})
    monkeypatch.setattr(
        reg_mod, "build_model_from_settings", lambda s, model_name=None: (None, fresh)
    )
    agent = SimpleNamespace(_coding_model=live)

    assert _apply_thinking_inplace(object(), agent, "demo") is True
    assert live.reasoning_effort == "low"
    assert live.extra_body == {"new": 2}


def test_apply_thinking_inplace_fallbacks(monkeypatch):
    """No live model or type mismatch -> False (caller rebuilds)."""
    import synapse.models_registry as reg_mod
    from synapse.slash_cmds import _apply_thinking_inplace

    # No live model.
    assert _apply_thinking_inplace(object(), SimpleNamespace(), "demo") is False

    # Type mismatch between live and freshly built model.
    class A:
        pass

    class B:
        pass

    monkeypatch.setattr(
        reg_mod, "build_model_from_settings", lambda s, model_name=None: (None, B())
    )
    agent = SimpleNamespace(_coding_model=A())
    assert _apply_thinking_inplace(object(), agent, "demo") is False


def test_rebuild_agent_reuses_pool_when_attached_flag_false(tmp_path, monkeypatch):
    """Pool exists with tools but agent._coding_mcp_attached=False → reuse anyway.

    This simulates the startup race where phase-2 connects MCP in the
    background, creating a live pool, but self.agent hasn't been swapped
    yet (or was replaced by a /model switch that deferred). The pool
    should still be reused instead of reconnecting every time.
    """
    from synapse import slash_cmds

    settings = _FakeSettings(tmp_path)
    tool = SimpleNamespace(name="t")
    pool = SimpleNamespace(tools=[tool], tool_names=["t"])
    monkeypatch.setattr(slash_cmds, "get_active_mcp_pool", lambda: pool)
    calls = _capture_build(monkeypatch)

    agent = SimpleNamespace(
        _coding_checkpointer=None,
        _coding_model=None,
        _coding_model_registry=None,
        _coding_mcp_attached=False,  # flag is stale, but pool is alive
    )
    slash_cmds._rebuild_agent(
        settings, project_root=tmp_path, model_name="openai:demo", agent=agent
    )
    assert calls
    kw = calls[0]
    assert kw["load_mcp"] is False
    assert kw["mcp_tools"] == [tool]
