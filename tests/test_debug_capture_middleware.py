"""Integration tests for the debug capture middleware ↔ store interface.

These cover the exact call chain that regressed once (the middleware used a
``store.begin_raw_capture()`` instance method before it existed), so the
middleware is exercised against the real ``DebugCaptureStore``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from synapse.observability.llm_debug import (
    DebugCaptureStore,
    note_raw_request,
    note_raw_response,
)
from synapse.runtime.debug_capture_middleware import build_debug_capture_middleware
from synapse.runtime.middleware import (
    clear_model_call_started_notifier,
    set_model_call_started_notifier,
)


def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(model=None, messages=[], system_message=None)


def _raw_request(url: str = "http://example.test/v1/chat/completions") -> dict:
    return {"method": "POST", "url": url, "body": '{"model":"m"}', "body_truncated": False}


def _raw_response() -> dict:
    return {"body": '{"choices":[]}', "body_truncated": False}


def test_middleware_sync_records_raw_payloads() -> None:
    store = DebugCaptureStore()
    store.enabled = True
    middleware = build_debug_capture_middleware(store)

    def handler(request):
        note_raw_request(_raw_request())
        note_raw_response(_raw_response())
        return "model-response"

    result = middleware.wrap_model_call(_fake_request(), handler)

    assert result == "model-response"
    records = store.records()
    assert len(records) == 1
    assert records[0].raw_request == _raw_request()
    assert records[0].raw_response == _raw_response()
    assert records[0].error is None


def test_middleware_async_records_raw_payloads() -> None:
    store = DebugCaptureStore()
    store.enabled = True
    middleware = build_debug_capture_middleware(store)

    async def handler(request):
        note_raw_request(_raw_request())
        note_raw_response(_raw_response())
        return "model-response"

    async def run():
        return await middleware.awrap_model_call(_fake_request(), handler)

    assert asyncio.run(run()) == "model-response"
    records = store.records()
    assert len(records) == 1
    assert records[0].raw_request["url"] == "http://example.test/v1/chat/completions"
    assert records[0].raw_response["body"] == '{"choices":[]}'


def test_middleware_records_request_and_error_on_exception() -> None:
    store = DebugCaptureStore()
    store.enabled = True
    middleware = build_debug_capture_middleware(store)

    def handler(request):
        note_raw_request(_raw_request())
        raise RuntimeError("boom")

    try:
        middleware.wrap_model_call(_fake_request(), handler)
    except RuntimeError:
        pass
    else:
        raise AssertionError("exception was not propagated")

    records = store.records()
    assert len(records) == 1
    assert records[0].error == "boom"
    assert records[0].raw_request == _raw_request()
    assert records[0].raw_response is None


def test_middleware_slot_does_not_leak_between_calls() -> None:
    store = DebugCaptureStore()
    store.enabled = True
    middleware = build_debug_capture_middleware(store)

    def with_raw(request):
        note_raw_request(_raw_request())
        note_raw_response(_raw_response())
        return "first"

    def without_raw(request):
        return "second"

    middleware.wrap_model_call(_fake_request(), with_raw)
    middleware.wrap_model_call(_fake_request(), without_raw)

    records = store.records()
    assert len(records) == 2
    assert records[0].raw_request is not None
    assert records[1].raw_request is None
    assert records[1].raw_response is None


def test_middleware_is_pass_through_when_disabled() -> None:
    store = DebugCaptureStore()
    store.enabled = False
    middleware = build_debug_capture_middleware(store)

    def handler(request):
        note_raw_request(_raw_request())
        return "model-response"

    result = middleware.wrap_model_call(_fake_request(), handler)

    assert result == "model-response"
    assert store.records() == []


def test_middleware_fires_model_call_started_when_disabled_sync() -> None:
    store = DebugCaptureStore()
    store.enabled = False
    middleware = build_debug_capture_middleware(store)
    seen: list[float] = []
    token = set_model_call_started_notifier(seen.append)

    try:
        result = middleware.wrap_model_call(_fake_request(), lambda request: "ok")
    finally:
        clear_model_call_started_notifier(token)

    assert result == "ok"
    assert len(seen) == 1
    assert seen[0] > 0.0


def test_middleware_fires_model_call_started_when_disabled_async() -> None:
    store = DebugCaptureStore()
    store.enabled = False
    middleware = build_debug_capture_middleware(store)
    seen: list[float] = []
    token = set_model_call_started_notifier(seen.append)

    async def handler(request):
        return "ok"

    async def run():
        return await middleware.awrap_model_call(_fake_request(), handler)

    try:
        assert asyncio.run(run()) == "ok"
    finally:
        clear_model_call_started_notifier(token)

    assert len(seen) == 1
    assert seen[0] > 0.0
