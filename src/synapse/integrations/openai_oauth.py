"""OpenAI Codex OAuth authentication compatible with the Codex CLI flow.

The OpenAI Python SDK does not initiate the Codex OAuth flow itself.  This
module owns the PKCE browser flow and securely persists a single OAuth grant;
``ChatOpenAI`` receives a callable that refreshes the access token on demand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from synapse.settings.config_paths import user_config_dir

OPENAI_OAUTH_ISSUER = "https://auth.openai.com"
OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_OAUTH_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
OPENAI_OAUTH_CALLBACK_PATH = "/auth/callback"
# OpenAI registers this loopback redirect URI for the Codex OAuth client.
OPENAI_OAUTH_LOCAL_PORT = 1455
OPENAI_OAUTH_ORIGINATOR = "codex_cli_rs"
OAUTH_FILENAME = "openai_oauth.json"
_REFRESH_SKEW_SECONDS = 60


class OpenAIOAuthError(RuntimeError):
    """A failed or cancelled OpenAI OAuth operation."""


@dataclass(frozen=True)
class OpenAIOAuthTokens:
    """Persisted OAuth grant. Tokens must never be shown in CLI output."""

    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str | None = None

    @property
    def is_expiring(self) -> bool:
        return self.expires_at <= time.time() + _REFRESH_SKEW_SECONDS

    @classmethod
    def from_response(cls, data: dict[str, Any], *, previous: OpenAIOAuthTokens | None = None):
        access_token = _required_string(data, "access_token")
        refresh_token = _optional_string(data.get("refresh_token")) or (
            previous.refresh_token if previous else None
        )
        if not refresh_token:
            raise OpenAIOAuthError("OpenAI OAuth response did not include a refresh token")
        try:
            expires_in = max(int(data.get("expires_in", 3600)), 60)
        except (TypeError, ValueError):
            expires_in = 3600
        id_token = _optional_string(data.get("id_token"))
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in,
            account_id=extract_account_id(id_token) or extract_account_id(access_token),
        )


def oauth_credentials_path() -> Path:
    """Canonical user-level OAuth credential path."""
    return user_config_dir() / OAUTH_FILENAME


class OpenAIOAuthStore:
    """Atomic, process-local locked storage for one user-level OAuth grant."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or oauth_credentials_path())
        self._lock = threading.RLock()

    def load(self) -> OpenAIOAuthTokens | None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, json.JSONDecodeError) as exc:
                raise OpenAIOAuthError(f"cannot read OpenAI OAuth credentials: {exc}") from exc
            if not isinstance(raw, dict):
                raise OpenAIOAuthError("OpenAI OAuth credentials must be a JSON object")
            try:
                return OpenAIOAuthTokens(
                    access_token=_required_string(raw, "access_token"),
                    refresh_token=_required_string(raw, "refresh_token"),
                    expires_at=float(raw["expires_at"]),
                    account_id=_optional_string(raw.get("account_id")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise OpenAIOAuthError("OpenAI OAuth credentials are incomplete") from exc

    def save(self, tokens: OpenAIOAuthTokens) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                temporary.write_text(json.dumps(asdict(tokens), indent=2) + "\n", encoding="utf-8")
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                os.replace(temporary, self.path)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def delete(self) -> bool:
        with self._lock:
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return False


class OpenAIOAuthTokenProvider:
    """Return an access token and refresh it before an OpenAI request when needed."""

    def __init__(self, store: OpenAIOAuthStore | None = None) -> None:
        self.store = store or OpenAIOAuthStore()
        self._lock = threading.RLock()

    def access_token(self) -> str:
        with self._lock:
            tokens = self.store.load()
            if tokens is None:
                raise OpenAIOAuthError(
                    "OpenAI OAuth is not logged in; run: synapse auth openai login"
                )
            if not tokens.is_expiring:
                return tokens.access_token
            refreshed = refresh_tokens(tokens)
            self.store.save(refreshed)
            return refreshed.access_token

    def account_id(self) -> str | None:
        tokens = self.store.load()
        return tokens.account_id if tokens else None


def build_authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
    """Build the Codex-compatible OAuth authorization URL."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": OPENAI_OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": OPENAI_OAUTH_SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": OPENAI_OAUTH_ORIGINATOR,
            "state": state,
        }
    )
    return f"{OPENAI_OAUTH_ISSUER}/oauth/authorize?{query}"


def generate_pkce() -> tuple[str, str]:
    """Generate a high-entropy PKCE verifier and S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def login_via_browser(
    *, timeout_seconds: float = 300.0, open_browser: bool = True
) -> OpenAIOAuthTokens:
    """Run a local-loopback PKCE flow and return the newly issued grant."""
    receiver = _AuthorizationCodeReceiver()
    redirect_uri = f"http://localhost:{OPENAI_OAUTH_LOCAL_PORT}{OPENAI_OAUTH_CALLBACK_PATH}"
    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce()
    authorize_url = build_authorize_url(redirect_uri, state, challenge)
    if open_browser:
        try:
            webbrowser.open(authorize_url, new=2)
        except Exception:  # noqa: BLE001
            pass
    print(f"Open this URL to sign in to OpenAI:\n{authorize_url}")
    code = receiver.wait_for_code(state, timeout_seconds)
    return exchange_authorization_code(code, verifier, redirect_uri)


def exchange_authorization_code(code: str, verifier: str, redirect_uri: str) -> OpenAIOAuthTokens:
    response = _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": OPENAI_OAUTH_CLIENT_ID,
            "code_verifier": verifier,
        }
    )
    return OpenAIOAuthTokens.from_response(response)


def refresh_tokens(tokens: OpenAIOAuthTokens) -> OpenAIOAuthTokens:
    """Refresh an existing grant, retaining the prior refresh token if omitted."""
    response = _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": OPENAI_OAUTH_CLIENT_ID,
        }
    )
    refreshed = OpenAIOAuthTokens.from_response(response, previous=tokens)
    if refreshed.account_id is None:
        return OpenAIOAuthTokens(
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            expires_at=refreshed.expires_at,
            account_id=tokens.account_id,
        )
    return refreshed


def import_codex_credentials(codex_home: Path | None = None) -> OpenAIOAuthTokens:
    """Read an existing Codex CLI OAuth grant without exposing its secrets."""
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    source = home / "auth.json"
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OpenAIOAuthError(f"Codex credentials not found: {source}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenAIOAuthError(f"cannot read Codex credentials: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        raise OpenAIOAuthError("Codex credentials do not contain OAuth tokens")
    tokens = data["tokens"]
    access_token = _optional_string(tokens.get("access_token"))
    refresh_token = _optional_string(tokens.get("refresh_token"))
    if not access_token or not refresh_token:
        raise OpenAIOAuthError("Codex credentials do not contain a reusable OAuth grant")
    expires_at = _jwt_expiry(access_token) or (time.time() - 1)
    return OpenAIOAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        account_id=(
            extract_account_id(_optional_string(tokens.get("id_token")))
            or extract_account_id(access_token)
        ),
    )


def extract_account_id(token: str | None) -> str | None:
    """Extract ChatGPT account id from a JWT without trusting it as authentication."""
    if not token:
        return None
    try:
        payload = _decode_jwt_payload(token)
    except (ValueError, json.JSONDecodeError):
        return None
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        value = _optional_string(auth.get("chatgpt_account_id"))
        if value:
            return value
    return _optional_string(payload.get("chatgpt_account_id"))


def _jwt_expiry(token: str) -> float | None:
    try:
        exp = _decode_jwt_payload(token).get("exp")
        return float(exp) if exp is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    data = json.loads(base64.urlsafe_b64decode(payload))
    if not isinstance(data, dict):
        raise ValueError("JWT payload is not an object")
    return data


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    try:
        response = httpx.post(f"{OPENAI_OAUTH_ISSUER}/oauth/token", data=payload, timeout=30.0)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise OpenAIOAuthError(f"OpenAI OAuth token request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise OpenAIOAuthError("OpenAI OAuth token response was not a JSON object")
    return data


def _required_string(data: dict[str, Any], name: str) -> str:
    value = _optional_string(data.get(name))
    if not value:
        raise OpenAIOAuthError(f"OpenAI OAuth response is missing {name}")
    return value


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class _AuthorizationCodeReceiver:
    def __init__(self, *, port: int = OPENAI_OAUTH_LOCAL_PORT) -> None:
        self._result: dict[str, str] = {}
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != OPENAI_OAUTH_CALLBACK_PATH:
                    self.send_error(404)
                    return
                query = parse_qs(parsed.query)
                parent._result = {key: values[-1] for key, values in query.items() if values}
                if parent._result.get("error"):
                    body = "OpenAI OAuth 登录未完成，可关闭此页面并返回终端。".encode()
                    status = 403
                else:
                    body = "OpenAI OAuth 登录成功，可关闭此页面并返回终端。".encode()
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self._server = HTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            raise OpenAIOAuthError(
                f"cannot listen on OAuth callback port {port}; "
                "close the process using it and retry"
            ) from exc

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    def wait_for_code(self, expected_state: str, timeout_seconds: float) -> str:
        deadline = time.monotonic() + timeout_seconds
        try:
            while not self._result:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OpenAIOAuthError("waiting for OpenAI OAuth callback timed out")
                self._server.timeout = remaining
                self._server.handle_request()
        finally:
            self._server.server_close()
        if self._result.get("state") != expected_state:
            raise OpenAIOAuthError("OpenAI OAuth callback state did not match")
        if error := self._result.get("error"):
            raise OpenAIOAuthError(f"OpenAI OAuth authorization failed: {error}")
        code = self._result.get("code")
        if not code:
            raise OpenAIOAuthError("OpenAI OAuth callback did not include an authorization code")
        return code