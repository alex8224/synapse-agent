"""Lightweight middleware that captures raw LLM request/response pairs.

Placed as the **innermost** middleware so it sees the final request shape
after all other middleware have modified it — exactly what is sent to the
provider.

When ``DebugCaptureStore.enabled`` is False the middleware skips capture
bookkeeping, but it still fires the model-call-started notifier used for TTFT
timing, so the timing hook is independent of debug capture being enabled.
"""

from __future__ import annotations

import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState

from synapse.observability.llm_debug import DebugCaptureStore
from synapse.runtime.middleware import notify_model_call_started


def build_debug_capture_middleware(store: DebugCaptureStore) -> AgentMiddleware:
    """Build a middleware that captures every model call into *store*.

    Must be appended **last** in the middleware list so it sits inside all
    other wrappers and records the final, provider-ready request.
    """

    class _DebugCaptureMiddleware(AgentMiddleware):
        state_schema = AgentState
        tools: list[Any] = []

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            notify_model_call_started()
            if not store.enabled:
                return handler(request)
            slot = store.begin_raw_capture()
            started_at = time.time()
            started_perf = time.perf_counter()
            try:
                try:
                    response = handler(request)
                except Exception as exc:
                    store.record(
                        request,
                        None,
                        started_at=started_at,
                        started_perf=started_perf,
                        error=str(exc),
                        raw_request=slot.get("request"),
                        raw_response=slot.get("response"),
                    )
                    raise
                store.record(
                    request,
                    response,
                    started_at=started_at,
                    started_perf=started_perf,
                    raw_request=slot.get("request"),
                    raw_response=slot.get("response"),
                )
                return response
            finally:
                store.end_raw_capture()

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            notify_model_call_started()
            if not store.enabled:
                return await handler(request)
            slot = store.begin_raw_capture()
            started_at = time.time()
            started_perf = time.perf_counter()
            try:
                try:
                    response = await handler(request)
                except Exception as exc:
                    store.record(
                        request,
                        None,
                        started_at=started_at,
                        started_perf=started_perf,
                        error=str(exc),
                        raw_request=slot.get("request"),
                        raw_response=slot.get("response"),
                    )
                    raise
                store.record(
                    request,
                    response,
                    started_at=started_at,
                    started_perf=started_perf,
                    raw_request=slot.get("request"),
                    raw_response=slot.get("response"),
                )
                return response
            finally:
                store.end_raw_capture()

    return _DebugCaptureMiddleware()
