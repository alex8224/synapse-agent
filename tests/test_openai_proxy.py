"""System proxy fallback for standalone Codex/OAuth requests; no network or credentials."""

from __future__ import annotations

from synapse.integrations import openai_proxy, openai_usage
from synapse.integrations.openai_oauth import OPENAI_OAUTH_ISSUER, _post_token
from synapse.integrations.openai_usage import CodexUsageClient


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"rate_limit": {}, "access_token": "not-a-real-token", "credits": []}


def _system_proxy(monkeypatch) -> None:
    monkeypatch.setattr(openai_proxy.os, "name", "nt")
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(openai_proxy.urllib.request, "proxy_bypass_registry", lambda host: False,
                        raising=False)
    monkeypatch.setattr(openai_proxy.urllib.request, "getproxies_registry",
                        lambda: {"https": "http://localhost:9876"}, raising=False)


def test_windows_system_proxy_when_no_environment_proxy(monkeypatch) -> None:
    _system_proxy(monkeypatch)
    assert openai_proxy.openai_proxy_kwargs("https://chatgpt.com/usage") == {
        "proxy": "http://localhost:9876"
    }


def test_environment_proxy_wins_and_registry_is_not_read(monkeypatch) -> None:
    _system_proxy(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://environment:9000")
    monkeypatch.setattr(openai_proxy.urllib.request, "getproxies_registry",
                        lambda: (_ for _ in ()).throw(AssertionError("must not read system proxy")))
    assert openai_proxy.openai_proxy_kwargs("https://chatgpt.com/usage") == {}


def test_system_proxy_bypass_is_respected(monkeypatch) -> None:
    _system_proxy(monkeypatch)
    monkeypatch.setattr(openai_proxy.urllib.request, "proxy_bypass_registry", lambda host: True)
    assert openai_proxy.openai_proxy_kwargs("https://chatgpt.com/usage") == {}


def test_non_windows_keeps_httpx_defaults(monkeypatch) -> None:
    _system_proxy(monkeypatch)
    monkeypatch.setattr(openai_proxy.os, "name", "posix")
    assert openai_proxy.openai_proxy_kwargs("https://chatgpt.com/usage") == {}


def test_codex_usage_and_credits_apply_proxy(monkeypatch) -> None:
    _system_proxy(monkeypatch)
    calls: list[dict] = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return _Response()

    monkeypatch.setattr(openai_usage.httpx, "get", get)

    class Store:
        def load(self):
            return None

    client = CodexUsageClient(store=Store())
    monkeypatch.setattr(client, "_auth_headers", lambda: {})
    client.fetch(force=True)
    client.fetch_reset_credits(force=True)
    assert [kwargs["proxy"] for kwargs in calls] == ["http://localhost:9876"] * 2


def test_oauth_token_request_applies_proxy(monkeypatch) -> None:
    import synapse.integrations.openai_oauth as oauth

    _system_proxy(monkeypatch)
    calls: list[tuple[str, dict]] = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr(oauth.httpx, "post", post)
    _post_token({"grant_type": "refresh_token"})
    assert calls == [
        (f"{OPENAI_OAUTH_ISSUER}/oauth/token", {
            "data": {"grant_type": "refresh_token"}, "timeout": 30.0,
            "proxy": "http://localhost:9876",
        })
    ]
