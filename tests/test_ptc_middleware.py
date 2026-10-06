"""End-to-end tests for the PTC middleware against a real ``create_agent`` graph.

These run the middleware inside a genuine agent graph with a fake chat model so
the LangGraph tool-node wiring (runtime injection, request overrides, stream
writer) is exercised for real. ``run_code`` is backed by a scripted stand-in for
``synapse.runtime.ptc.process.run_code`` that honours the same signature; further
tests wire the real process module to check the SDK execution example and the
sync-timeout regression.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from synapse.runtime.ptc import sdk
from synapse.runtime.ptc.middleware import (
    _append_system_prompt,
    _run_code_args,
    build_ptc_middleware,
)
from synapse.runtime.ptc.protocol import PtcLimits


# --------------------------------------------------------------------------- #
# Tools used across the tests
# --------------------------------------------------------------------------- #
@tool
def read_file(path: str) -> str:
    """Read a file from the workspace."""
    return f"contents of {path}"


@tool(response_format="content_and_artifact")
def stat_file(path: str) -> tuple[str, dict]:
    """Stat a file and return a canonical payload."""
    return f"stat {path}", {"ptc": {"data": {"size": 3}, "truncated": False}}


@tool
def write_file(path: str, content: str) -> str:
    """Write a file."""
    return "written"


@tool
def write_todos(todos: list[str]) -> str:
    """Manage the session todo list."""
    return "ok"


@tool
def mcp_probe(q: str) -> str:
    """A tool with no local contract (stands in for an MCP tool)."""
    return q


@tool
def id_probe(path: str, runtime: ToolRuntime) -> str:
    """Report the runtime tool_call_id it was invoked with."""
    return f"id={runtime.tool_call_id}"


def _message_text(message: Any) -> str:
    """Flatten a message's content blocks (or string) into plain text."""
    if message is None:
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


# --------------------------------------------------------------------------- #
# Fake model + scripted process runner
# --------------------------------------------------------------------------- #
class _ToolBindableFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolBindableFakeModel:  # noqa: ARG002
        return self


def _run_code_response(code: Any, call_id: str = "call-1") -> AIMessage:
    if not isinstance(code, str):
        code = json.dumps(code)
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "run_code",
                "args": {"code": code, "intent": "orchestrate"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _scripted_runner(*, concurrent: bool = False) -> Any:
    """A stand-in for ``process.run_code`` that runs the calls in ``code``.

    ``code`` is a JSON object ``{"calls": [[name, args], ...]}``.
    """

    async def run_code(*, code, tool_names, dispatch, cwd, limits, **kwargs):  # noqa: ARG001
        spec = json.loads(code)
        calls = spec["calls"]
        results: list[Any] = []
        if concurrent:
            gathered = await asyncio.gather(
                *[dispatch(name, args) for name, args in calls], return_exceptions=True
            )
            results = list(gathered)
        else:
            for name, args in calls:
                try:
                    results.append(await dispatch(name, args))
                except BaseException as exc:  # noqa: BLE001
                    results.append(exc)
        value: list[Any] = []
        error: dict[str, Any] | None = None
        for result in results:
            if isinstance(result, BaseException):
                value.append(None)
                if error is None:
                    error = {"kind": getattr(result, "kind", "error"), "message": str(result)}
            else:
                value.append(result)
        out: dict[str, Any] = {"logs": [], "value": value}
        if error is not None:
            out["error"] = error
        return out

    return run_code


def _middleware(
    *,
    mode: str = "both",
    excluded: list[str] | None = None,
    require_approval: bool = False,
    readonly: bool = False,
    runner: Any | None = None,
    limits: PtcLimits | None = None,
) -> Any:
    return build_ptc_middleware(
        mode=mode,
        project_root=Path("."),
        excluded_tools=excluded or [],
        require_approval=require_approval,
        readonly=readonly,
        limits=limits or PtcLimits(),
        run_code=runner if runner is not None else _scripted_runner(),
    )


def _agent(tools: list[Any], middleware: Any, responses: list[AIMessage]) -> Any:
    return create_agent(
        model=_ToolBindableFakeModel(responses=responses),
        tools=tools,
        middleware=[middleware],
    )


def _run_code_message(result: dict[str, Any]) -> ToolMessage:
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(messages) == 1
    return messages[0]


# --------------------------------------------------------------------------- #
# Registration / visibility
# --------------------------------------------------------------------------- #
def test_both_mode_registers_run_code() -> None:
    middleware = _middleware(mode="both")
    assert [tool_.name for tool_ in middleware.tools] == ["run_code"]


def test_run_code_description_states_read_only_default() -> None:
    """The tool description and SDK must advertise the same read-only default."""
    middleware = _middleware(mode="both")
    description = middleware.tools[0].description

    assert "use it by default for read-only work" in description
    assert "analyzing code, running read-only commands, finding files" in description
    assert "including a single read-only call" in description
    assert "Batch known independent queries" in description
    assert "run them concurrently within the configured limit" in description
    assert "Return to the model when the next step needs interpretation" in description
    assert "Keep state-changing calls native when visible" in description
    assert "never bypass approval or tool restrictions" in description
    assert "not a security sandbox" in description


def test_native_mode_registers_nothing() -> None:
    middleware = _middleware(mode="native")
    assert list(middleware.tools) == []


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        _middleware(mode="sideways")


def test_readonly_never_registers_run_code() -> None:
    middleware = _middleware(readonly=True)
    assert list(middleware.tools) == []


def test_excluded_run_code_is_not_registered() -> None:
    middleware = _middleware(excluded=["run_code"])
    assert list(middleware.tools) == []


class _FakeModelRequest:
    def __init__(self, tools: list[Any], system_message: Any = None, runtime: Any = None) -> None:
        self.tools = list(tools)
        self.system_message = system_message
        self.runtime = runtime

    def override(self, **changes: Any) -> _FakeModelRequest:
        clone = _FakeModelRequest(self.tools, self.system_message, self.runtime)
        for key, value in changes.items():
            setattr(clone, key, value)
        return clone


def _names(request: _FakeModelRequest) -> list[str]:
    return [getattr(tool_, "name", str(tool_)) for tool_ in request.tools]


def test_code_mode_folds_orchestratable_tools() -> None:
    middleware = _middleware(mode="code")
    request = _FakeModelRequest([read_file, write_file, write_todos, mcp_probe])
    out = middleware.wrap_model_call(request, lambda req: req)
    # read_file, write_file and mcp_probe are orchestratable without approval ->
    # folded away; only the session-state tool stays native.
    assert set(_names(out)) == {"write_todos"}
    assert "read_file" in _message_text(out.system_message)


def test_code_mode_keeps_writes_native_under_approval() -> None:
    middleware = _middleware(mode="code", require_approval=True)
    request = _FakeModelRequest([read_file, write_file, mcp_probe])
    out = middleware.wrap_model_call(request, lambda req: req)
    # Only the read is folded; approval-gated writes and unknown-contract tools
    # stay native so their prompt still happens.
    assert set(_names(out)) == {"write_file", "mcp_probe"}


def test_both_mode_keeps_native_tools_visible() -> None:
    middleware = _middleware(mode="both")
    request = _FakeModelRequest([read_file, write_file])
    out = middleware.wrap_model_call(request, lambda req: req)
    assert set(_names(out)) == {"read_file", "write_file"}


def test_sdk_prompt_is_injected_into_system_message() -> None:
    middleware = _middleware(mode="both")
    request = _FakeModelRequest([read_file])
    out = middleware.wrap_model_call(request, lambda req: req)
    text = _message_text(out.system_message)
    assert "Programmatic tool calling" in text
    assert "async def read_file(*, path: str) -> ToolEnvelope:" in text
    assert "use `run_code` by default for read-only work" in text
    assert "single read-only call: use a minimal `run_code` wrapper" in text
    assert "you must batch them in one `run_code`" in text


def test_sdk_prompt_is_added_as_a_new_block() -> None:
    middleware = _middleware(mode="both")
    original = SystemMessage(content="existing system prompt")
    request = _FakeModelRequest([read_file], system_message=original)
    out = middleware.wrap_model_call(request, lambda req: req)
    blocks = out.system_message.content_blocks
    assert blocks[0]["text"] == "existing system prompt"
    assert "Programmatic tool calling" in blocks[-1]["text"]


def test_append_system_prompt_preserves_blocks_and_metadata() -> None:
    original = SystemMessage(
        content=[{"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}],
        additional_kwargs={"marker": "keep"},
    )
    out = _append_system_prompt(original, "SDK")
    blocks = out.content_blocks
    assert blocks[0]["text"] == "stable"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[-1] == {"type": "text", "text": "SDK"}
    assert out.additional_kwargs == {"marker": "keep"}


def test_append_system_prompt_on_none() -> None:
    out = _append_system_prompt(None, "SDK")
    assert out.content_blocks == [{"type": "text", "text": "SDK"}]


def test_append_system_prompt_keeps_the_raw_content_list() -> None:
    # The original blocks are copied verbatim, so a block that
    # ``content_blocks`` would normalise (an ``image_url`` becomes an ``image``
    # block) keeps its exact shape and any cache marker stays put.
    blocks = [
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
        {"type": "image_url", "image_url": {"url": "https://example.test/x.png"}},
    ]
    out = _append_system_prompt(SystemMessage(content=blocks), "SDK")
    assert out.content[0] == blocks[0]
    assert out.content[1] == blocks[1]
    assert out.content[2] == {"type": "text", "text": "SDK"}
    assert len(out.content) == 3


def test_sdk_budget_falls_back_without_truncating(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> str:
        raise sdk.SdkBudgetExceeded(size=99_999, budget=1)

    monkeypatch.setattr(sdk, "build_sdk_prompt", _boom)
    middleware = _middleware(mode="both")
    request = _FakeModelRequest([read_file])
    out = middleware.wrap_model_call(request, lambda req: req)
    text = _message_text(out.system_message)
    assert "exceeded the SDK size budget" in text
    assert "read_file" in text


def test_sdk_budget_fallback_does_not_fold_in_code_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> str:
        raise sdk.SdkBudgetExceeded(size=99_999, budget=1)

    monkeypatch.setattr(sdk, "build_sdk_prompt", _boom)
    middleware = _middleware(mode="code")
    request = _FakeModelRequest([read_file, write_file])
    out = middleware.wrap_model_call(request, lambda req: req)
    # Nothing is folded, so the exact schemas stay visible in the native tools.
    assert set(_names(out)) == {"read_file", "write_file"}
    text = _message_text(out.system_message)
    assert "native tool definition" in text
    assert "stays visible in this request" in text


def test_readonly_refuses_forced_run_code() -> None:
    middleware = _middleware(readonly=True)

    def handler(req: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError("run_code must not reach the handler in read-only mode")

    request = _FakeModelRequest([])
    request.tool_call = {"name": "run_code", "args": {}, "id": "call-1", "type": "tool_call"}
    result = middleware.wrap_tool_call(request, handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "Permission denied" in result.content


def test_excluded_run_code_refuses_forced_call() -> None:
    middleware = _middleware(excluded=["run_code"])

    def handler(req: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError("excluded run_code must not reach the handler")

    request = _FakeModelRequest([])
    request.tool_call = {"name": "run_code", "args": {}, "id": "call-1", "type": "tool_call"}
    result = middleware.wrap_tool_call(request, handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"


def test_readonly_does_not_inject_sdk() -> None:
    middleware = _middleware(readonly=True)
    request = _FakeModelRequest([read_file])
    out = middleware.wrap_model_call(request, lambda req: req)
    assert out is request
    assert out.system_message is None


def test_async_model_call_injects_sdk() -> None:
    middleware = _middleware(mode="both")
    request = _FakeModelRequest([read_file])

    async def handler(req: Any) -> Any:
        return req

    out = asyncio.run(middleware.awrap_model_call(request, handler))
    assert "Programmatic tool calling" in _message_text(out.system_message)


def test_forced_call_to_folded_tool_still_runs() -> None:
    # Folding is a visibility optimisation, not a security boundary: a forged
    # direct call to a folded-but-allowed tool still reaches its handler.
    middleware = _middleware(mode="code")
    request = _FakeModelRequest([])
    request.tool_call = {"name": "read_file", "args": {"path": "a"}, "id": "c", "type": "tool_call"}
    ran: list[str] = []

    def handler(req: Any) -> ToolMessage:
        ran.append(req.tool_call["name"])
        return ToolMessage(content="ok", tool_call_id="c", name="read_file")

    result = middleware.wrap_tool_call(request, handler)
    assert ran == ["read_file"]
    assert result.content == "ok"


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #
def _forced_call(code: Any, intent: Any = "orchestrate") -> _FakeModelRequest:
    request = _FakeModelRequest([])
    request.tool_call = {
        "name": "run_code",
        "args": {"code": code, "intent": intent},
        "id": "call-1",
        "type": "tool_call",
    }
    return request


def test_non_string_code_is_rejected_before_running() -> None:
    middleware = _middleware(mode="both")

    def handler(req: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError("non-string code must not reach the runner")

    result = middleware.wrap_tool_call(_forced_call({"calls": []}), handler)
    assert result.status == "error"
    assert "`code`" in result.content


def test_blank_intent_is_rejected_before_running() -> None:
    middleware = _middleware(mode="both")

    def handler(req: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError("blank intent must not reach the runner")

    result = middleware.wrap_tool_call(_forced_call("return 1", intent="   "), handler)
    assert result.status == "error"


def test_missing_intent_is_rejected() -> None:
    middleware = _middleware(mode="both")
    request = _FakeModelRequest([])
    request.tool_call = {
        "name": "run_code",
        "args": {"code": "return 1"},
        "id": "call-1",
        "type": "tool_call",
    }

    def handler(req: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError

    assert middleware.wrap_tool_call(request, handler).status == "error"


def test_extra_run_code_args_are_rejected() -> None:
    middleware = _middleware(mode="both")

    def handler(req: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError("extra args must not reach the runner")

    request = _FakeModelRequest([])
    request.tool_call = {
        "name": "run_code",
        "args": {"code": "return 1", "intent": "go", "sneaky": "x"},
        "id": "call-1",
        "type": "tool_call",
    }
    result = middleware.wrap_tool_call(request, handler)
    assert result.status == "error"
    assert "no other arguments" in result.content


def test_run_code_args_accepts_empty_code_but_rejects_wrong_types() -> None:
    base = {"name": "run_code", "id": "call-1", "type": "tool_call"}
    # An empty body is allowed; the worker runs it and returns None.
    assert _run_code_args({**base, "args": {"code": "", "intent": "go"}}) == ("", "go")
    # A non-string code, a non-string/blank intent, or an extra key is rejected.
    assert _run_code_args({**base, "args": {"code": 1, "intent": "go"}}) is None
    assert _run_code_args({**base, "args": {"code": "1", "intent": 2}}) is None
    assert _run_code_args({**base, "args": {"code": "1", "intent": "  "}}) is None
    assert _run_code_args({**base, "args": {"code": "1"}}) is None
    assert _run_code_args({**base, "args": {"code": "1", "intent": "go", "extra": 1}}) is None


# --------------------------------------------------------------------------- #
# Result / error messages
# --------------------------------------------------------------------------- #
def test_result_message_is_compact_and_keeps_unicode() -> None:
    middleware = _middleware(mode="both")
    request = _forced_call("return 1")
    message = middleware._result_message(request, {"logs": [], "value": "héllo 世界"})
    assert message.status == "success"
    assert "héllo 世界" in message.content
    assert "\\u" not in message.content  # ensure_ascii=False
    assert ", " not in message.content  # compact separators


def test_error_message_is_bounded() -> None:
    middleware = _middleware(mode="both")
    request = _forced_call("return 1")
    message = middleware._error_message(request, RuntimeError("x" * 50_000))
    assert message.status == "error"
    # The error is the same bounded JSON contract as a normal result.
    payload = json.loads(message.content)
    assert payload["value"] is None
    assert payload["error"]["kind"] == "middleware"
    assert len(message.content.encode("utf-8")) <= PtcLimits().max_output_bytes


def test_error_message_fits_a_tiny_output_cap() -> None:
    # Even at the 256-byte floor the JSON error envelope stays within the cap.
    middleware = _middleware(mode="both", limits=PtcLimits(max_output_bytes=256))
    request = _forced_call("return 1")
    message = middleware._error_message(request, RuntimeError("x" * 50_000))
    assert message.status == "error"
    assert len(message.content.encode("utf-8")) <= 256
    assert json.loads(message.content)["error"]["kind"] == "middleware"


def test_invalid_args_and_refusal_stay_short_text() -> None:
    middleware = _middleware(mode="both")
    request = _forced_call("return 1")
    invalid = middleware._invalid_args_message(request)
    refusal = middleware._refusal(request)
    assert invalid.status == "error" and refusal.status == "error"
    # Short human text (not a JSON envelope), but still length-capped.
    assert len(invalid.content) <= 2_000
    assert len(refusal.content) <= 2_000


def test_result_message_recaps_bridge_warning_over_budget() -> None:
    # The bridge appends a timeout warning *after* the runner returned its
    # already-capped result, so the middleware must re-cap the combined message.
    limits = PtcLimits(max_output_bytes=256)
    middleware = _middleware(mode="both", limits=limits)
    request = _forced_call("return 1")
    result = {
        "logs": ["x" * 5_000],
        "value": {"big": "y" * 5_000},
        "warning": "1 synchronous tool call(s) may still be running in the background",
    }
    message = middleware._result_message(request, result)
    assert message.status == "error"
    assert len(message.content.encode("utf-8")) <= 256
    assert json.loads(message.content)["error"]["kind"] == "output_limit"


def test_result_message_rejects_nan_as_error_not_success() -> None:
    middleware = _middleware(mode="both")
    request = _forced_call("return 1")
    message = middleware._result_message(request, {"logs": [], "value": float("nan")})
    assert message.status == "error"
    assert json.loads(message.content)["error"]["kind"] == "serialize"
    assert "NaN" not in message.content


def test_result_message_rejects_non_serializable_as_error() -> None:
    middleware = _middleware(mode="both")
    request = _forced_call("return 1")
    message = middleware._result_message(request, {"logs": [], "value": object()})
    assert message.status == "error"
    assert json.loads(message.content)["error"]["kind"] == "serialize"


def test_result_message_rejects_non_mapping_as_error() -> None:
    middleware = _middleware(mode="both")
    request = _forced_call("return 1")
    message = middleware._result_message(request, ["not", "a", "mapping"])
    assert message.status == "error"
    assert json.loads(message.content)["error"]["kind"] == "result"


def test_context_passes_all_registered_tool_names() -> None:
    middleware = _middleware(mode="code")
    request = _forced_call("return 1")
    request.runtime = SimpleNamespace(
        tools=[read_file, write_file, write_todos],
        stream_writer=None,
    )
    ctx = middleware._context(request, lambda r: r, offload=False)
    # Every registered name is handed to the sandbox so a denied tool is not
    # masked as unregistered.
    assert set(ctx.tool_names) == {"read_file", "write_file", "write_todos"}


# --------------------------------------------------------------------------- #
# End-to-end dispatch
# --------------------------------------------------------------------------- #
def test_end_to_end_nested_results_and_runtime_id_sync() -> None:
    code = json.dumps(
        {
            "calls": [
                ["read_file", {"path": "a.txt"}],
                ["stat_file", {"path": "b.txt"}],
                ["id_probe", {"path": "c.txt"}],
            ]
        }
    )
    middleware = _middleware()
    agent = _agent(
        [read_file, stat_file, id_probe],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})

    message = _run_code_message(result)
    assert message.status == "success"
    payload = json.loads(message.content)
    assert payload["value"][0] == {
        "content": "contents of a.txt",
        "data": None,
        "truncated": None,
    }
    assert payload["value"][1]["data"] == {"size": 3}
    assert payload["value"][1]["truncated"] is False
    # The child saw its nested id, not the parent's.
    assert payload["value"][2]["content"] == "id=call-1:ptc:2"


def test_end_to_end_async() -> None:
    code = json.dumps({"calls": [["read_file", {"path": "a.txt"}]]})
    middleware = _middleware()
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="go")]}))
    message = _run_code_message(result)
    assert message.status == "success"
    assert json.loads(message.content)["value"][0]["content"] == "contents of a.txt"


def test_only_parent_message_enters_graph_state() -> None:
    code = json.dumps({"calls": [["read_file", {"path": "a.txt"}]]})
    middleware = _middleware()
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["call-1"]
    assert all(":ptc:" not in (m.tool_call_id or "") for m in tool_messages)


def test_code_mode_still_dispatches_folded_tool_through_graph() -> None:
    # Folding only hides the tool from the model; the runtime still exposes every
    # registered tool to the sandbox whitelist, so a folded call still dispatches.
    code = json.dumps({"calls": [["read_file", {"path": "a.txt"}]]})
    middleware = _middleware(mode="code")
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    message = _run_code_message(result)
    assert message.status == "success"
    assert json.loads(message.content)["value"][0]["content"] == "contents of a.txt"


def test_stateful_tool_is_rejected_end_to_end() -> None:
    code = json.dumps({"calls": [["write_todos", {"todos": ["x"]}]]})
    middleware = _middleware()
    agent = _agent(
        [write_todos],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    message = _run_code_message(result)
    assert message.status == "error"
    payload = json.loads(message.content)
    assert payload["error"]["kind"] == "denied"


def test_recursive_run_code_is_rejected_end_to_end() -> None:
    code = json.dumps({"calls": [["run_code", {"code": "1"}]]})
    middleware = _middleware()
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    message = _run_code_message(result)
    assert message.status == "error"
    assert json.loads(message.content)["error"]["kind"] == "denied"


def test_unknown_tool_is_rejected_end_to_end() -> None:
    code = json.dumps({"calls": [["mystery", {}]]})
    middleware = _middleware()
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    message = _run_code_message(result)
    assert message.status == "error"
    assert json.loads(message.content)["error"]["kind"] == "unknown"


def test_excluded_tool_is_rejected_from_code_end_to_end() -> None:
    code = json.dumps({"calls": [["read_file", {"path": "a.txt"}]]})
    middleware = _middleware(excluded=["read_file"])
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    message = _run_code_message(result)
    assert message.status == "error"
    assert json.loads(message.content)["error"]["kind"] == "denied"


def test_require_approval_rejects_nested_approval_and_unknown() -> None:
    for name, args in (("write_file", {"path": "a", "content": "x"}), ("mcp_probe", {"q": "hi"})):
        code = json.dumps({"calls": [[name, args]]})
        agent = _agent(
            [read_file, write_file, mcp_probe],
            _middleware(require_approval=True),
            [_run_code_response(code), AIMessage(content="done")],
        )
        result = agent.invoke({"messages": [HumanMessage(content="go")]})
        message = _run_code_message(result)
        assert message.status == "error"
        assert "native tool" in message.content


def test_concurrent_reads_through_graph() -> None:
    code = json.dumps({"calls": [["read_file", {"path": "a"}], ["read_file", {"path": "b"}]]})
    middleware = _middleware(runner=_scripted_runner(concurrent=True))
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="go")]}))
    message = _run_code_message(result)
    assert message.status == "success"
    assert len(json.loads(message.content)["value"]) == 2


def test_stream_events_are_emitted() -> None:
    code = json.dumps({"calls": [["read_file", {"path": "a.txt", "api_key": "SECRET"}]]})
    middleware = _middleware()
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )

    async def collect() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for mode, chunk in agent.astream(
            {"messages": [HumanMessage(content="go")]}, stream_mode=["custom"]
        ):
            if mode == "custom" and isinstance(chunk, dict) and chunk.get("type") == "ptc_tool":
                events.append(chunk)
        return events

    events = asyncio.run(collect())
    assert [event["event"] for event in events] == ["started", "finished"]
    assert events[0]["name"] == "read_file"
    assert events[0]["parent_call_id"] == "call-1"
    assert events[0]["call_id"] == "call-1:ptc:0"
    assert "api_key" not in events[0]["args"]
    assert events[1]["status"] == "success"


# --------------------------------------------------------------------------- #
# Real process module
# --------------------------------------------------------------------------- #
def test_real_process_run_code_with_sdk_stub() -> None:
    process = pytest.importorskip("synapse.runtime.ptc.process")
    limits = PtcLimits(timeout_seconds=30.0)
    middleware = build_ptc_middleware(
        mode="both",
        project_root=Path("."),
        excluded_tools=[],
        require_approval=False,
        readonly=False,
        limits=limits,
        run_code=process.run_code,
    )
    # The typed stub form the SDK advertises: keyword args + await.
    code = "result = await tools.read_file(path='a.txt')\nreturn result"
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    message = _run_code_message(result)
    assert message.status == "success"
    payload = json.loads(message.content)
    assert payload["value"]["content"] == "contents of a.txt"
    assert payload["value"]["data"] is None


def test_real_process_run_code_with_generic_call() -> None:
    process = pytest.importorskip("synapse.runtime.ptc.process")
    limits = PtcLimits(timeout_seconds=30.0)
    middleware = build_ptc_middleware(
        mode="both",
        project_root=Path("."),
        excluded_tools=[],
        require_approval=False,
        readonly=False,
        limits=limits,
        run_code=process.run_code,
    )
    code = "return await tools.call('read_file', {'path': 'a.txt'})"
    agent = _agent(
        [read_file],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    message = _run_code_message(result)
    assert message.status == "success"
    assert json.loads(message.content)["value"]["content"] == "contents of a.txt"


def test_sync_timeout_does_not_wait_for_blocking_tool() -> None:
    process = pytest.importorskip("synapse.runtime.ptc.process")
    entered = threading.Event()

    @tool
    def slow_tool(seconds: int = 30) -> str:
        """Block for ``seconds`` (a tool that ignores cancellation)."""
        entered.set()
        time.sleep(seconds)
        return "done"

    # A generous timeout so the dispatch is reliably reached even on a cold,
    # loaded machine; the 30s handler is far beyond it either way.
    limits = PtcLimits(timeout_seconds=4.0)
    middleware = build_ptc_middleware(
        mode="both",
        project_root=Path("."),
        excluded_tools=[],
        require_approval=False,
        readonly=False,
        limits=limits,
        run_code=process.run_code,
    )
    code = "return await tools.slow_tool(seconds=30)"
    agent = _agent(
        [slow_tool],
        middleware,
        [_run_code_response(code), AIMessage(content="done")],
    )
    start = time.monotonic()
    result = agent.invoke({"messages": [HumanMessage(content="go")]})
    elapsed = time.monotonic() - start
    # The run must return at the timeout, not after the 30s sleep.
    assert elapsed < 25, f"run blocked for {elapsed:.1f}s on a blocking sync tool"
    message = _run_code_message(result)
    assert message.status == "error"
    payload = json.loads(message.content)
    assert payload["error"]["kind"] == "timeout"
    # The blocking handler really was dispatched (and cannot be killed).
    assert entered.is_set()
    # The result must not pretend the sync handler stopped.
    assert "may still be running" in payload.get("warning", "")


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
def _run_code_request(code: str, tools: list[Any], call_id: str = "call-1") -> _FakeModelRequest:
    """A forced ``run_code`` request whose runtime exposes ``tools``."""
    request = _FakeModelRequest([])
    request.runtime = SimpleNamespace(tools=list(tools), stream_writer=None)
    request.tool_call = {
        "name": "run_code",
        "args": {"code": code, "intent": "orchestrate"},
        "id": call_id,
        "type": "tool_call",
    }
    return request


def test_preflight_refusal_never_starts_the_runner() -> None:
    started: list[str] = []

    async def runner(*, code, tool_names, dispatch, cwd, limits, **kwargs):  # noqa: ARG001
        started.append(code)
        return {"logs": [], "value": None}

    middleware = _middleware(mode="both", runner=runner)
    request = _run_code_request("return await tools.find_files(pattern='**/*.py')", [read_file])

    def handler(req: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError("a pre-flight refusal must not reach the handler")

    result = middleware.wrap_tool_call(request, handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    payload = json.loads(result.content)
    # Same JSON error contract as every other run_code failure.
    assert payload["logs"] == []
    assert payload["value"] is None
    assert payload["error"]["kind"] == "unknown_tool"
    assert "find_files" in payload["error"]["message"]
    # The refusal names the tools that *are* callable (bounded allowlist).
    assert "read_file" in payload["error"]["message"]
    # The subprocess runner was never started.
    assert started == []


def test_async_preflight_refusal_never_starts_the_runner() -> None:
    started: list[str] = []

    async def runner(*, code, tool_names, dispatch, cwd, limits, **kwargs):  # noqa: ARG001
        started.append(code)
        return {"logs": [], "value": None}

    middleware = _middleware(mode="both", runner=runner)
    request = _run_code_request("return await tools.find_files(pattern='**/*.py')", [read_file])

    async def handler(req: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError("a pre-flight refusal must not reach the handler")

    result = asyncio.run(middleware.awrap_tool_call(request, handler))
    assert result.status == "error"
    assert json.loads(result.content)["error"]["kind"] == "unknown_tool"
    assert started == []


def test_available_tool_script_is_not_preflighted() -> None:
    started: list[str] = []

    async def runner(*, code, tool_names, dispatch, cwd, limits, **kwargs):  # noqa: ARG001
        started.append(code)
        return {"logs": [], "value": "ok"}

    middleware = _middleware(mode="both", runner=runner)
    code = "return await tools.read_file(path='a.txt')"
    result = middleware.wrap_tool_call(_run_code_request(code, [read_file]), lambda r: None)
    assert result.status == "success"
    assert started == [code]


def test_dynamic_tool_access_is_not_preflighted() -> None:
    started: list[str] = []

    async def runner(*, code, tool_names, dispatch, cwd, limits, **kwargs):  # noqa: ARG001
        started.append(code)
        return {"logs": [], "value": None}

    middleware = _middleware(mode="both", runner=runner)
    code = "name = 'find_files'\nreturn await getattr(tools, name)(pattern='**/*.py')"
    result = middleware.wrap_tool_call(_run_code_request(code, [read_file]), lambda r: None)
    # A dynamic reference is left to the runtime, so the runner still runs.
    assert result.status == "success"
    assert started == [code]


def test_code_mode_folding_uses_the_bridge_allowlist() -> None:
    middleware = _middleware(mode="code")
    request = _FakeModelRequest([read_file, write_file, write_todos, mcp_probe])
    out = middleware.wrap_model_call(request, lambda req: req)
    # Session-state tools stay native; everything callable from code is folded.
    assert set(_names(out)) == {"write_todos"}
    text = _message_text(out.system_message)
    assert "async def read_file" in text
    assert "async def write_todos" not in text
