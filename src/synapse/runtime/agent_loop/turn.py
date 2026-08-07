"""Headless execution state machine for one agent turn."""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from synapse.runtime.agent_loop.model import (
    CancelToken,
    TurnContext,
    TurnHandle,
    TurnResult,
    TurnStatus,
)
from synapse.runtime.async_runtime import AsyncRuntime, get_async_runtime
from synapse.runtime.streaming import AgentEventSink


class _SafeEventSink:
    """Prevent an observer callback from failing the Agent turn."""

    def __init__(self, sink: AgentEventSink) -> None:
        self._sink = sink

    def emit(self, event: Any) -> None:
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 - observers are not turn owners
            pass


class _HeadlessRenderer:
    """No-op legacy renderer while P1 parser still exposes StreamSink calls."""

    streamed_answer = False
    streamed_reasoning = False

    def __init__(self) -> None:
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name in {
            "activity_start",
            "activity_update",
            "activity_stop",
            "write_reasoning",
            "close_reasoning",
            "write_answer_token",
            "write_answer_complete",
            "finalize_line",
            "tool_calls_started",
            "tool_result",
            "info",
            "note_usage",
        }:
            return lambda *args, **kwargs: None
        raise AttributeError(name)


@dataclass(frozen=True, slots=True)
class StreamRunnerOptions:
    """Optional compatibility renderer settings supplied by app assembly."""

    renderer: Any | None = None


class AgentTurnRuntime:
    """Execute exactly one frozen TurnContext without any UI dependency."""

    def __init__(
        self,
        async_runtime: AsyncRuntime | None = None,
        *,
        stream_runner: Callable[..., Any] | None = None,
        runner_options: StreamRunnerOptions | None = None,
    ) -> None:
        self._async_runtime = async_runtime or get_async_runtime()
        self._stream_runner = stream_runner
        self._runner_options = runner_options or StreamRunnerOptions()
        self._run_lock = threading.Lock()
        self._running_turns: set[str] = set()

    def submit(
        self,
        context: TurnContext,
        *,
        sink: AgentEventSink | None = None,
        cancel_token: CancelToken | None = None,
    ) -> TurnHandle:
        """Schedule one turn on the process Agent runtime loop."""
        token = cancel_token or CancelToken()
        future = self._async_runtime.submit(self.arun(context, sink=sink, cancel_token=token))
        return TurnHandle(turn_id=context.turn_id, future=future, cancel_token=token)

    def submit_coroutine(self, coroutine: Any) -> concurrent.futures.Future[Any]:
        """Schedule runtime coordination without exposing the process loop owner."""
        return self._async_runtime.submit(coroutine)

    async def arun(
        self,
        context: TurnContext,
        *,
        sink: AgentEventSink | None = None,
        cancel_token: CancelToken | None = None,
    ) -> TurnResult:
        """Run one turn without blocking the current asyncio loop."""
        token = cancel_token or CancelToken()
        self._claim(context.turn_id)
        try:
            # stream_agent remains the P1 compatibility parser and is synchronous.
            # Running it in a bounded asyncio worker prevents sync saver fallback
            # from blocking the process Agent loop. Its async stream bridge still
            # schedules onto the checkpointer's bound loop when required.
            stream_runner = self._stream_runner or self._resolve_stream_runner(context.agent)
            return await asyncio.to_thread(
                self._run_sync_once,
                context,
                sink,
                token,
                stream_runner,
                self._runner_options,
            )
        finally:
            self._release(context.turn_id)

    def run(
        self,
        context: TurnContext,
        *,
        sink: AgentEventSink | None = None,
        cancel_token: CancelToken | None = None,
        timeout: float | None = None,
    ) -> TurnResult:
        """Synchronous compatibility entry from outside the Agent runtime loop."""
        self._assert_not_runtime_loop()
        handle = self.submit(context, sink=sink, cancel_token=cancel_token)
        return handle.result(timeout=timeout)

    def _claim(self, turn_id: str) -> None:
        with self._run_lock:
            if turn_id in self._running_turns:
                raise RuntimeError(f"turn already running: {turn_id}")
            self._running_turns.add(turn_id)

    def _release(self, turn_id: str) -> None:
        with self._run_lock:
            self._running_turns.discard(turn_id)

    def _assert_not_runtime_loop(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        if running is self._async_runtime.loop:
            raise RuntimeError(
                "AgentTurnRuntime.run() cannot block the Agent runtime loop; use arun()"
            )

    @staticmethod
    def _resolve_stream_runner(agent: Any) -> Callable[..., Any]:
        """Resolve the P1 parser registered by app assembly or the agent."""
        runner = getattr(agent, "_coding_stream_runner", None)
        if callable(runner):
            return runner
        module_name = "synapse" + ".ui.stream"
        return importlib.import_module(module_name).stream_agent

    @staticmethod
    def _run_sync_once(
        context: TurnContext,
        sink: AgentEventSink | None,
        token: CancelToken,
        stream_runner: Callable[..., Any],
        runner_options: StreamRunnerOptions,
    ) -> TurnResult:
        settings = context.settings
        safe_sink = _SafeEventSink(sink) if sink is not None else None
        try:
            result = stream_runner(
                context.agent,
                context.request.payload,
                context.request.mutable_config(),
                token_stream=bool(getattr(settings, "token_stream", True)),
                prefer_async=True,
                max_concurrency=int(getattr(settings, "max_concurrency", 4)),
                sink=runner_options.renderer or _HeadlessRenderer(),
                event_sink=safe_sink,
                cancel_event=token.event,
                show_reasoning_placeholders=bool(
                    getattr(settings, "show_reasoning_placeholders", True)
                ),
            )
        except BaseException as exc:
            return TurnResult(
                turn_id=context.turn_id,
                thread_id=context.thread_id,
                status=TurnStatus.FAILED,
                cancel_reason=token.reason if token.cancelled else None,
                error_type=type(exc).__name__,
                error_message=str(exc)[:2000],
            )

        if result.cancelled or token.cancelled:
            status = TurnStatus.CANCELLED
        elif result.interrupted:
            status = TurnStatus.WAITING_APPROVAL
        else:
            status = TurnStatus.COMPLETED
        return TurnResult(
            turn_id=context.turn_id,
            thread_id=context.thread_id,
            status=status,
            state=dict(result.state),
            final_text=result.final_text,
            reasoning_text=result.reasoning_text,
            tool_calls=result.tool_calls,
            elapsed_s=result.elapsed_s,
            streamed_answer=result.streamed_answer,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_tokens=result.cache_tokens,
            total_tokens=result.total_tokens,
            last_input_tokens=result.last_input_tokens,
            last_output_tokens=result.last_output_tokens,
            last_cache_tokens=result.last_cache_tokens,
            last_output_tokens_per_second=result.last_output_tokens_per_second,
            last_ttft_s=result.last_ttft_s,
            last_rate_basis=result.last_rate_basis,
            compact_events=result.compact_events,
            cancel_reason=token.reason if status is TurnStatus.CANCELLED else None,
        )


def completed_future(result: TurnResult) -> concurrent.futures.Future[TurnResult]:
    """Create an already-completed future for adapters and tests."""
    future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
    future.set_result(result)
    return future
