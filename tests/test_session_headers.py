"""Session-header injection for gateway session affinity."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import synapse.integrations.http_clients as hc
from synapse.runtime.session_header_middleware import build_session_header_middleware
from synapse.runtime.session_headers import (
    attach_session_headers,
    get_session_id,
    session_header_values,
    session_id_context,
    set_session_id,
)


def test_hook_injects_both_headers_when_session_active():
    set_session_id("thread-abc")
    try:
        request = httpx.Request("POST", "http://example.invalid/v1/chat/completions")

        async def run() -> None:
            await attach_session_headers(request)

        asyncio.run(run())
        assert request.headers["X-Session-ID"] == "thread-abc"
        assert request.headers["Session-Id"] == "thread-abc"
    finally:
        set_session_id(None)


def test_hook_overrides_pre_existing_same_headers():
    set_session_id("thread-abc")
    try:
        request = httpx.Request("POST", "http://example.invalid/v1/chat/completions")
        request.headers["X-Session-ID"] = "static-value"
        request.headers["Session-Id"] = "static-value"

        async def run() -> None:
            await attach_session_headers(request)

        asyncio.run(run())
        # Active session id wins over statically configured values.
        assert request.headers["X-Session-ID"] == "thread-abc"
        assert request.headers["Session-Id"] == "thread-abc"
    finally:
        set_session_id(None)


def test_hook_noop_without_active_session():
    set_session_id(None)
    request = httpx.Request("POST", "http://example.invalid/v1/chat/completions")

    async def run() -> None:
        await attach_session_headers(request)

    asyncio.run(run())
    assert "X-Session-ID" not in request.headers
    assert "Session-Id" not in request.headers


def test_hook_skips_unsafe_header_values():
    set_session_id("bad\r\nInjected: x")
    try:
        request = httpx.Request("POST", "http://example.invalid/v1/chat/completions")

        async def run() -> None:
            await attach_session_headers(request)

        asyncio.run(run())
        assert "X-Session-ID" not in request.headers
        assert "Session-Id" not in request.headers
    finally:
        set_session_id(None)


def test_session_id_context_restores_previous_value():
    set_session_id("outer")
    try:
        with session_id_context("inner"):
            assert get_session_id() == "inner"
        assert get_session_id() == "outer"
    finally:
        set_session_id(None)


def test_session_header_values_shared_shape_and_sanitize():
    # Active session maps to both header names (the shape the httpx hook and
    # the native Rust client both consume).
    with session_id_context("thread-abc"):
        assert session_header_values() == {
            "X-Session-ID": "thread-abc",
            "Session-Id": "thread-abc",
        }
    # Unsafe values (CR/LF) are dropped, not passed through.
    with session_id_context("bad\r\nInjected: x"):
        assert session_header_values() is None
    # No active session yields None.
    assert session_header_values() is None


class _Runtime:
    def __init__(self, thread_id: str | None) -> None:
        self.config: dict[str, Any] = (
            {"configurable": {"thread_id": thread_id}} if thread_id else {}
        )


class _Request:
    def __init__(self, thread_id: str | None) -> None:
        self.runtime = _Runtime(thread_id)


def test_middleware_publishes_thread_id_during_call():
    seen: list[str | None] = []

    def handler(request: Any) -> str:
        seen.append(get_session_id())
        return "ok"

    middleware = build_session_header_middleware()
    result = middleware.wrap_model_call(_Request("t-1"), handler)
    assert result == "ok"
    assert seen == ["t-1"]
    assert get_session_id() is None


def test_middleware_async_publishes_thread_id_during_call():
    seen: list[str | None] = []

    async def handler(request: Any) -> str:
        seen.append(get_session_id())
        return "ok"

    middleware = build_session_header_middleware()

    async def run() -> str:
        return await middleware.awrap_model_call(_Request("t-2"), handler)

    assert asyncio.run(run()) == "ok"
    assert seen == ["t-2"]
    assert get_session_id() is None


def test_middleware_without_thread_id_publishes_none():
    seen: list[str | None] = []

    def handler(request: Any) -> str:
        seen.append(get_session_id())
        return "ok"

    middleware = build_session_header_middleware()
    middleware.wrap_model_call(_Request(None), handler)
    assert seen == [None]


def test_middleware_restores_context_on_exception():
    def handler(request: Any) -> str:
        raise RuntimeError("boom")

    middleware = build_session_header_middleware()
    with pytest.raises(RuntimeError, match="boom"):
        middleware.wrap_model_call(_Request("t-3"), handler)
    assert get_session_id() is None


def test_middleware_async_restores_context_on_exception():
    async def handler(request: Any) -> str:
        raise RuntimeError("boom")

    middleware = build_session_header_middleware()

    async def run() -> str:
        return await middleware.awrap_model_call(_Request("t-4"), handler)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())
    assert get_session_id() is None


def test_concurrent_tasks_keep_isolated_session_ids():
    """Interleaved tasks must not leak session ids across contexts."""
    seen: list[tuple[str, str | None]] = []

    async def worker(name: str, tid: str) -> None:
        with session_id_context(tid):
            for _ in range(3):
                await asyncio.sleep(0)
                seen.append((name, get_session_id()))

    async def main() -> None:
        await asyncio.gather(worker("a", "tid-a"), worker("b", "tid-b"))

    asyncio.run(main())
    expected = {"a": "tid-a", "b": "tid-b"}
    assert all(expected[name] == sid for name, sid in seen)
    assert len(seen) == 6


def test_openai_client_registers_session_header_hook(monkeypatch):
    from synapse.runtime.async_runtime import AsyncRuntime

    runtime = AsyncRuntime(name="session-header-test")
    monkeypatch.setattr("synapse.runtime.async_runtime._RUNTIME", runtime)
    try:
        client = hc.build_openai_async_http_client()
        try:
            hooks = client.event_hooks.get("request") or []
            assert any(h.__name__ == "attach_session_headers" for h in hooks)
        finally:
            runtime.close_connection(client)
    finally:
        runtime.close()


def test_openai_client_request_carries_session_headers(monkeypatch):
    """End-to-end: a request through the built client carries both headers."""
    from synapse.runtime.async_runtime import AsyncRuntime

    runtime = AsyncRuntime(name="session-header-e2e-test")
    monkeypatch.setattr("synapse.runtime.async_runtime._RUNTIME", runtime)
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["X-Session-ID"] = request.headers.get("X-Session-ID", "")
        captured["Session-Id"] = request.headers.get("Session-Id", "")
        return httpx.Response(200, json={"ok": True})

    try:
        client = hc.build_openai_async_http_client()
        try:
            # Route through an in-process transport so no network is touched.
            client._transport = httpx.MockTransport(handler)  # type: ignore[assignment]
            set_session_id("e2e-thread")
            try:
                asyncio.run(client.get("http://example.invalid/v1/chat/completions"))
            finally:
                set_session_id(None)
            assert captured == {"X-Session-ID": "e2e-thread", "Session-Id": "e2e-thread"}
        finally:
            runtime.close_connection(client)
    finally:
        runtime.close()
