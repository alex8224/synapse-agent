"""Tests for model request compression accounting and provider safety diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

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
