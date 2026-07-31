"""Tests for estimated token savings reused by later model calls."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from synapse.runtime.tool_output_usage_middleware import build_tool_output_usage_middleware
from synapse.tool_output import ToolOutputRepository, TransformEvent


def test_model_reuse_records_transformed_tool_token_saving(tmp_path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    repo.record_event(
        "thread-a",
        TransformEvent(
            content_type="text",
            transformer="generic-v1",
            outcome="transformed",
            original_bytes=1000,
            visible_bytes=200,
            duration_ms=1.0,
            critical_total=1,
            critical_retained=1,
            ref_created=True,
            estimated_original_tokens=300,
            estimated_visible_tokens=80,
        ),
        ref="tool-output://ref",
    )
    message = ToolMessage(content="compressed", tool_call_id="call-1", name="execute")
    message.artifact = {
        "tool_output_transform": {
            "estimated_saved_tokens": 220,
        }
    }
    request = SimpleNamespace(
        runtime=SimpleNamespace(config={"configurable": {"thread_id": "thread-a"}}),
        state={"messages": [message]},
    )

    middleware = build_tool_output_usage_middleware(repo)
    middleware.wrap_model_call(request, lambda value: value)

    stats = repo.stats(thread_id="thread-a")
    assert stats["estimated_saved_tokens"] == 220
    assert stats["estimated_reused_tokens"] == 220
