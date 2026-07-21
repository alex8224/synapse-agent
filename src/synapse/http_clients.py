"""Tune LLM HTTP keep-alive without hijacking shared httpx clients.

Background
----------
OpenAI / Anthropic SDK defaults use ``keepalive_expiry=5`` seconds. That only
affects how long an *idle pooled* connection may live; it does not force the
client to keep a socket open after every SSE stream.

Earlier approach (process-global shared ``httpx.AsyncClient`` injected into
ChatOpenAI / ChatAnthropic) caused ``h11`` state hangs: one AsyncClient used
across event loops / providers breaks HTTP/1.1 connection state.

Safe approach
-------------
Patch each SDK's ``DEFAULT_CONNECTION_LIMITS`` so *their own* client factories
create normal per-SDK clients with ``keepalive_expiry=300`` (5 minutes). No
shared transport, no cross-loop client reuse.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 5 minutes — idle pooled connections only.
HTTP_KEEPALIVE_EXPIRY_SECONDS = 300.0

_PATCHED = False

_LONG_KEEPALIVE_LIMITS = httpx.Limits(
    max_connections=1000,
    max_keepalive_connections=100,
    keepalive_expiry=HTTP_KEEPALIVE_EXPIRY_SECONDS,
)


def long_keepalive_limits() -> httpx.Limits:
    return _LONG_KEEPALIVE_LIMITS


def enable_long_keepalive_http_defaults() -> None:
    """Idempotent: raise SDK default keep-alive to 5 minutes."""
    global _PATCHED
    if _PATCHED:
        return

    # OpenAI Python SDK (ChatOpenAI → openai.OpenAI / AsyncOpenAI)
    try:
        import openai._base_client as openai_base
        import openai._constants as openai_constants

        openai_constants.DEFAULT_CONNECTION_LIMITS = _LONG_KEEPALIVE_LIMITS
        openai_base.DEFAULT_CONNECTION_LIMITS = _LONG_KEEPALIVE_LIMITS
    except Exception as exc:  # noqa: BLE001
        logger.debug("openai keep-alive patch skipped: %s", exc)

    # Anthropic Python SDK (ChatAnthropic → anthropic.Client / AsyncClient)
    try:
        import anthropic._base_client as anthropic_base
        import anthropic._constants as anthropic_constants

        anthropic_constants.DEFAULT_CONNECTION_LIMITS = _LONG_KEEPALIVE_LIMITS
        anthropic_base.DEFAULT_CONNECTION_LIMITS = _LONG_KEEPALIVE_LIMITS
    except Exception as exc:  # noqa: BLE001
        logger.debug("anthropic keep-alive patch skipped: %s", exc)

    # langchain-anthropic caches default httpx wrappers — drop stale entries.
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

    _PATCHED = True


# --- deprecated API (no-op / thin wrappers) so old imports do not crash ---


def get_shared_http_clients() -> tuple[Any, Any]:
    """Deprecated. Shared clients were removed (h11 hang risk)."""
    enable_long_keepalive_http_defaults()
    raise RuntimeError(
        "shared httpx clients were removed; keep-alive is applied via SDK "
        "DEFAULT_CONNECTION_LIMITS only"
    )


get_shared_openai_http_clients = get_shared_http_clients


def inject_openai_http_clients(kwargs: dict[str, Any]) -> dict[str, Any]:
    """No longer injects clients; only ensures SDK keep-alive patch is on."""
    enable_long_keepalive_http_defaults()
    return dict(kwargs)


def apply_keepalive_http_clients_to_model(model: Any) -> Any:
    """No longer seeds Anthropic clients; only ensures SDK keep-alive patch."""
    enable_long_keepalive_http_defaults()
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
