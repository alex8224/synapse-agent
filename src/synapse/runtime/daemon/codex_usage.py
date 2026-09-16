"""Daemon-side Codex usage / reset-credit provider.

This is the business adapter the composition root injects into
``LocalAgentRuntimeService`` as ``codex_usage_provider``.  It owns one lazily
created :class:`~synapse.integrations.openai_usage.CodexUsageClient` and turns
its blocking calls into ``to_thread`` work, so the event loop never blocks on
HTTP and no request is issued from the loop thread.

Three rules shape it:

- **Serialized.** The endpoints are account-scoped, so every client call runs
  under one process-wide lock: an overlapping usage read could otherwise return
  a snapshot that predates a redemption, and two consumes could interleave.
- **Idempotent.** ``command_id`` is the client-minted idempotency key.  A
  replayed command returns the recorded result, a replay whose fingerprint
  (session, model, credit) changed is refused, and a credit whose consume never
  resolved is never retried with a *new* key (that could redeem it twice).
- **Bounded and redacted.** The replay ledger and the unresolved-credit set are
  bounded, and no view carries the OAuth grant's ``expires_at`` (it is not a
  rate-limit reset time), a token, or an account id.  Upstream failures are
  raised unchanged for the service to replace with fixed copy, so no response
  text or credential can reach a client.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import OrderedDict

from synapse.integrations.openai_usage import CodexUsageClient, CodexUsageSnapshot, ResetCredits
from synapse.runtime.service.codex_usage import (
    MAX_CODEX_COUNT,
    MAX_CODEX_CREDITS,
    MAX_CODEX_TIMESTAMP,
    MAX_CODEX_WINDOW_MINUTES,
    CodexConsumeResult,
    CodexResetCredit,
    CodexResetCreditsView,
    CodexUsageConflictError,
    CodexUsageView,
    CodexUsageWindow,
    ConsumeCodexResetCommand,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = ["CodexUsageAdapter", "MAX_CODEX_COMMAND_RECORDS"]

#: Bound for the replay ledger and the unresolved-credit set (oldest first).
MAX_CODEX_COMMAND_RECORDS = 64

#: Upstream verb -> the five wire outcomes.  No alias is invented for a verb
#: this surface does not know: an unexpected answer is reported as ``unknown``.
_OUTCOME_ALIASES = {
    "reset": "reset",
    "alreadyredeemed": "alreadyRedeemed",
    "nothingtoreset": "nothingToReset",
    "nocredit": "noCredit",
    "unknown": "unknown",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _clamp(value: object, *, maximum: float) -> float | None:
    """Clamp an upstream number into ``0..maximum`` (``None`` when not a number)."""
    number = _number(value)
    if number is None:
        return None
    return min(max(number, 0.0), maximum)


def _timestamp(value: object) -> float | None:
    """Keep an upstream Unix-second field only inside the wire range."""
    number = _number(value)
    if number is None or number < 0 or number > MAX_CODEX_TIMESTAMP:
        return None
    return number


def _minutes(value: object) -> int | None:
    number = _clamp(value, maximum=float(MAX_CODEX_WINDOW_MINUTES))
    if number is None or number < 1:
        return None
    return int(number)


def _count(value: object) -> int | None:
    number = _clamp(value, maximum=float(MAX_CODEX_COUNT))
    return None if number is None else int(number)


def _window(raw: object) -> CodexUsageWindow | None:
    if raw is None:
        return None
    return CodexUsageWindow(
        used_percent=_clamp(getattr(raw, "used_percent", None), maximum=100.0),
        window_minutes=_minutes(getattr(raw, "window_minutes", None)),
        reset_at=_timestamp(getattr(raw, "reset_at", None)),
    )


def _credit_rows(credits: object) -> tuple[CodexResetCredit, ...]:
    """Project bounded credit rows, dropping a row the console would reject."""
    rows: list[CodexResetCredit] = []
    source = credits if isinstance(credits, (list, tuple)) else ()
    for credit in list(source)[:MAX_CODEX_CREDITS]:
        try:
            rows.append(
                CodexResetCredit(
                    id=str(getattr(credit, "id", "") or ""),
                    reset_type=str(getattr(credit, "reset_type", "") or "unknown"),
                    status=str(getattr(credit, "status", "") or "unknown"),
                    granted_at=_timestamp(getattr(credit, "granted_at", None)),
                    expires_at=_timestamp(getattr(credit, "expires_at", None)),
                    title=getattr(credit, "title", None) or None,
                    description=getattr(credit, "description", None) or None,
                )
            )
        except ValueError:
            continue
    return tuple(rows)


def _map_outcome(raw: object) -> str:
    if not isinstance(raw, str):
        return "unknown"
    key = "".join(char for char in raw.strip().casefold() if char.isalnum())
    return _OUTCOME_ALIASES.get(key, "unknown")


def _is_redeemable(credits: ResetCredits, credit_id: str, *, now: float) -> bool:
    """Whether ``credit_id`` is currently redeemable in a fetched credit list."""
    for credit in credits.credits:
        if credit.id != credit_id or not credit.id:
            continue
        if str(credit.status or "").strip().casefold() != "available":
            return False
        expires_at = credit.expires_at
        if expires_at is not None:
            expiry = _timestamp(expires_at)
            if expiry is None or expiry <= now:
                return False
        return True
    return False


class CodexUsageAdapter:
    """Real :class:`CodexUsageProvider` implementation for the daemon."""

    def __init__(self) -> None:
        # One lock serializes every client call (reads and the consume write):
        # the endpoints are account-scoped, so interleaving them could serve a
        # snapshot that predates a redemption.  All ledger state is mutated
        # under this lock, on the worker thread, never on the event loop.
        self._lock = threading.RLock()
        self._client: CodexUsageClient | None = None
        #: command_id -> (fingerprint, settled result); bounded replay ledger.
        self._commands: OrderedDict[str, tuple[tuple[str, ...], CodexConsumeResult]] = OrderedDict()
        #: Credit ids whose consume never resolved (in flight, failed, unknown).
        self._unresolved: set[tuple[str, str]] = set()

    # -- provider port -----------------------------------------------------

    async def get_usage(
        self, session: SessionRef, model: str, force: bool
    ) -> CodexUsageView:
        """Read the usage summary off the loop thread (never on the render path)."""
        snapshot = await asyncio.to_thread(self._fetch_usage, force)
        captured = _clamp(snapshot.captured_at, maximum=float(MAX_CODEX_TIMESTAMP))
        summary = snapshot.reset_credits
        return CodexUsageView(
            session=session,
            model=model,
            primary=_window(snapshot.primary),
            secondary=_window(snapshot.secondary),
            captured_at=0.0 if captured is None else captured,
            # The OAuth grant's ``expires_at`` is deliberately not projected.
            available_reset_count=None if summary is None else _count(summary.available_count),
        )

    async def get_reset_credits(
        self, session: SessionRef, model: str, force: bool
    ) -> CodexResetCreditsView:
        details = await asyncio.to_thread(self._fetch_credits, force)
        return CodexResetCreditsView(
            session=session,
            model=model,
            available_count=_count(details.available_count) or 0,
            credits=_credit_rows(details.credits),
        )

    async def consume_reset(
        self, command: ConsumeCodexResetCommand, model: str
    ) -> CodexConsumeResult:
        return await asyncio.to_thread(self._consume, command, model)

    # -- client plumbing ---------------------------------------------------

    def _new_client(self) -> CodexUsageClient:
        """Create the real client lazily (a seam for an injected test double)."""
        return CodexUsageClient()

    def _codex_client(self) -> CodexUsageClient:
        client = self._client
        if client is None:
            client = self._new_client()
            self._client = client
        return client

    def _fetch_usage(self, force: bool) -> CodexUsageSnapshot:
        with self._lock:
            return self._codex_client().fetch(force=force)

    def _fetch_credits(self, force: bool) -> ResetCredits:
        with self._lock:
            return self._codex_client().fetch_reset_credits(force=force)

    # -- consume / idempotency ---------------------------------------------

    def _consume(
        self, command: ConsumeCodexResetCommand, model: str
    ) -> CodexConsumeResult:
        with self._lock:
            client = self._codex_client()
            account = client.account_key() or "unknown-account"
            fingerprint = (
                account, command.session.project_id, command.session.thread_id,
                model, command.credit_id,
            )
            credit_key = (account, command.credit_id)
            recorded = self._commands.get(command.command_id)
            if recorded is not None:
                if recorded[0] != fingerprint:
                    raise CodexUsageConflictError(
                        "command id was already used for another reset request"
                    )
                self._commands.move_to_end(command.command_id)
                return recorded[1]
            if credit_key in self._unresolved:
                # A previous attempt for this credit never resolved.  A new
                # idempotency key could redeem it twice, so report the unknown
                # state instead of sending anything.
                return self._settle(command, model, fingerprint, "unknown")
            for previous, result in self._commands.values():
                if (previous[0], previous[-1]) == credit_key and result.outcome in {
                    "reset", "alreadyRedeemed"
                }:
                    return self._settle(command, model, fingerprint, "alreadyRedeemed")
            if len(self._unresolved) >= MAX_CODEX_COMMAND_RECORDS:
                # Never evict an unresolved write just to permit another POST.
                raise CodexUsageConflictError("too many unresolved reset requests")
            if not _is_redeemable(
                client.fetch_reset_credits(force=True), command.credit_id, now=time.time()
            ):
                return self._settle(command, model, fingerprint, "noCredit")
            if client.account_key() != account:
                raise CodexUsageConflictError("account changed before reset")
            self._unresolved.add(credit_key)
            try:
                raw = client.consume_reset_credit(
                    credit_id=command.credit_id,
                    idempotency_key=command.command_id,
                    expected_account_key=account,
                )
            except Exception:  # noqa: BLE001 - POST may have applied; never retry it
                outcome = "unknown"
            else:
                outcome = _map_outcome(getattr(raw, "outcome", None))
            finally:
                # A redemption moves the usage windows and the credit rows at
                # once, so both cached copies are dropped whatever happened (the
                # request may have been applied even when the call failed).
                client.invalidate()
            if outcome != "unknown":
                self._unresolved.discard(credit_key)
            return self._settle(command, model, fingerprint, outcome)

    def _settle(
        self,
        command: ConsumeCodexResetCommand,
        model: str,
        fingerprint: tuple[str, ...],
        outcome: str,
    ) -> CodexConsumeResult:
        result = CodexConsumeResult(
            session=command.session,
            model=model,
            command_id=command.command_id,
            outcome=outcome,  # type: ignore[arg-type]
        )
        self._commands[command.command_id] = (fingerprint, result)
        self._commands.move_to_end(command.command_id)
        while len(self._commands) > MAX_CODEX_COMMAND_RECORDS:
            self._commands.popitem(last=False)
        return result
