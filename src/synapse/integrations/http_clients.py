"""Provider-specific HTTP setup for LLM SDK clients.

OpenAI-compatible models use one dedicated ``httpx.AsyncClient`` per cached
model. Clients share one process SSLContext, but never share a connection pool
across models or event loops. Anthropic keeps its own lazy async client path.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 5 minutes — idle pooled connections only.
HTTP_KEEPALIVE_EXPIRY_SECONDS = 300.0

_OPENAI_PATCHED = False
_ANTHROPIC_PATCHED = False
# Backward-compatible reset flag for callers/tests of the combined helper.
_PATCHED = False

_LONG_KEEPALIVE_LIMITS = httpx.Limits(
    max_connections=1000,
    max_keepalive_connections=100,
    keepalive_expiry=HTTP_KEEPALIVE_EXPIRY_SECONDS,
)


def long_keepalive_limits() -> httpx.Limits:
    return _LONG_KEEPALIVE_LIMITS


def enable_openai_long_keepalive_defaults() -> None:
    """Patch only OpenAI defaults; never import Anthropic on this path."""
    global _OPENAI_PATCHED
    if _OPENAI_PATCHED:
        return
    try:
        import openai._base_client as openai_base
        import openai._constants as openai_constants

        openai_constants.DEFAULT_CONNECTION_LIMITS = _LONG_KEEPALIVE_LIMITS
        openai_base.DEFAULT_CONNECTION_LIMITS = _LONG_KEEPALIVE_LIMITS
    except Exception as exc:  # noqa: BLE001
        logger.debug("openai keep-alive patch skipped: %s", exc)

    try:
        from langchain_openai.chat_models import base as openai_chat_base

        for name in (
            "_cached_sync_httpx_client",
            "_cached_async_httpx_client",
            "_get_default_httpx_client",
            "_get_default_async_httpx_client",
        ):
            fn = getattr(openai_chat_base, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
    except Exception as exc:  # noqa: BLE001
        logger.debug("langchain_openai cache clear skipped: %s", exc)
    _OPENAI_PATCHED = True


def enable_anthropic_long_keepalive_defaults() -> None:
    """Patch only Anthropic defaults; its async client remains lazily created."""
    global _ANTHROPIC_PATCHED
    if _ANTHROPIC_PATCHED:
        return
    try:
        import anthropic._base_client as anthropic_base
        import anthropic._constants as anthropic_constants

        anthropic_constants.DEFAULT_CONNECTION_LIMITS = _LONG_KEEPALIVE_LIMITS
        anthropic_base.DEFAULT_CONNECTION_LIMITS = _LONG_KEEPALIVE_LIMITS
    except Exception as exc:  # noqa: BLE001
        logger.debug("anthropic keep-alive patch skipped: %s", exc)

    try:
        from langchain_anthropic.chat_models import (
            _get_default_async_httpx_client,
            _get_default_httpx_client,
        )

        if hasattr(_get_default_httpx_client, "cache_clear"):
            _get_default_httpx_client.cache_clear()
        if hasattr(_get_default_async_httpx_client, "cache_clear"):
            _get_default_async_httpx_client.cache_clear()
    except Exception as exc:  # noqa: BLE001
        logger.debug("langchain_anthropic cache clear skipped: %s", exc)
    _ANTHROPIC_PATCHED = True


def enable_long_keepalive_http_defaults() -> None:
    """Backward-compatible combined helper for callers that need both SDKs."""
    global _PATCHED
    if _PATCHED:
        return
    enable_openai_long_keepalive_defaults()
    enable_anthropic_long_keepalive_defaults()
    _PATCHED = True


def shared_openai_ssl_context():
    """Return LangChain OpenAI's process singleton SSLContext."""
    from langchain_openai.chat_models.base import global_ssl_context

    return global_ssl_context


def build_openai_async_http_client(
    *,
    timeout: Any = None,
    proxy: str | None = None,
    capture_store: Any = None,
) -> httpx.AsyncClient:
    """Create one model-local AsyncClient on the process async runtime.

    The client is wrapped in a capture transport when a debug capture store is
    available, so the exact HTTP request/response bodies sent to / received
    from the provider land in ``DebugCaptureRecord.raw_request/raw_response``.
    """
    enable_openai_long_keepalive_defaults()
    from synapse.runtime.async_runtime import get_async_runtime

    runtime = get_async_runtime()

    if capture_store is None:
        from synapse.observability.llm_debug import get_debug_store

        capture_store = get_debug_store()

    async def _build() -> httpx.AsyncClient:
        from openai import DEFAULT_TIMEOUT

        transport = httpx.AsyncHTTPTransport(
            verify=shared_openai_ssl_context(),
            limits=_LONG_KEEPALIVE_LIMITS,
            proxy=proxy,
        )
        if capture_store is not None:
            transport = _CapturingAsyncTransport(transport, capture_store)
        return httpx.AsyncClient(
            transport=transport,
            timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
        )

    client = runtime.run(_build())
    runtime.track_connection(client)
    return client


def close_model_async_http_client(model: Any) -> None:
    if bool(getattr(model, "_coding_websocket", False)):
        try:
            from synapse.runtime.async_runtime import get_async_runtime

            get_async_runtime().close_connection(model)
        except Exception:  # noqa: BLE001
            pass
    client = getattr(model, "_coding_http_async_client", None)
    if client is None:
        return
    try:
        from synapse.runtime.async_runtime import get_async_runtime

        get_async_runtime().close_connection(client)
    except Exception:  # noqa: BLE001
        pass
    try:
        model._coding_http_async_client = None
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Raw HTTP capture transport (OpenAI-compatible channel)
# ---------------------------------------------------------------------------

# Cap on the raw request/response body kept per model call. Bodies beyond this
# size are truncated (the live stream handed to the SDK is never affected).
# 2 MiB per body keeps ~100 MB worst-case at the default 50-record ring buffer.
_MAX_RAW_BODY_CHARS = 2 * 1024 * 1024


class _AsyncGeneratorStream(httpx.AsyncByteStream):
    """Adapt an async generator to httpx's AsyncByteStream protocol."""

    def __init__(self, generator: Any) -> None:
        self._generator = generator

    def __aiter__(self) -> Any:
        return self.aiter_bytes()

    async def aiter_bytes(self) -> Any:
        async for chunk in self._generator:
            yield chunk

    async def aclose(self) -> None:
        close = getattr(self._generator, "aclose", None)
        if close is not None:
            await close()


class _CapturingAsyncTransport(httpx.AsyncBaseTransport):
    """Pass-through transport that records raw request/response bodies.

    Wraps the inner transport and, while the debug store is enabled, captures:

    - the exact request body sent to the provider (``request.content``), and
    - the full response body (streamed or not) after the stream is consumed.

    The payload is attached to the active model-call slot via
    ``synapse.observability.llm_debug.note_raw_*``; the debug capture
    middleware picks it up when it records the model call. When the store is
    disabled the wrapper is a transparent pass-through.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, store: Any) -> None:
        self._inner = inner
        self._store = store

    def __getattr__(self, name: str) -> Any:
        # Expose inner attributes (e.g. ``_pool`` used by keepalive helpers).
        return getattr(self._inner, name)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._store.enabled:
            return await self._inner.handle_async_request(request)

        from synapse.observability.llm_debug import note_raw_request

        try:
            response = await self._inner.handle_async_request(request)
        except Exception:
            # Record the attempted request even when the call never reached
            # the provider (DNS / connect / timeout failures).
            note_raw_request(_payload_for(request, request.content or b""))
            raise
        note_raw_request(_payload_for(request, request.content or b""))
        return self._wrap_response(request, response)

    @staticmethod
    def _wrap_response(request: httpx.Request, response: httpx.Response) -> httpx.Response:
        """Re-wrap the response stream so the body can be copied as it flows."""
        captured = bytearray()
        truncated = False

        async def _stream() -> Any:
            nonlocal truncated
            try:
                async for chunk in response.aiter_bytes():
                    if not truncated:
                        room = _MAX_RAW_BODY_CHARS - len(captured)
                        if room > 0:
                            captured.extend(chunk[:room])
                            if len(chunk) > room:
                                truncated = True
                        else:
                            truncated = True
                    yield chunk
            finally:
                _finish_capture(captured, truncated)

        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_AsyncGeneratorStream(_stream()),
            request=request,
            extensions=response.extensions,
        )


def _payload_for(request: httpx.Request, body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", "replace")
    return {
        "method": request.method,
        "url": str(request.url),
        "body": text[:_MAX_RAW_BODY_CHARS],
        "body_truncated": len(text) > _MAX_RAW_BODY_CHARS,
    }


def _finish_capture(body: bytearray, truncated: bool) -> None:
    from synapse.observability.llm_debug import note_raw_response

    text = bytes(body).decode("utf-8", "replace")
    note_raw_response(
        {
            "body": text,
            "body_truncated": truncated or len(text) > _MAX_RAW_BODY_CHARS,
        }
    )


# --- deprecated API (no-op / thin wrappers) so old imports do not crash ---


def get_shared_http_clients() -> tuple[Any, Any]:
    """Deprecated. Process-global shared pools remain disabled."""
    enable_long_keepalive_http_defaults()
    raise RuntimeError("shared HTTP clients are disabled; use model-local AsyncClient")


get_shared_openai_http_clients = get_shared_http_clients


def inject_openai_http_clients(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Compatibility helper: apply OpenAI defaults without injecting a sync client."""
    enable_openai_long_keepalive_defaults()
    return dict(kwargs)


def apply_keepalive_http_clients_to_model(model: Any) -> Any:
    """Compatibility helper for Anthropic's lazy client path."""
    enable_anthropic_long_keepalive_defaults()
    return model


def close_shared_http_clients() -> None:
    """Deprecated no-op (no process-global clients)."""
    return


def client_keepalive_expiry(client: httpx.Client | httpx.AsyncClient) -> float | None:
    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None) if transport is not None else None
    expiry = getattr(pool, "_keepalive_expiry", None) if pool is not None else None
    if expiry is None:
        return None
    return float(expiry)
