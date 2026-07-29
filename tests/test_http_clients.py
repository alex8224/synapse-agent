"""HTTP keep-alive defaults for LLM SDKs (no shared httpx clients)."""

from __future__ import annotations

import httpx

import synapse.integrations.http_clients as hc
from synapse.integrations.http_clients import (
    HTTP_KEEPALIVE_EXPIRY_SECONDS,
    client_keepalive_expiry,
    enable_anthropic_long_keepalive_defaults,
    enable_long_keepalive_http_defaults,
    enable_openai_long_keepalive_defaults,
    long_keepalive_limits,
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
