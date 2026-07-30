from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from synapse.integrations.openai_oauth import (
    OPENAI_OAUTH_CALLBACK_PATH,
    OPENAI_OAUTH_CLIENT_ID,
    OPENAI_OAUTH_LOCAL_PORT,
    OpenAIOAuthError,
    OpenAIOAuthStore,
    OpenAIOAuthTokenProvider,
    OpenAIOAuthTokens,
    _AuthorizationCodeReceiver,
    build_authorize_url,
    extract_account_id,
    generate_pkce,
    import_codex_credentials,
)


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


def test_pkce_is_url_safe_and_authorize_url_contains_codex_fields() -> None:
    verifier, challenge = generate_pkce()
    assert len(verifier) >= 43
    assert "=" not in challenge

    assert OPENAI_OAUTH_LOCAL_PORT == 1455
    redirect_uri = f"http://localhost:{OPENAI_OAUTH_LOCAL_PORT}{OPENAI_OAUTH_CALLBACK_PATH}"
    url = build_authorize_url(redirect_uri, "state", challenge)
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == [OPENAI_OAUTH_CLIENT_ID]
    assert query["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["codex_cli_simplified_flow"] == ["true"]
    assert query["state"] == ["state"]


def test_callback_receiver_ignores_non_callback_request() -> None:
    import threading

    import httpx

    receiver = _AuthorizationCodeReceiver(port=0)
    worker = threading.Thread(target=receiver._server.handle_request)
    worker.start()
    response = httpx.get(f"http://127.0.0.1:{receiver.port}/favicon.ico")
    worker.join(timeout=2)
    receiver._server.server_close()

    assert response.status_code == 404
    assert receiver._result == {}


def test_extract_account_id_from_openai_auth_claim() -> None:
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"}})
    assert extract_account_id(token) == "acct-123"
    assert extract_account_id("not-a-jwt") is None


def test_store_round_trip_and_delete(tmp_path: Path) -> None:
    store = OpenAIOAuthStore(tmp_path / "oauth.json")
    tokens = OpenAIOAuthTokens("access", "refresh", 1234.0, "acct-1")
    store.save(tokens)

    assert store.load() == tokens
    assert store.delete() is True
    assert store.load() is None
    assert store.delete() is False


def test_token_provider_rejects_missing_login(tmp_path: Path) -> None:
    provider = OpenAIOAuthTokenProvider(OpenAIOAuthStore(tmp_path / "missing.json"))
    with pytest.raises(OpenAIOAuthError, match="not logged in"):
        provider.access_token()


def test_import_codex_credentials_uses_jwt_expiry_and_account_id(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    token = _jwt({"exp": time.time() + 300, "chatgpt_account_id": "acct-imported"})
    (home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": token, "refresh_token": "refresh"},
            }
        ),
        encoding="utf-8",
    )

    imported = import_codex_credentials(home)
    assert imported.access_token == token
    assert imported.account_id == "acct-imported"
    assert imported.expires_at > time.time()