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
) -> httpx.AsyncClient:
    """Create one model-local AsyncClient on the process async runtime."""
    enable_openai_long_keepalive_defaults()
    from synapse.runtime.async_runtime import get_async_runtime

    runtime = get_async_runtime()

    async def _build() -> httpx.AsyncClient:
        from openai import DEFAULT_TIMEOUT

        kwargs: dict[str, Any] = {
            "verify": shared_openai_ssl_context(),
            "limits": _LONG_KEEPALIVE_LIMITS,
            "timeout": DEFAULT_TIMEOUT if timeout is None else timeout,
        }
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

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
