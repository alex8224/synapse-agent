"""Read-only Codex OAuth usage snapshots for the TUI bottombar."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from synapse.integrations.openai_oauth import OpenAIOAuthStore, OpenAIOAuthTokenProvider

OPENAI_USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
OPENAI_RESET_CREDITS_ENDPOINT = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
)
OPENAI_CONSUME_RESET_ENDPOINT = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
)
USAGE_CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """One Codex rate-limit window."""
    used_percent: float | None
    window_minutes: int | None
    reset_at: float | None
    @property
    def remaining_percent(self) -> int | None:
        if self.used_percent is None:
            return None
        return round(max(0.0, min(100.0, 100.0 - self.used_percent)))


@dataclass(frozen=True, slots=True)
class ResetCreditDetail:
    """One available / consumed rate-limit reset credit."""
    id: str
    reset_type: str
    status: str
    granted_at: float | None
    expires_at: float | None
    title: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class ResetCredits:
    """Aggregated reset-credit snapshot from the backend."""
    available_count: int
    credits: list[ResetCreditDetail] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ConsumeResetResult:
    """Outcome of a single reset-credit redemption."""
    outcome: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CodexUsageSnapshot:
    """Parsed Codex primary/secondary usage windows + optional reset credits."""
    primary: UsageWindow | None = None
    secondary: UsageWindow | None = None
    captured_at: float = 0.0
    expires_at: float | None = None
    reset_credits: ResetCredits | None = None
    @property
    def lowest_remaining_percent(self) -> int | None:
        values = [
            value for value in (
                self.primary.remaining_percent if self.primary else None,
                self.secondary.remaining_percent if self.secondary else None,
            ) if value is not None
        ]
        return min(values) if values else None


class CodexUsageClient:
    """Fetch and cache the Codex OAuth usage / reset-credit endpoints."""
    def __init__(self, *, store=None, timeout=10.0, cache_ttl=USAGE_CACHE_TTL_SECONDS):
        self._store = store or OpenAIOAuthStore()
        self._token_provider = OpenAIOAuthTokenProvider(self._store)
        self._timeout = max(1.0, float(timeout))
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._lock = threading.RLock()
        self._cached: CodexUsageSnapshot | None = None
        self._cached_at = 0.0
        self._cached_details: ResetCredits | None = None
        self._cached_details_at = 0.0

    def _auth_headers(self) -> dict[str, str]:
        tokens = self._store.load()
        if tokens is None:
            raise RuntimeError("OpenAI Codex OAuth is not logged in")
        access_token = self._token_provider.access_token()
        tokens = self._store.load() or tokens
        headers: dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "originator": "synapse",
        }
        if tokens.account_id:
            headers["ChatGPT-Account-Id"] = tokens.account_id
        return headers

    def get_cached(self) -> CodexUsageSnapshot | None:
        with self._lock:
            if self._cached is None:
                return None
            if time.monotonic() - self._cached_at > self._cache_ttl:
                return None
            return self._cached

    def fetch(self, *, force=False) -> CodexUsageSnapshot:
        with self._lock:
            if not force:
                cached = self.get_cached()
                if cached is not None:
                    return cached
        headers = self._auth_headers()
        response = httpx.get(OPENAI_USAGE_ENDPOINT, headers=headers, timeout=self._timeout)
        response.raise_for_status()
        payload = response.json()
        tokens = self._store.load()
        snapshot = parse_usage_payload(payload, expires_at=tokens.expires_at if tokens else None)
        with self._lock:
            self._cached = snapshot
            self._cached_at = time.monotonic()
        return snapshot

    def get_cached_details(self) -> ResetCredits | None:
        with self._lock:
            if self._cached_details is None:
                return None
            if time.monotonic() - self._cached_details_at > self._cache_ttl:
                return None
            return self._cached_details

    def fetch_reset_credits(self, *, force=False) -> ResetCredits:
        with self._lock:
            if not force:
                cached = self.get_cached_details()
                if cached is not None:
                    return cached
        headers = self._auth_headers()
        response = httpx.get(OPENAI_RESET_CREDITS_ENDPOINT, headers=headers, timeout=self._timeout)
        response.raise_for_status()
        details = parse_reset_credits_details(response.json())
        with self._lock:
            self._cached_details = details
            self._cached_details_at = time.monotonic()
        return details

    def consume_reset_credit(self, *, credit_id=None, idempotency_key=None) -> ConsumeResetResult:
        key = idempotency_key or uuid.uuid4().hex
        headers = self._auth_headers()
        body: dict[str, str] = {"redeem_request_id": key}
        if credit_id:
            body["credit_id"] = credit_id
        response = httpx.post(
            OPENAI_CONSUME_RESET_ENDPOINT, headers=headers, json=body, timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        outcome = str(payload.get("outcome") or payload.get("code") or "unknown")
        return ConsumeResetResult(outcome=outcome, idempotency_key=key)


def parse_usage_payload(payload, *, expires_at=None, captured_at=None) -> CodexUsageSnapshot:
    if not isinstance(payload, dict):
        raise ValueError("Codex usage response must be a JSON object")
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise ValueError("Codex usage response has no rate_limit object")
    return CodexUsageSnapshot(
        primary=_parse_window(rate_limit.get("primary_window")),
        secondary=_parse_window(rate_limit.get("secondary_window")),
        captured_at=time.time() if captured_at is None else float(captured_at),
        expires_at=expires_at,
        reset_credits=_parse_reset_credits_summary(payload.get("rate_limit_reset_credits")),
    )


def parse_reset_credits_details(payload) -> ResetCredits:
    if not isinstance(payload, dict):
        raise ValueError("Reset-credits response must be a JSON object")
    count = _as_int(payload.get("available_count")) or 0
    raw = payload.get("credits")
    credits: list[ResetCreditDetail] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                credits.append(_parse_reset_credit_detail(item))
    return ResetCredits(available_count=count, credits=credits)


def _parse_reset_credits_summary(raw) -> ResetCredits | None:
    if not isinstance(raw, dict):
        return None
    count = _as_int(raw.get("available_count"))
    if count is None:
        return None
    raw_list = raw.get("credits")
    credits: list[ResetCreditDetail] = []
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                credits.append(_parse_reset_credit_detail(item))
    return ResetCredits(available_count=count, credits=credits)


def _parse_reset_credit_detail(raw: dict[str, Any]) -> ResetCreditDetail:
    return ResetCreditDetail(
        id=str(raw.get("id") or ""),
        reset_type=str(raw.get("resetType") or raw.get("reset_type") or "unknown"),
        status=str(raw.get("status") or "unknown"),
        granted_at=_as_timestamp(raw.get("grantedAt") or raw.get("granted_at")),
        expires_at=_as_timestamp(raw.get("expiresAt") or raw.get("expires_at")),
        title=raw.get("title") or None,
        description=raw.get("description") or None,
    )


def _as_timestamp(value: Any) -> float | None:
    """Parse Unix seconds or an ISO 8601 timestamp from backend responses."""
    number = _as_float(value)
    if number is not None:
        return number
    if not isinstance(value, str) or not value.strip():
        return None
    from datetime import datetime

    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _parse_window(raw) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None
    used = _as_float(raw.get("used_percent"))
    if used is None:
        return None
    seconds = _as_int(raw.get("limit_window_seconds"))
    return UsageWindow(
        used_percent=max(0.0, min(100.0, used)),
        window_minutes=(seconds + 59) // 60 if seconds and seconds > 0 else None,
        reset_at=_as_float(raw.get("reset_at")),
    )


def _as_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def format_reset_remaining(reset_at, *, now=None) -> str:
    if reset_at is None:
        return "--"
    seconds = max(0, int(reset_at - (time.time() if now is None else now)))
    if seconds < 60:
        return f"{seconds}s"
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = (minutes + 59) // 60
    if hours < 24:
        return f"{hours}h"
    return f"{(hours + 23) // 24}d"


def format_usage_label(snapshot) -> str:
    if snapshot is None:
        return "codex n/a"
    parts: list[str] = []
    for label, window in (("5h", snapshot.primary), ("1d", snapshot.secondary)):
        if window is None or window.remaining_percent is None:
            continue
        reset = format_reset_remaining(window.reset_at)
        parts.append(f"{label} {window.remaining_percent}%/{reset}")
    if snapshot.reset_credits is not None and snapshot.reset_credits.available_count > 0:
        parts.append(f"resets {snapshot.reset_credits.available_count}")
    if snapshot.expires_at is not None:
        parts.append(f"exp {format_reset_remaining(snapshot.expires_at)}")
    return " · ".join(parts) if parts else "codex n/a"


def usage_style(snapshot) -> str:
    if snapshot is not None and snapshot.lowest_remaining_percent is not None:
        if snapshot.lowest_remaining_percent < 50:
            return "#f28b82"
    return "#8ab4f8"


# ---------------------------------------------------------------------------
# TUI-facing service — owns cache / refresh / consume state
# ---------------------------------------------------------------------------


class CodexUsageService:
    """State holder that the TUI binds to a bottombar component.

    The TUI calls :meth:`refresh_usage` and :meth:`open_reset_dialog` on a
    Textual worker thread.  This object stores the latest snapshot / details
    and exposes a ``label`` property for the render path so the bottombar
    never blocks on I/O.
    """

    def __init__(self, settings=None) -> None:
        self._client = CodexUsageClient()
        self.snapshot: CodexUsageSnapshot | None = None
        self.loading = False
        self.error = False
        self.reset_credits: ResetCredits | None = None
        self.consuming = False
        self._last_refresh = 0.0
        self._settings = settings

    # -- helpers called from the TUI UI thread --------------------------------

    @property
    def label(self) -> object:
        """Return rich Text or a plain str for the bottombar render path."""
        from rich.text import Text

        if self.loading and self.snapshot is None:
            return Text("codex ...", style="#5f6368")
        if self.snapshot is None:
            return Text("codex n/a", style="#5f6368")
        return Text(
            format_usage_label(self.snapshot),
            style=usage_style(self.snapshot),
        )

    def has_oauth_profile(self) -> bool:
        """Return True when the active model profile uses Codex OAuth."""
        settings = self._settings
        if settings is None:
            return False
        try:
            from synapse.models.registry import registry_from_settings

            registry = registry_from_settings(settings)
            selected = getattr(settings, "active_model", None) or registry.default
            return registry.get(selected).auth == "openai_oauth"
        except Exception:  # noqa: BLE001
            return False

    def invalidate(self) -> None:
        """Clear all state (non-OAuth model selected)."""
        self.snapshot = None
        self.loading = False
        self.error = False
        self.reset_credits = None
        self._last_refresh = 0.0

    # -- refresh (called from TUI worker thread) ------------------------------

    def should_refresh(self, *, force: bool = False) -> bool:
        if not self.has_oauth_profile():
            return False
        if force:
            return True
        if time.monotonic() - self._last_refresh < 300:
            return False
        if self.loading:
            return False
        return True

    def refresh_usage(self, *, force: bool = False) -> None:
        """Blocking call — run on a worker thread."""
        self.loading = True
        self.error = False
        self._last_refresh = time.monotonic()
        try:
            self.snapshot = self._client.fetch(force=force)
            self.loading = False
        except Exception:  # noqa: BLE001
            self.loading = False
            self.error = True

    def fetch_reset_credits(self) -> None:
        """Blocking call — fetch detailed reset-credit rows."""
        try:
            self.reset_credits = self._client.fetch_reset_credits()
        except Exception:  # noqa: BLE001
            pass

    def consume_reset(self, credit_id: str) -> ConsumeResetResult:
        """Blocking call — redeem one reset credit."""
        return self._client.consume_reset_credit(credit_id=credit_id)
