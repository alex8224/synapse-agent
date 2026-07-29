"""LangChain middleware that rewrites large tool outputs through tool_output."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from synapse.runtime.execute_capture import begin_execute_capture, end_execute_capture
from synapse.runtime.interaction_ledger import current_position
from synapse.tool_output.models import CompressionStageEvent, TransformContext, TransformEvent
from synapse.tool_output.pipeline import ToolOutputTransformPipeline
from synapse.tool_output.repository import ToolOutputRepository, content_to_text


def _estimate_tokens(content: str) -> int:
    """Estimate model-visible tokens using the same approximation as compaction."""
    try:
        from langchain_core.messages import ToolMessage
        from langchain_core.messages.utils import count_tokens_approximately

        return max(
            0,
            int(
                count_tokens_approximately(
                    [ToolMessage(content=content, tool_call_id="estimate", name="tool")]
                )
            ),
        )
    except Exception:  # noqa: BLE001
        # Conservative fallback for environments without LangChain utilities.
        return max(0, (len(content) + 3) // 4)



def build_tool_output_transform_middleware(
    repository: ToolOutputRepository,
    *,
    threshold_bytes: int = 512,
    pipeline: ToolOutputTransformPipeline | None = None,
    enabled: bool = True,
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

    def call_args(request: Any) -> dict[str, Any]:
        call = getattr(request, "tool_call", None)
        value = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
        return dict(value) if isinstance(value, dict) else {}

    def summarized_args(args: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key, value in args.items():
            if key in {"content", "new_string", "old_string"}:
                text = str(value or "")
                summary[key] = {
                    "bytes": len(text.encode("utf-8")),
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                }
            elif isinstance(value, str):
                summary[key] = value if len(value) <= 500 else value[:500] + "..."
            elif isinstance(value, int | float | bool | type(None)):
                summary[key] = value
            else:
                summary[key] = str(value)[:500]
        return summary

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
        original_tokens = _estimate_tokens(original)
        status = str(message.status or "success")
        thread_id, checkpoint_ns = runtime_identity(request)
        tool_call_id = str(message.tool_call_id or call_value(request, "id"))
        message_id = str(getattr(message, "id", None) or "")
        content_sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()
        started = time.perf_counter()

        def record_decision(
            *,
            decision: str,
            reason_code: str,
            content_type: str = "unknown",
            transformer: str = "none",
            visible_content: str | None = None,
            algorithm_output: str | None = None,
            eligible: bool = False,
            ref: str | None = None,
            critical_total: int = 0,
            critical_retained: int = 0,
            execution_path: str = "not_run",
            confidence: float = 0.0,
            reason_detail: str = "",
            stages: tuple[CompressionStageEvent, ...] = (),
        ) -> TransformEvent:
            visible = original if visible_content is None else visible_content
            algorithm = visible if algorithm_output is None else algorithm_output
            event = TransformEvent(
                content_type=content_type,
                transformer=transformer,
                outcome="transformed" if decision == "transformed" else "passthrough",
                original_bytes=original_bytes,
                visible_bytes=len(visible.encode("utf-8")),
                duration_ms=(time.perf_counter() - started) * 1000,
                critical_total=critical_total,
                critical_retained=critical_retained,
                ref_created=bool(ref),
                execution_path=execution_path,
                estimated_original_tokens=original_tokens,
                estimated_visible_tokens=_estimate_tokens(visible),
                decision=decision,
                reason_code=reason_code,
                reason_detail=reason_detail,
                eligible=eligible,
                detection_confidence=confidence,
                threshold_bytes=threshold,
                tool_call_id=tool_call_id,
                tool_name=name,
                status=status,
                checkpoint_ns=checkpoint_ns,
                message_id=message_id,
                algorithm_output_bytes=len(algorithm.encode("utf-8")),
                algorithm_output_tokens=_estimate_tokens(algorithm),
                content_sha256=content_sha256,
                stages=stages,
            )
            repository.record_event(thread_id, event, ref=ref)
            return event

        if not enabled:
            record_decision(
                decision="skipped",
                reason_code="global_disabled",
                reason_detail="tool-output transformation is disabled by configuration",
                stages=(
                    CompressionStageEvent(
                        phase="eligibility",
                        algorithm="feature-flag-policy",
                        applied=False,
                        reason_code="global_disabled",
                        input_bytes=original_bytes,
                        output_bytes=original_bytes,
                        input_tokens=original_tokens,
                        output_tokens=original_tokens,
                    ),
                ),
            )
            return message
        compress_error_output = status == "error" and original_bytes > threshold
        if status == "error" and not compress_error_output:
            record_decision(
                decision="skipped",
                reason_code="error_output_protected",
                reason_detail="small failed tool result remains intact for diagnostics",
                stages=(
                    CompressionStageEvent(
                        phase="eligibility",
                        algorithm="tool-status-policy",
                        applied=False,
                        reason_code="error_output_protected",
                        input_bytes=original_bytes,
                        output_bytes=original_bytes,
                        input_tokens=original_tokens,
                        output_tokens=original_tokens,
                    ),
                ),
            )
            return message
        if original_bytes <= threshold and not execute_output_truncated:
            record_decision(
                decision="skipped",
                reason_code="below_threshold",
                reason_detail=f"{original_bytes} bytes <= {threshold} byte threshold",
                stages=(
                    CompressionStageEvent(
                        phase="eligibility",
                        algorithm="byte-threshold-v1",
                        applied=False,
                        reason_code="below_threshold",
                        input_bytes=original_bytes,
                        output_bytes=original_bytes,
                        input_tokens=original_tokens,
                        output_tokens=original_tokens,
                        metadata={"threshold_bytes": threshold},
                    ),
                ),
            )
            return message

        args = call_args(request)
        file_path = str(args.get("file_path") or args.get("path") or "")
        file_suffix = Path(file_path).suffix.casefold() if file_path else ""
        try:
            transformed = pipeline.transform(
                original,
                TransformContext(
                    tool_name=name,
                    status=status,
                    query=current_query(request),
                    tool_args=args,
                    file_path=file_path,
                    file_suffix=file_suffix,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            record_decision(
                decision="fallback",
                reason_code="transform_error",
                eligible=True,
                reason_detail=type(exc).__name__,
                stages=(
                    CompressionStageEvent(
                        phase="transform",
                        algorithm="tool-output-pipeline",
                        applied=False,
                        reason_code="transform_error",
                        input_bytes=original_bytes,
                        output_bytes=original_bytes,
                        input_tokens=original_tokens,
                        output_tokens=original_tokens,
                        metadata={"error": type(exc).__name__},
                    ),
                ),
            )
            return message
        confidence = float(transformed.metadata.get("detection_confidence", 0.0) or 0.0)
        execution_path = str(transformed.metadata.get("execution_path", "python_only"))
        fallback_reason = str(transformed.metadata.get("fallback") or "no_byte_savings")
        if transformed.content == original:
            skipped_reasons = {"disabled", "fresh_read_source_protected"}
            decision = "skipped" if fallback_reason in skipped_reasons else "fallback"
            reason_code = (
                "disabled_content_type" if fallback_reason == "disabled" else fallback_reason
            )
            record_decision(
                decision=decision,
                reason_code=reason_code,
                content_type=transformed.content_type.value,
                transformer=transformed.transformer,
                eligible=decision == "fallback",
                critical_total=transformed.critical_total,
                critical_retained=transformed.critical_retained,
                execution_path=execution_path,
                confidence=confidence,
                stages=transformed.stages,
            )
            return message

        algorithm_bytes = len(transformed.content.encode("utf-8"))
        provisional_ref = "tool-output://" + ("0" * 32)
        envelope_template = (
            "[tool output transformed]\n"
            f"tool: {name}\n"
            f"type: {transformed.content_type.value}\n"
            f"transformer: {transformed.transformer}\n"
            f"ref: {provisional_ref}\n"
            f"original_bytes: {original_bytes}\n"
            f"visible_bytes: {algorithm_bytes}\n"
            f"content:\n{transformed.content}\n\n"
            "Use read_tool_result(ref=..., query=...) for targeted retrieval, "
            "or offset/limit for exact lines."
        )
        envelope_tokens = _estimate_tokens(envelope_template)
        saved_tokens = max(0, original_tokens - envelope_tokens)
        savings_ratio = saved_tokens / original_tokens if original_tokens else 0.0
        diff_effective = (
            transformed.content_type.value != "diff"
            or saved_tokens >= 128
            or (saved_tokens >= 32 and savings_ratio >= 0.05)
        )
        token_guard_accepted = envelope_tokens < original_tokens and diff_effective
        token_guard_reason = (
            "accepted"
            if token_guard_accepted
            else (
                "insufficient_effective_savings"
                if envelope_tokens < original_tokens
                else "envelope_erased_savings"
            )
        )
        token_guard_stage = CompressionStageEvent(
            phase="token-guard",
            algorithm="langchain-approximate-envelope-v2",
            applied=token_guard_accepted,
            reason_code=token_guard_reason,
            input_bytes=original_bytes,
            output_bytes=len(envelope_template.encode("utf-8")),
            input_tokens=original_tokens,
            output_tokens=envelope_tokens,
            metadata={
                "saved_tokens": saved_tokens,
                "savings_ratio": round(savings_ratio, 4),
                "min_absolute_tokens": 128,
                "min_conditional_tokens": 32,
                "min_conditional_ratio": 0.05,
            },
        )
        stages = (*transformed.stages, token_guard_stage)
        if not token_guard_accepted:
            record_decision(
                decision="fallback",
                reason_code=token_guard_reason,
                reason_detail=(
                    "final diff envelope savings did not clear the effective savings floor"
                    if token_guard_reason == "insufficient_effective_savings"
                    else "final model-visible wrapper did not reduce estimated tokens"
                ),
                content_type=transformed.content_type.value,
                transformer=transformed.transformer,
                algorithm_output=transformed.content,
                eligible=True,
                critical_total=transformed.critical_total,
                critical_retained=transformed.critical_retained,
                execution_path=execution_path,
                confidence=confidence,
                stages=stages,
            )
            return message
        try:
            record = repository.put(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                tool_call_id=tool_call_id,
                tool_name=name,
                status=status,
                content=original,
            )
        except Exception as exc:  # noqa: BLE001
            record_decision(
                decision="fallback",
                reason_code="storage_error",
                reason_detail=type(exc).__name__,
                content_type=transformed.content_type.value,
                transformer=transformed.transformer,
                algorithm_output=transformed.content,
                eligible=True,
                critical_total=transformed.critical_total,
                critical_retained=transformed.critical_retained,
                execution_path=execution_path,
                confidence=confidence,
                stages=stages,
            )
            return message

        final_content = envelope_template.replace(provisional_ref, record.ref, 1)
        event = record_decision(
            decision="transformed",
            reason_code="compressed",
            content_type=transformed.content_type.value,
            transformer=transformed.transformer,
            visible_content=final_content,
            algorithm_output=transformed.content,
            eligible=True,
            ref=record.ref,
            critical_total=transformed.critical_total,
            critical_retained=transformed.critical_retained,
            execution_path=execution_path,
            confidence=confidence,
            stages=stages,
        )
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
        message.content = final_content
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

    def record_tool_interaction(request: Any, result: Any, *, started: float) -> None:
        thread_id, checkpoint_ns = runtime_identity(request)
        name = call_value(request, "name", "tool")
        args = call_args(request)
        position = current_position(thread_id)
        messages: list[ToolMessage] = []
        if isinstance(result, ToolMessage):
            messages = [result]
        elif isinstance(result, Command) and isinstance(result.update, dict):
            messages = [m for m in result.update.get("messages", []) if isinstance(m, ToolMessage)]
        elif isinstance(result, list):
            messages = [m for m in result if isinstance(m, ToolMessage)]
        message = messages[0] if messages else None
        artifact = getattr(message, "artifact", None) if message is not None else None
        transform = artifact.get("tool_output_transform") if isinstance(artifact, dict) else None
        output = content_to_text(message.content) if message is not None else ""
        repository.record_interaction(
            thread_id=thread_id,
            event={
                "event_type": "tool_call",
                "turn_id": position.turn_id,
                "turn_index": position.turn_index,
                "model_call_index": position.model_call_index,
                "tool_call_id": str(
                    getattr(message, "tool_call_id", None) or call_value(request, "id")
                ),
                "tool_name": name,
                "tool_args": summarized_args(args),
                "checkpoint_ns": checkpoint_ns,
                "status": str(getattr(message, "status", None) or "success"),
                "output_bytes": len(output.encode("utf-8")),
                "compression_managed": name not in excluded,
                "compression_decision": (
                    str(transform.get("decision")) if isinstance(transform, dict) else ""
                ),
                "compression_reason": (
                    str(transform.get("reason_code")) if isinstance(transform, dict) else ""
                ),
                "duration_ms": (time.perf_counter() - started) * 1000,
            },
        )

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ARG001
        started = time.perf_counter()
        if not is_execute(request):
            result = rewrite_result(request, handler(request))
            record_tool_interaction(request, result, started=started)
            return result
        capture, token = begin_execute_capture()
        try:
            result = handler(request)
        finally:
            end_execute_capture(token)
        rewritten = rewrite_result(
            request,
            result,
            original_content=capture.full_output,
            execute_output_truncated=capture.truncated,
        )
        record_tool_interaction(request, rewritten, started=started)
        return rewritten

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001, ARG001
        started = time.perf_counter()
        if not is_execute(request):
            result = rewrite_result(request, await handler(request))
            record_tool_interaction(request, result, started=started)
            return result
        capture, token = begin_execute_capture()
        try:
            result = await handler(request)
        finally:
            end_execute_capture(token)
        rewritten = rewrite_result(
            request,
            result,
            original_content=capture.full_output,
            execute_output_truncated=capture.truncated,
        )
        record_tool_interaction(request, rewritten, started=started)
        return rewritten

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
