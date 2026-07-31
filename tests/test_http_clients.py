"""HTTP keep-alive defaults for LLM SDKs (no shared httpx clients)."""

from __future__ import annotations

import asyncio
import json

import httpx

import synapse.integrations.http_clients as hc
from synapse.integrations.http_clients import (
    HTTP_KEEPALIVE_EXPIRY_SECONDS,
    _CapturingAsyncTransport,
    client_keepalive_expiry,
    enable_anthropic_long_keepalive_defaults,
    enable_long_keepalive_http_defaults,
    enable_openai_long_keepalive_defaults,
    long_keepalive_limits,
)
from synapse.observability.llm_debug import (
    DebugCaptureStore,
    begin_raw_capture,
    end_raw_capture,
)


def setup_function() -> None:
    hc._PATCHED = False
    hc._OPENAI_PATCHED = False
    hc._ANTHROPIC_PATCHED = False


def test_keepalive_expiry_is_five_minutes():
    assert HTTP_KEEPALIVE_EXPIRY_SECONDS == 300.0
    assert long_keepalive_limits().keepalive_expiry == 300.0


def test_provider_specific_patches_do_not_cross_import(monkeypatch):
    import builtins

    imported: list[str] = []
    original = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        imported.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    enable_openai_long_keepalive_defaults()
    assert not any(name.startswith("anthropic") for name in imported)

    imported.clear()
    enable_anthropic_long_keepalive_defaults()
    assert not any(name.startswith("openai") for name in imported)


def test_openai_async_client_uses_sdk_default_timeout(monkeypatch):
    from synapse.runtime.async_runtime import AsyncRuntime

    runtime = AsyncRuntime(name="http-client-timeout-test")
    monkeypatch.setattr("synapse.runtime.async_runtime._RUNTIME", runtime)
    client = hc.build_openai_async_http_client()
    try:
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 600
        assert client.timeout.write == 600
        assert client.timeout.pool == 600
        assert client_keepalive_expiry(client) == 300.0
    finally:
        runtime.close()


def test_enable_patches_openai_and_anthropic_defaults():
    hc._PATCHED = False
    enable_long_keepalive_http_defaults()

    import anthropic._base_client as anthropic_base
    import anthropic._constants as anthropic_constants
    import openai._base_client as openai_base
    import openai._constants as openai_constants

    for limits in (
        openai_constants.DEFAULT_CONNECTION_LIMITS,
        openai_base.DEFAULT_CONNECTION_LIMITS,
        anthropic_constants.DEFAULT_CONNECTION_LIMITS,
        anthropic_base.DEFAULT_CONNECTION_LIMITS,
    ):
        assert isinstance(limits, httpx.Limits)
        assert limits.keepalive_expiry == 300.0


def test_enable_is_idempotent():
    hc._PATCHED = False
    enable_long_keepalive_http_defaults()
    first = id(long_keepalive_limits())
    enable_long_keepalive_http_defaults()
    assert id(long_keepalive_limits()) == first


def test_openai_default_client_uses_long_keepalive():
    hc._PATCHED = False
    enable_long_keepalive_http_defaults()
    from openai._base_client import SyncHttpxClientWrapper

    client = SyncHttpxClientWrapper(base_url="http://127.0.0.1:9/v1")
    try:
        pool = client._transport._pool
        assert pool._keepalive_expiry == 300.0
    finally:
        client.close()


def test_anthropic_default_client_uses_long_keepalive():
    hc._PATCHED = False
    enable_long_keepalive_http_defaults()
    import anthropic

    client = anthropic.DefaultHttpxClient(base_url="http://127.0.0.1:9")
    try:
        pool = client._transport._pool
        assert pool._keepalive_expiry == 300.0
    finally:
        client.close()


def _run_transport_capture(
    store: DebugCaptureStore,
    request_handler,
) -> tuple[dict, dict]:
    """Run one request through the capture transport inside a capture slot."""

    async def run() -> tuple[dict, dict]:
        transport = _CapturingAsyncTransport(httpx.MockTransport(request_handler), store)
        slot = begin_raw_capture()
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.post(
                    "http://example.test/v1/chat/completions",
                    json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                )
                await response.aread()
        finally:
            end_raw_capture()
        return slot.get("request"), slot.get("response")

    return asyncio.run(run())


def test_capture_transport_records_raw_request_and_response():
    store = DebugCaptureStore()
    store.enabled = True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "resp-1", "choices": [{"index": 0}]},
            request=request,
        )

    raw_request, raw_response = _run_transport_capture(store, handler)

    assert raw_request is not None
    assert raw_request["method"] == "POST"
    assert raw_request["url"] == "http://example.test/v1/chat/completions"
    body = json.loads(raw_request["body"])
    assert body["model"] == "test"
    assert raw_request["body_truncated"] is False
    assert json.loads(raw_response["body"])["id"] == "resp-1"


def test_capture_transport_records_streamed_response_body():
    store = DebugCaptureStore()
    store.enabled = True

    def handler(request: httpx.Request) -> httpx.Response:
        async def chunks():
            yield b'data: {"delta": "a"}\n\n'
            yield b'data: {"delta": "b"}\n\n'
            yield b"data: [DONE]\n\n"

        return httpx.Response(
            200,
            stream=hc._AsyncGeneratorStream(chunks()),
            request=request,
        )

    raw_request, raw_response = _run_transport_capture(store, handler)

    expected = 'data: {"delta": "a"}\n\ndata: {"delta": "b"}\n\ndata: [DONE]\n\n'
    assert raw_response is not None
    assert raw_response["body"] == expected
    assert raw_response["body_truncated"] is False


def test_capture_transport_truncates_large_response_body():
    store = DebugCaptureStore()
    store.enabled = True
    limit = hc._MAX_RAW_BODY_CHARS

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (limit + 10_000), request=request)

    _, raw_response = _run_transport_capture(store, handler)

    assert raw_response is not None
    assert len(raw_response["body"]) == limit
    assert raw_response["body_truncated"] is True


def test_capture_transport_is_pass_through_when_disabled():
    store = DebugCaptureStore()
    store.enabled = False

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    raw_request, raw_response = _run_transport_capture(store, handler)

    assert raw_request is None
    assert raw_response is None


def test_capture_transport_records_request_on_provider_error():
    store = DebugCaptureStore()
    store.enabled = True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}}, request=request)

    raw_request, raw_response = _run_transport_capture(store, handler)

    assert raw_request is not None
    assert raw_response is not None
    assert json.loads(raw_response["body"])["error"]["message"] == "rate limited"
