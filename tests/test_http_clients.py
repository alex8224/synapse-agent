"""HTTP keep-alive defaults for LLM SDKs (no shared httpx clients)."""

from __future__ import annotations

import httpx

from synapse import http_clients as hc
from synapse.http_clients import (
    HTTP_KEEPALIVE_EXPIRY_SECONDS,
    enable_long_keepalive_http_defaults,
    long_keepalive_limits,
)


def setup_function() -> None:
    hc._PATCHED = False


def test_keepalive_expiry_is_five_minutes():
    assert HTTP_KEEPALIVE_EXPIRY_SECONDS == 300.0
    assert long_keepalive_limits().keepalive_expiry == 300.0


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
