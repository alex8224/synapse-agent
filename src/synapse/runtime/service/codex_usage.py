"""Query, command, and result DTOs for the Codex usage / reset-credit surface.

The views mirror ``web/src/runtime-client/codexUsage.ts`` field by field: every
declared key is always present, a nullable field carries ``None`` instead of
being omitted, and the numeric / text bounds are the *same* rejection
thresholds the console decoder applies (Unix seconds, percent, minutes, count,
row count, and text length).  A payload the console would reject therefore
cannot be built here either.

Nothing credential-shaped is representable: there is no access token, account
id, OAuth-grant expiry, header, or URL field.  ``CodexResetCredit.expires_at``
is the *credit's* own expiry, never the OAuth grant's ``expires_at``.

``CodexUsageProvider`` is the port the composition root implements (the daemon
supplies a real Codex client adapter); this module stays dependency-free so the
DTO contract can be imported without the execution stack.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "CODEX_CONSUME_OUTCOMES",
    "CodexConsumeResult",
    "CodexResetCredit",
    "CodexResetCreditsView",
    "CodexUsageConflictError",
    "CodexUsageProvider",
    "CodexUsageView",
    "CodexUsageWindow",
    "ConsumeCodexResetCommand",
    "GetCodexResetCreditsQuery",
    "GetCodexUsageQuery",
    "MAX_CODEX_COUNT",
    "MAX_CODEX_CREDITS",
    "MAX_CODEX_DESCRIPTION",
    "MAX_CODEX_ID",
    "MAX_CODEX_MODEL",
    "MAX_CODEX_TIMESTAMP",
    "MAX_CODEX_TITLE",
    "MAX_CODEX_TOKEN",
    "MAX_CODEX_WINDOW_MINUTES",
]

#: Plausible Unix-second ceiling (2100-01-01T00:00:00Z), shared with the console.
MAX_CODEX_TIMESTAMP = 4_102_444_800
#: Longest accepted rate-limit window (one year, in minutes).
MAX_CODEX_WINDOW_MINUTES = 527_040
#: Largest advertised reset-credit count.
MAX_CODEX_COUNT = 1000
#: Bounded reset-credit row count; a longer backend list is never projected.
MAX_CODEX_CREDITS = 200
#: Text bounds, identical to the console decoder's limits.
MAX_CODEX_ID = 128
MAX_CODEX_TOKEN = 64
MAX_CODEX_MODEL = 256
MAX_CODEX_TITLE = 256
MAX_CODEX_DESCRIPTION = 1024

CODEX_CONSUME_OUTCOMES: Final[tuple[str, ...]] = (
    "reset",
    "alreadyRedeemed",
    "nothingToReset",
    "noCredit",
    "unknown",
)


def _wire_units(value: str) -> int:
    """Length in the units ``codexUsage.ts`` counts (JS UTF-16 code units)."""
    return len(value.encode("utf-16-le")) // 2


def _wire_text(value: object, *, name: str, maximum: int) -> str:
    """Validate one non-empty bounded text field without echoing its content."""
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        size = _wire_units(value)
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid text") from None
    if size > maximum:
        raise ValueError(f"{name} exceeds the length limit")
    return value


def _wire_optional_text(value: object, *, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _wire_text(value, name=name, maximum=maximum)


def _wire_number(value: object, *, name: str, maximum: float) -> float:
    """Validate one finite number inside ``0..maximum`` (``bool`` is not one)."""
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if number < 0 or number > maximum:
        raise ValueError(f"{name} is outside the allowed range")
    return number


def _wire_timestamp(value: object, *, name: str) -> float:
    return _wire_number(value, name=name, maximum=float(MAX_CODEX_TIMESTAMP))


def _wire_optional_timestamp(value: object, *, name: str) -> float | None:
    if value is None:
        return None
    return _wire_timestamp(value, name=name)


def _wire_percent(value: object, *, name: str) -> float | None:
    if value is None:
        return None
    return _wire_number(value, name=name, maximum=100.0)


def _wire_minutes(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_CODEX_WINDOW_MINUTES:
        raise ValueError(f"{name} must be an integer in 1..{MAX_CODEX_WINDOW_MINUTES}")
    return value


def _wire_count(value: object, *, name: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CODEX_COUNT:
        raise ValueError(f"{name} must be an integer in 0..{MAX_CODEX_COUNT}")
    return value


def _wire_optional_count(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    return _wire_count(value, name=name)


def _wire_session(value: object, *, name: str) -> SessionRef:
    if type(value) is not SessionRef:
        raise ValueError(f"{name} must be a SessionRef")
    if not value.project_id or not value.thread_id:
        raise ValueError(f"{name} must have non-empty project_id and thread_id")
    _wire_text(value.project_id, name=f"{name} project_id", maximum=MAX_CODEX_ID)
    _wire_text(value.thread_id, name=f"{name} thread_id", maximum=MAX_CODEX_ID)
    return value


@dataclass(frozen=True, slots=True)
class GetCodexUsageQuery:
    """Read the current Codex rate-limit usage for one *open* session."""

    session: SessionRef
    force: bool = False

    def __post_init__(self) -> None:
        _wire_session(self.session, name="session")
        if type(self.force) is not bool:
            raise ValueError("force must be a boolean")


@dataclass(frozen=True, slots=True)
class GetCodexResetCreditsQuery:
    """Read the session's reset-credit rows for one *open* session."""

    session: SessionRef
    force: bool = False

    def __post_init__(self) -> None:
        _wire_session(self.session, name="session")
        if type(self.force) is not bool:
            raise ValueError("force must be a boolean")


@dataclass(frozen=True, slots=True)
class ConsumeCodexResetCommand:
    """Redeem one reset credit.

    ``confirmed`` is the user-consent gate the console always sends as the
    literal ``True``; anything else (including a truthy non-bool) is rejected
    here, so a caller cannot redeem a credit without an explicit confirmation.
    """

    session: SessionRef
    expected_model: str
    credit_id: str
    command_id: str
    confirmed: bool

    def __post_init__(self) -> None:
        _wire_session(self.session, name="session")
        object.__setattr__(
            self,
            "expected_model",
            _wire_text(self.expected_model, name="expected_model", maximum=MAX_CODEX_MODEL),
        )
        object.__setattr__(
            self,
            "credit_id",
            _wire_text(self.credit_id, name="credit_id", maximum=MAX_CODEX_ID),
        )
        object.__setattr__(
            self,
            "command_id",
            _wire_text(self.command_id, name="command_id", maximum=MAX_CODEX_ID),
        )
        if self.confirmed is not True:
            raise ValueError("reset must be explicitly confirmed")


@dataclass(frozen=True, slots=True)
class CodexUsageWindow:
    """One rate-limit window; every field is nullable and always present."""

    used_percent: float | None
    window_minutes: int | None
    reset_at: float | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "used_percent", _wire_percent(self.used_percent, name="used_percent")
        )
        object.__setattr__(
            self, "window_minutes", _wire_minutes(self.window_minutes, name="window_minutes")
        )
        object.__setattr__(
            self, "reset_at", _wire_optional_timestamp(self.reset_at, name="reset_at")
        )


@dataclass(frozen=True, slots=True)
class CodexResetCredit:
    """One reset credit row (the backend's own display fields)."""

    id: str
    reset_type: str
    status: str
    granted_at: float | None
    expires_at: float | None
    title: str | None
    description: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _wire_text(self.id, name="credit id", maximum=MAX_CODEX_ID))
        object.__setattr__(
            self,
            "reset_type",
            _wire_text(self.reset_type, name="credit reset_type", maximum=MAX_CODEX_TOKEN),
        )
        object.__setattr__(
            self,
            "status",
            _wire_text(self.status, name="credit status", maximum=MAX_CODEX_TOKEN),
        )
        object.__setattr__(
            self, "granted_at", _wire_optional_timestamp(self.granted_at, name="credit granted_at")
        )
        object.__setattr__(
            self, "expires_at", _wire_optional_timestamp(self.expires_at, name="credit expires_at")
        )
        object.__setattr__(
            self,
            "title",
            _wire_optional_text(self.title, name="credit title", maximum=MAX_CODEX_TITLE),
        )
        object.__setattr__(
            self,
            "description",
            _wire_optional_text(
                self.description, name="credit description", maximum=MAX_CODEX_DESCRIPTION
            ),
        )


@dataclass(frozen=True, slots=True)
class CodexUsageView:
    """``runtime.codex.usage.get`` result."""

    session: SessionRef
    model: str
    primary: CodexUsageWindow | None
    secondary: CodexUsageWindow | None
    captured_at: float
    available_reset_count: int | None

    def __post_init__(self) -> None:
        _wire_session(self.session, name="session")
        object.__setattr__(
            self, "model", _wire_text(self.model, name="model", maximum=MAX_CODEX_MODEL)
        )
        for name in ("primary", "secondary"):
            window = getattr(self, name)
            if window is not None and type(window) is not CodexUsageWindow:
                raise ValueError(f"{name} must be a CodexUsageWindow or None")
        object.__setattr__(
            self, "captured_at", _wire_timestamp(self.captured_at, name="captured_at")
        )
        object.__setattr__(
            self,
            "available_reset_count",
            _wire_optional_count(self.available_reset_count, name="available_reset_count"),
        )


@dataclass(frozen=True, slots=True)
class CodexResetCreditsView:
    """``runtime.codex.reset_credits.get`` result."""

    session: SessionRef
    model: str
    available_count: int
    credits: tuple[CodexResetCredit, ...]

    def __post_init__(self) -> None:
        _wire_session(self.session, name="session")
        object.__setattr__(
            self, "model", _wire_text(self.model, name="model", maximum=MAX_CODEX_MODEL)
        )
        object.__setattr__(
            self, "available_count", _wire_count(self.available_count, name="available_count")
        )
        rows = tuple(self.credits)
        if len(rows) > MAX_CODEX_CREDITS:
            raise ValueError(f"credits must hold at most {MAX_CODEX_CREDITS} rows")
        if any(type(row) is not CodexResetCredit for row in rows):
            raise ValueError("credits must contain CodexResetCredit values")
        object.__setattr__(self, "credits", rows)


@dataclass(frozen=True, slots=True)
class CodexConsumeResult:
    """``runtime.codex.reset_credits.consume`` result."""

    session: SessionRef
    model: str
    command_id: str
    outcome: Literal["reset", "alreadyRedeemed", "nothingToReset", "noCredit", "unknown"]

    def __post_init__(self) -> None:
        _wire_session(self.session, name="session")
        object.__setattr__(
            self, "model", _wire_text(self.model, name="model", maximum=MAX_CODEX_MODEL)
        )
        object.__setattr__(
            self, "command_id", _wire_text(self.command_id, name="command_id", maximum=MAX_CODEX_ID)
        )
        if self.outcome not in CODEX_CONSUME_OUTCOMES:
            raise ValueError("outcome is not a known reset verb")


class CodexUsageConflictError(RuntimeError):
    """The provider refused a replay whose parameters no longer match.

    Raised across the provider port only; the service maps it to the fixed
    ``ConflictError`` copy, so a provider message never reaches the wire.
    """


class CodexUsageProvider(Protocol):
    """Port implemented by the composition root (daemon adapter).

    Every method is called with the *server-resolved* session and model: the
    service has already verified that the session is open, that its selected
    profile uses Codex OAuth, and (for a consume) that the confirmation and the
    expected model match.
    """

    async def get_usage(
        self, session: SessionRef, model: str, force: bool
    ) -> CodexUsageView: ...

    async def get_reset_credits(
        self, session: SessionRef, model: str, force: bool
    ) -> CodexResetCreditsView: ...

    async def consume_reset(
        self, command: ConsumeCodexResetCommand, model: str
    ) -> CodexConsumeResult: ...
