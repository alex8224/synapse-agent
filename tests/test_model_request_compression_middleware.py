"""Tests for model request compression accounting and provider safety diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables.config import var_child_runnable_config

from synapse.interaction_ledger import clear_interaction_positions
from synapse.middleware import build_strip_redundant_prompt_blocks
from synapse.model_request_compression_middleware import (
    build_model_request_compression_middleware,
)
from synapse.tool_output import ToolOutputRepository


class _Request:
    def __init__(self, messages, *, model=None, system_message=None, model_settings=None):
        self.messages = messages
        self.system_message = system_message
        self.tools = []
        self.model = model or SimpleNamespace(model_name="gpt-5")
        self.model_settings = model_settings or {}
        self.state = {"messages": messages}
        self.runtime = SimpleNamespace(
            config={"configurable": {"thread_id": "thread-a"}}
        )

    def override(self, **values):
        clone = _Request(
            values.get("messages", self.messages),
            model=values.get("model", self.model),
            system_message=values.get("system_message", self.system_message),
            model_settings=values.get("model_settings", self.model_settings),
        )
        clone.tools = values.get("tools", self.tools)
        clone.state = self.state
        clone.runtime = self.runtime
        return clone


def test_request_ledger_records_before_after_usage_and_tool_savings(tmp_path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    tool = ToolMessage(content="compressed output", tool_call_id="call-1", name="execute")
    tool.artifact = {
        "tool_output_transform": {
            "decision": "transformed",
            "estimated_saved_tokens": 220,
        }
    }
    request = _Request(
        [HumanMessage(content="inspect"), tool],
        system_message=SystemMessage(content="system"),
    )
    response = SimpleNamespace(
        result=[
            AIMessage(
                content="done",
                usage_metadata={
                    "input_tokens": 500,
                    "output_tokens": 20,
                    "total_tokens": 520,
                    "input_token_details": {"cache_read": 400},
                },
            )
        ]
    )

    middleware = build_model_request_compression_middleware(repo)
    result = middleware.wrap_model_call(request, lambda _request: response)

    assert result is response
    events = repo.model_request_events(thread_id="thread-a")
    assert len(events) == 1
    event = events[0]
    expected_after = count_tokens_approximately(
        [request.system_message, *request.messages], tools=[]
    )
    assert event["input_tokens_after"] == expected_after
    assert event["input_tokens_before"] == expected_after + 220
    assert event["tool_output_saved_tokens"] == 220
    assert event["prompt_saved_tokens"] == 0
    assert event["provider_input_tokens"] == 500
    assert event["cache_read_tokens"] == 400
    assert event["uncached_input_tokens"] == 100
    assert event["output_tokens"] == 20
    breakdown = event["content_breakdown"]
    assert breakdown["system"] > 0
    assert breakdown["current_user"] > 0
    assert breakdown["tool_output_visible"] > 0
    assert breakdown["tool_output_original"] > breakdown["tool_output_visible"]
    opportunities = event["opportunity_tokens_by_reason"]
    assert opportunities["current_user_not_in_pipeline"] > 0


def test_request_ledger_uses_active_runnable_config_when_runtime_has_no_config(
    tmp_path,
) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    request = _Request([HumanMessage(content="inspect")])
    request.runtime = SimpleNamespace()
    response = SimpleNamespace(result=[AIMessage(content="done")])
    middleware = build_model_request_compression_middleware(repo)
    token = var_child_runnable_config.set(
        {"configurable": {"thread_id": "thread-from-runnable"}}
    )
    try:
        middleware.wrap_model_call(request, lambda _request: response)
    finally:
        var_child_runnable_config.reset(token)

    events = repo.model_request_events(thread_id="thread-from-runnable")
    assert len(events) == 1
    assert events[0]["content_breakdown"]["current_user"] > 0


def test_request_content_breakdown_classifies_history_reasoning_args_and_schemas(
    tmp_path,
) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    assistant = AIMessage(
        content="assistant answer",
        additional_kwargs={"reasoning_content": "reason" * 100},
        tool_calls=[{"id": "call-1", "name": "write_file", "args": {"content": "x" * 800}}],
    )
    request = _Request(
        [
            HumanMessage(content="historical request"),
            assistant,
            HumanMessage(content="current request" * 50),
        ],
        system_message=SystemMessage(content="system instructions" * 20),
    )
    request.tools = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "write content" * 30,
                "parameters": {"type": "object", "properties": {"content": {"type": "string"}}},
            },
        }
    ]
    response = SimpleNamespace(result=[AIMessage(content="done")])

    middleware = build_model_request_compression_middleware(repo)
    middleware.wrap_model_call(request, lambda _request: response)

    event = repo.model_request_events(thread_id="thread-a")[0]
    breakdown = event["content_breakdown"]
    assert breakdown["system"] > 0
    assert breakdown["tool_schemas"] > 0
    assert breakdown["historical_user"] > 0
    assert breakdown["current_user"] > breakdown["historical_user"]
    assert breakdown["assistant_content"] > 0
    assert breakdown["reasoning"] > 0
    assert breakdown["tool_call_arguments"] > 0
    opportunities = event["opportunity_tokens_by_reason"]
    assert opportunities["tool_schema_fixed_overhead"] > 0
    assert opportunities["reasoning_not_in_pipeline"] > 0
    assert opportunities["tool_call_arguments_not_in_pipeline"] > 0


def test_turn_live_zone_wire_and_schema_profiles(tmp_path) -> None:
    clear_interaction_positions()
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    middleware = build_model_request_compression_middleware(repo)
    response = SimpleNamespace(
        result=[
            AIMessage(
                content="done",
                usage_metadata={
                    "input_tokens": 1000,
                    "output_tokens": 20,
                    "total_tokens": 1020,
                    "input_token_details": {"cache_read": 100},
                },
            )
        ]
    )
    first = _Request([HumanMessage(content="turn one")])
    first.tools = [
        {
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "description" * 30,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    middleware.wrap_model_call(first, lambda _request: response)
    middleware.wrap_model_call(first, lambda _request: response)
    second = _Request(
        [
            HumanMessage(content="turn one"),
            AIMessage(content="done"),
            HumanMessage(content="turn two"),
        ]
    )
    second.tools = first.tools
    middleware.wrap_model_call(second, lambda _request: response)

    events = list(reversed(repo.model_request_events(thread_id="thread-a")))
    assert [item["turn_index"] for item in events] == [1, 1, 2]
    assert [item["model_call_index"] for item in events] == [1, 2, 1]
    assert events[0]["turn_id"] == events[1]["turn_id"]
    assert events[2]["turn_id"] != events[1]["turn_id"]
    assert events[0]["live_zone_tokens"]["live"] > 0
    assert events[0]["wire_fingerprints"]["tools_hash"]
    assert events[0]["tool_schema_profiles"][0]["tool_name"] == "large_tool"
    assert events[1]["cache_diagnostics"]["previous_request_available"] is True
    assert events[1]["cache_diagnostics"]["cache_bust_suspected"] is True


def test_read_lifecycle_classifies_historical_read_as_frozen(tmp_path) -> None:
    clear_interaction_positions()
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    middleware = build_model_request_compression_middleware(repo)
    read_call = AIMessage(
        content="",
        tool_calls=[
            {"id": "read-1", "name": "read_file", "args": {"file_path": "/src/app.py"}}
        ],
    )
    read_result = ToolMessage(
        content="original source",
        tool_call_id="read-1",
        name="read_file",
    )
    edit_call = AIMessage(
        content="",
        tool_calls=[
            {"id": "edit-1", "name": "edit_file", "args": {"file_path": "/src/app.py"}}
        ],
    )
    edit_result = ToolMessage(content="edited", tool_call_id="edit-1", name="edit_file")
    request = _Request(
        [
            HumanMessage(content="first"),
            read_call,
            read_result,
            edit_call,
            edit_result,
            HumanMessage(content="continue"),
        ]
    )
    response = SimpleNamespace(result=[AIMessage(content="done")])

    seen: list[_Request] = []
    middleware.wrap_model_call(request, lambda prepared: seen.append(prepared) or response)

    assert seen[0].messages[2].content == "original source"
    event = repo.model_request_events(thread_id="thread-a")[0]
    lifecycle = event["cache_diagnostics"]["read_lifecycle"]
    assert lifecycle[0]["state"] == "stale"
    assert lifecycle[0]["zone"] == "frozen"
    assert lifecycle[0]["replaceable"] is False


def test_openai_and_codex_provider_safety_classification(tmp_path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    messages = [
        ToolMessage(content="old" * 100, tool_call_id="old", name="execute"),
        ToolMessage(content="new" * 100, tool_call_id="new", name="execute"),
        AIMessage(content="", additional_kwargs={"encrypted_content": "x" * 400}),
    ]
    model = SimpleNamespace(
        model_name="gpt-5-codex",
        base_url="https://chatgpt.com/backend-api/codex",
    )
    request = _Request(messages, model=model)
    response = SimpleNamespace(result=[AIMessage(content="done")])

    middleware = build_model_request_compression_middleware(repo)
    middleware.wrap_model_call(request, lambda _request: response)

    event = repo.model_request_events(thread_id="thread-a")[0]
    assert event["provider"] == "openai"
    assert event["api_style"] == "responses"
    assert event["auth_mode"] == "subscription"
    protected = event["protected_tokens_by_reason"]
    assert protected["codex_historical_output"] > 0
    assert protected["codex_reasoning_protected"] > 0


def test_anthropic_cache_control_marks_frozen_prefix(tmp_path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    cached = HumanMessage(
        content=[
            {"type": "text", "text": "cached prefix"},
            {"type": "text", "text": "boundary", "cache_control": {"type": "ephemeral"}},
        ]
    )
    latest = HumanMessage(content="latest")
    model = type("ChatAnthropic", (), {"model": "claude-sonnet-4"})()
    request = _Request([cached, latest], model=model)
    response = SimpleNamespace(result=[AIMessage(content="done")])

    middleware = build_model_request_compression_middleware(repo)
    middleware.wrap_model_call(request, lambda _request: response)

    event = repo.model_request_events(thread_id="thread-a")[0]
    assert event["provider"] == "anthropic"
    assert event["protected_tokens_by_reason"]["anthropic_before_cache_control"] > 0


def test_prompt_cleanup_is_attributed_without_leaking_model_settings(tmp_path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    system = SystemMessage(
        content_blocks=[
            {"type": "text", "text": "keep"},
            {"type": "text", "text": "\n\n## `write_todos`" + (" docs" * 100)},
        ]
    )
    request = _Request([HumanMessage(content="go")], system_message=system)
    cleanup = build_strip_redundant_prompt_blocks()
    ledger = build_model_request_compression_middleware(repo)
    response = SimpleNamespace(result=[AIMessage(content="done")])

    cleanup.wrap_model_call(
        request,
        lambda cleaned: ledger.wrap_model_call(cleaned, lambda _request: response),
    )

    event = repo.model_request_events(thread_id="thread-a")[0]
    assert event["prompt_saved_tokens"] > 0
    assert "_synapse_prompt_saved_tokens" not in request.model_settings