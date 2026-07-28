"""LangChain middleware that rewrites large tool outputs through tool_output."""

from __future__ import annotations

import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from synapse.execute_capture import begin_execute_capture, end_execute_capture
from synapse.tool_output import (
    ToolOutputRepository,
    ToolOutputTransformPipeline,
    TransformContext,
    TransformEvent,
    content_to_text,
)


def build_tool_output_transform_middleware(
    repository: ToolOutputRepository,
    *,
    threshold_bytes: int = 512,
    pipeline: ToolOutputTransformPipeline | None = None,
):
    """Rewrite large outputs once, preserving originals only when needed.

    The middleware owns all result rewriting for regular, async, and Command
    result paths. ``execute_capture`` remains in use so an already-truncated
    backend message can be transformed from its complete captured output.
    """
    threshold = max(0, int(threshold_bytes))
    pipeline = pipeline or ToolOutputTransformPipeline()
    excluded = frozenset({"read_tool_result", "compact_conversation"})

    def runtime_identity(request: Any) -> tuple[str, str]:
        config = dict(getattr(request.runtime, "config", None) or {})
        try:
            from langchain_core.runnables.config import get_config

            active = get_config()
            if active:
                config = dict(active)
        except (RuntimeError, ImportError):
            pass
        configurable = dict(config.get("configurable") or {})
        return (
            str(configurable.get("thread_id") or "unknown-thread"),
            str(configurable.get("checkpoint_ns") or ""),
        )

    def call_value(request: Any, key: str, default: str = "") -> str:
        call = getattr(request, "tool_call", None)
        if isinstance(call, dict):
            return str(call.get(key) or default)
        return str(getattr(call, key, None) or default)

    def current_query(request: Any) -> str:
        """Best-effort latest human text without changing graph state."""
        runtime = getattr(request, "runtime", None)
        state = getattr(runtime, "state", None)
        messages = state.get("messages") if isinstance(state, dict) else None
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            role = getattr(message, "type", None) or getattr(message, "role", None)
            if role not in {"human", "user"}:
                continue
            content = content_to_text(getattr(message, "content", ""))
            if content.strip():
                return content
        return ""

    def rewrite_message(
        request: Any,
        message: ToolMessage,
        *,
        original_content: str | None = None,
        execute_output_truncated: bool = False,
    ) -> ToolMessage:
        name = str(message.name or call_value(request, "name", "tool"))
        if name in excluded:
            return message
        original = (
            original_content if original_content is not None else content_to_text(message.content)
        )
        original_bytes = len(original.encode("utf-8"))
        # A failed tool call is itself diagnostic context. Keep it intact rather
        # than risking removal of an argument, exit code, or traceback detail.
        if str(message.status or "success") == "error":
            return message
        if original_bytes <= threshold and not execute_output_truncated:
            return message

        started = time.perf_counter()
        try:
            transformed = pipeline.transform(
                original,
                TransformContext(
                    tool_name=name,
                    status=str(message.status or "success"),
                    query=current_query(request),
                ),
            )
        except Exception:  # noqa: BLE001
            return message
        thread_id, checkpoint_ns = runtime_identity(request)
        if transformed.content == original:
            repository.record_event(
                thread_id,
                TransformEvent(
                    content_type=transformed.content_type.value,
                    transformer=transformed.transformer,
                    outcome="passthrough",
                    original_bytes=original_bytes,
                    visible_bytes=original_bytes,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    critical_total=transformed.critical_total,
                    critical_retained=transformed.critical_retained,
                    ref_created=False,
                    execution_path=str(transformed.metadata.get("execution_path", "passthrough")),
                ),
            )
            return message

        try:
            record = repository.put(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                tool_call_id=str(message.tool_call_id or call_value(request, "id")),
                tool_name=name,
                status=str(message.status or "success"),
                content=original,
            )
        except Exception:  # noqa: BLE001
            return message

        visible_bytes = len(transformed.content.encode("utf-8"))
        event = TransformEvent(
            content_type=transformed.content_type.value,
            transformer=transformed.transformer,
            outcome="transformed",
            original_bytes=original_bytes,
            visible_bytes=visible_bytes,
            duration_ms=(time.perf_counter() - started) * 1000,
            critical_total=transformed.critical_total,
            critical_retained=transformed.critical_retained,
            ref_created=True,
            execution_path=str(transformed.metadata.get("execution_path", "python_only")),
        )
        repository.record_event(thread_id, event, ref=record.ref)
        metadata = dict(message.artifact) if isinstance(message.artifact, dict) else {}
        metadata["tool_output_transform"] = {
            **event.as_dict(),
            "ref": record.ref,
            "sha256": record.sha256,
            **transformed.metadata,
        }
        if execute_output_truncated:
            metadata["tool_output_contains_untruncated_execute_output"] = True
        message.artifact = metadata
        message.content = (
            "[tool output transformed]\n"
            f"tool: {name}\n"
            f"type: {transformed.content_type.value}\n"
            f"transformer: {transformed.transformer}\n"
            f"ref: {record.ref}\n"
            f"original_bytes: {original_bytes}\n"
            f"visible_bytes: {visible_bytes}\n"
            f"content:\n{transformed.content}\n\n"
            "Use read_tool_result(ref=..., query=...) for targeted retrieval, "
            "or offset/limit for exact lines."
        )
        return message

    def rewrite_result(
        request: Any,
        result: Any,
        *,
        original_content: str | None = None,
        execute_output_truncated: bool = False,
    ) -> Any:
        if isinstance(result, ToolMessage):
            return rewrite_message(
                request,
                result,
                original_content=original_content,
                execute_output_truncated=execute_output_truncated,
            )
        if isinstance(result, Command):
            update = result.update
            messages = update.get("messages") if isinstance(update, dict) else None
            if not isinstance(messages, list):
                return result
            rewritten = [
                rewrite_message(request, item) if isinstance(item, ToolMessage) else item
                for item in messages
            ]
            return Command(
                graph=result.graph,
                update={**update, "messages": rewritten},
                resume=result.resume,
                goto=result.goto,
            )
        if isinstance(result, list):
            return [rewrite_result(request, item) for item in result]
        return result

    def is_execute(request: Any) -> bool:
        return call_value(request, "name") == "execute"

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ARG001
        if not is_execute(request):
            return rewrite_result(request, handler(request))
        capture, token = begin_execute_capture()
        try:
            result = handler(request)
        finally:
            end_execute_capture(token)
        return rewrite_result(
            request,
            result,
            original_content=capture.full_output,
            execute_output_truncated=capture.truncated,
        )

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001, ARG001
        if not is_execute(request):
            return rewrite_result(request, await handler(request))
        capture, token = begin_execute_capture()
        try:
            result = await handler(request)
        finally:
            end_execute_capture(token)
        return rewrite_result(
            request,
            result,
            original_content=capture.full_output,
            execute_output_truncated=capture.truncated,
        )

    return type(
        "transform_tool_outputs",
        (AgentMiddleware,),
        {
            "state_schema": AgentState,
            "tools": [],
            "wrap_tool_call": wrap_tool_call,
            "awrap_tool_call": awrap_tool_call,
        },
    )()
