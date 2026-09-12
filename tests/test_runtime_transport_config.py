"""Runtime config transport slice: protocol decode/dispatch + strict client parsing.

Covers the `runtime.config.get` method registration, strict params decoding,
dispatch routing, wire projection, and the Python client's strict result
decoding (unknown or extra fields, wrong types, and unbounded lists are all
rejected).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from synapse.runtime.service import (
    GetRuntimeConfigQuery,
    McpServerView,
    RuntimeConfigView,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import (
    CAPABILITIES,
    RuntimeWebSocketClient,
)
from synapse.runtime.transport.client import ProtocolTransportError
from synapse.runtime.transport.protocol import (
    METHODS,
    ProtocolError,
    decode_params,
    dispatch,
    project_result,
)

SESSION = SessionRef("p", "t")


def run(coro):
    return asyncio.run(coro)


def _config_result(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "current_model": "openai:alpha",
        "available_models": ["openai:alpha", "openai:beta"],
        "thinking_level": "high",
        "thinking_levels": ["off", "low", "high"],
        "mcp_servers": [
            {
                "name": "files",
                "transport": "stdio",
                "enabled": True,
                "tool_prefix": "mcp__files",
            }
        ],
        "mcp_enabled": True,
        "can_set_thinking": False,
        "can_toggle_mcp_global": False,
        "project_thinking_level": "low",
        "can_set_project_thinking": True,
    }
    data.update(overrides)
    return data


def _config_view() -> RuntimeConfigView:
    return RuntimeConfigView(
        current_model="openai:alpha",
        available_models=("openai:alpha", "openai:beta"),
        thinking_level="high",
        thinking_levels=("off", "low", "high"),
        mcp_servers=(McpServerView("files", "stdio", True, tool_prefix="mcp__files"),),
        mcp_enabled=True,
        project_thinking_level="low",
        can_set_project_thinking=True,
    )


# ---------------------------------------------------------------------------
# Protocol: method registration + decode_params
# ---------------------------------------------------------------------------


def test_runtime_config_get_is_registered() -> None:
    assert "runtime.config.get" in METHODS


def test_decode_runtime_config_get() -> None:
    decoded = decode_params(
        "runtime.config.get", {"session": {"project_id": "p", "thread_id": "t"}}
    )
    assert isinstance(decoded, GetRuntimeConfigQuery)
    assert decoded.session == SESSION

    for params in (
        {},
        {"session": {"project_id": "p"}},
        {"session": {"project_id": "p", "thread_id": "t"}, "extra": 1},
        {"project_id": "p"},
    ):
        with pytest.raises(ProtocolError) as caught:
            decode_params("runtime.config.get", params)
        assert caught.value.service_code == "invalid_params"


class _ConfigDispatchSpy:
    def __init__(self) -> None:
        self.calls = 0
        self.query: GetRuntimeConfigQuery | None = None

    async def get_runtime_config(self, query: GetRuntimeConfigQuery) -> RuntimeConfigView:
        self.calls += 1
        self.query = query
        return _config_view()


def test_dispatch_routes_runtime_config_get() -> None:
    async def body() -> None:
        spy = _ConfigDispatchSpy()
        result = await dispatch(
            spy,
            "runtime.config.get",
            {"session": {"project_id": "p", "thread_id": "t"}},
        )
        assert spy.calls == 1
        assert isinstance(result, RuntimeConfigView)
        assert result.current_model == "openai:alpha"

    run(body())


def test_wire_projection_of_runtime_config_view() -> None:
    assert project_result(_config_view()) == _config_result()


def test_wire_projection_cannot_carry_secret_like_keys() -> None:
    projected = project_result(_config_view())
    wire = json.dumps(projected, allow_nan=False)
    for forbidden in ("api_key", "env", "headers", "url", "command", "args", "goal"):
        assert forbidden not in wire


# ---------------------------------------------------------------------------
# RuntimeWebSocketClient strict parsing
# ---------------------------------------------------------------------------


class Fake:
    def __init__(self, result: dict[str, object]) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, object]] = []
        self.result = result
        self.closed = False

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            response = {
                "wire_version": "1",
                "supported_versions": ["1"],
                "capabilities": CAPABILITIES,
            }
        else:
            response = self.result
        await self.inbox.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "meta": {"wire_version": "1"},
                    "result": response,
                }
            )
        )

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


def client(fake: Fake) -> RuntimeWebSocketClient:
    return RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: fake)


def _business_frame(fake: Fake) -> dict[str, object]:
    return next(
        frame for frame in fake.frames if frame["method"] != "runtime.protocol.negotiate"
    )


def test_client_get_runtime_config_roundtrip() -> None:
    async def body() -> None:
        fake = Fake(_config_result())
        result = await client(fake).get_runtime_config(GetRuntimeConfigQuery(SESSION))
        assert isinstance(result, RuntimeConfigView)
        assert result.current_model == "openai:alpha"
        assert result.available_models == ("openai:alpha", "openai:beta")
        assert result.thinking_level == "high"
        assert result.thinking_levels == ("off", "low", "high")
        assert len(result.mcp_servers) == 1
        mcp = result.mcp_servers[0]
        assert (mcp.name, mcp.transport, mcp.enabled, mcp.tool_prefix) == (
            "files",
            "stdio",
            True,
            "mcp__files",
        )
        assert result.can_set_thinking is False
        assert result.can_toggle_mcp_global is False
        assert result.project_thinking_level == "low"
        assert result.can_set_project_thinking is True
        frame = _business_frame(fake)
        assert frame["method"] == "runtime.config.get"
        assert frame["params"] == {"session": {"project_id": "p", "thread_id": "t"}}

    run(body())


def test_client_ignores_additive_runtime_config_fields() -> None:
    async def body() -> None:
        runtime_client = client(Fake({**_config_result(), "project_id": "p"}))
        try:
            result = await runtime_client.get_runtime_config(GetRuntimeConfigQuery(SESSION))
            assert not hasattr(result, "project_id")
        finally:
            await runtime_client.close()

    run(body())


def test_client_rejects_invalid_runtime_config_results() -> None:
    for label, result in (
        (
            "missing field",
            {
                key: value
                for key, value in _config_result().items()
                if key != "can_set_thinking"
            },
        ),
        ("current model null", _config_result(current_model=None)),
        ("models not list", _config_result(available_models="openai:alpha")),
        ("model not str", _config_result(available_models=["openai:alpha", 3])),
        ("empty current model", _config_result(current_model="")),
        ("thinking level wrong type", _config_result(thinking_level=5)),
        ("levels wrong type", _config_result(thinking_levels="high")),
        ("mcp not list", _config_result(mcp_servers={})),
        (
            "mcp extra secret key",
            _config_result(
                mcp_servers=[
                    {
                        "name": "files",
                        "transport": "stdio",
                        "enabled": True,
                        "tool_prefix": None,
                        "command": "leak",
                    }
                ]
            ),
        ),
        (
            "mcp wrong flag type",
            _config_result(
                mcp_servers=[
                    {
                        "name": "files",
                        "transport": "stdio",
                        "enabled": 1,
                        "tool_prefix": None,
                    }
                ]
            ),
        ),
        ("capability wrong type", _config_result(can_toggle_mcp_global=1)),
        (
            "project capability wrong type",
            _config_result(can_set_project_thinking=1),
        ),
        (
            "project level wrong type",
            _config_result(project_thinking_level=5),
        ),
        ("project level empty", _config_result(project_thinking_level="")),
        ("too many models", _config_result(available_models=["m"] * 257)),
        ("too many levels", _config_result(thinking_levels=["high"] * 33)),
        (
            "too many mcp servers",
            _config_result(
                mcp_servers=[
                    {
                        "name": f"s{i}",
                        "transport": "stdio",
                        "enabled": True,
                        "tool_prefix": None,
                    }
                    for i in range(129)
                ]
            ),
        ),
    ):
        try:
            run(client(Fake(result)).get_runtime_config(GetRuntimeConfigQuery(SESSION)))
        except ProtocolTransportError:
            continue
        raise AssertionError(f"expected ProtocolTransportError for case: {label}")
