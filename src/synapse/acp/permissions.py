"""ACP permission request coordination and session-scoped policy state."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any


class ACPPermissionError(RuntimeError):
    """Permission cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """One deterministic decision for one pending runtime action."""

    kind: str
    option_id: str | None = None
    message: str | None = None


@dataclass(slots=True)
class PendingPermission:
    permission_id: str
    session_id: str
    prompt_id: str
    turn_id: str
    tool_call_id: str
    action_name: str
    task: asyncio.Task[Any]
    state: str = "pending"


class PermissionCoordinator:
    """Coordinate Client permission RPCs without default approval.

    The registry is session-scoped. ``allow_always`` and ``reject_always`` are
    keyed by the normalized action name and never cross ACP sessions.
    """

    def __init__(self, *, request_timeout: float = 120.0) -> None:
        self._pending: dict[str, PendingPermission] = {}
        self._policies: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()
        self._request_timeout = max(0.01, float(request_timeout))

    async def resolve(
        self,
        *,
        session_id: str,
        prompt_id: str,
        turn_id: str,
        tool_call_id: str,
        action_name: str,
        request: Any,
        options: list[Any],
        request_permission: Any,
    ) -> PermissionDecision:
        """Request one decision and atomically settle/remove its registry entry."""
        policy_key = (session_id, self._policy_key(action_name))
        async with self._lock:
            cached = self._policies.get(policy_key)
        if cached is not None:
            return PermissionDecision(kind=cached, option_id=cached)

        permission_id = f"{session_id}:{prompt_id}:{turn_id}:{tool_call_id}"
        request_task = asyncio.create_task(request_permission(request, options))
        pending = PendingPermission(
            permission_id=permission_id,
            session_id=session_id,
            prompt_id=prompt_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            action_name=action_name,
            task=request_task,
        )
        async with self._lock:
            if permission_id in self._pending:
                request_task.cancel()
                raise ACPPermissionError("duplicate pending permission")
            self._pending[permission_id] = pending

        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(request_task), timeout=self._request_timeout
            )
            decision = self._decode_outcome(outcome, options)
            if decision.kind in {"allow_always", "reject_always"}:
                async with self._lock:
                    if pending.state == "pending":
                        self._policies[policy_key] = decision.kind
            async with self._lock:
                if pending.state == "cancelled":
                    return PermissionDecision(kind="cancelled")
                pending.state = "settled"
            return decision
        except asyncio.CancelledError:
            async with self._lock:
                cancelled = pending.state == "cancelled"
                pending.state = "cancelled"
            if not cancelled and not request_task.done():
                request_task.cancel()
            if cancelled:
                return PermissionDecision(kind="cancelled")
            raise
        except Exception as exc:
            async with self._lock:
                pending.state = "timeout" if isinstance(exc, TimeoutError) else "failed"
            if isinstance(exc, TimeoutError) and not request_task.done():
                request_task.cancel()
            if isinstance(exc, ACPPermissionError):
                raise
            if isinstance(exc, TimeoutError):
                raise ACPPermissionError("permission request timed out") from exc
            raise ACPPermissionError("permission request failed") from exc
        finally:
            if not request_task.done():
                request_task.cancel()
            with contextlib.suppress(BaseException):
                await request_task
            async with self._lock:
                self._pending.pop(permission_id, None)

    async def cancel_session(self, session_id: str) -> None:
        """Mark all session permissions cancelled; never approve on cancellation."""
        current = asyncio.current_task()
        async with self._lock:
            pending = [
                item
                for item in self._pending.values()
                if item.session_id == session_id
            ]
            for item in pending:
                item.state = "cancelled"
                if item.task is not current and not item.task.done():
                    item.task.cancel()
            self._policies = {
                key: value for key, value in self._policies.items() if key[0] != session_id
            }

    async def clear_session(self, session_id: str) -> None:
        await self.cancel_session(session_id)

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = {item.session_id for item in self._pending.values()}
        for session_id in sessions:
            await self.cancel_session(session_id)
        async with self._lock:
            self._pending.clear()
            self._policies.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_for_session(self, session_id: str) -> tuple[PendingPermission, ...]:
        return tuple(
            item for item in self._pending.values() if item.session_id == session_id
        )

    @staticmethod
    def _policy_key(action_name: str) -> str:
        return " ".join(str(action_name).strip().casefold().split())

    @staticmethod
    def _decode_outcome(outcome: Any, options: list[Any]) -> PermissionDecision:
        outcome_name = str(getattr(outcome, "outcome", ""))
        if outcome_name == "cancelled":
            return PermissionDecision(kind="cancelled")
        if outcome_name != "selected":
            raise ACPPermissionError("permission client returned an invalid outcome")
        option_id = str(getattr(outcome, "option_id", ""))
        by_id = {str(getattr(option, "option_id", "")): option for option in options}
        option = by_id.get(option_id)
        if option is None:
            raise ACPPermissionError("permission client selected an unknown option")
        kind = str(getattr(option, "kind", ""))
        if kind not in {"allow_once", "allow_always", "reject_once", "reject_always"}:
            raise ACPPermissionError("permission option has an invalid kind")
        if kind.startswith("allow"):
            return PermissionDecision(kind=kind, option_id=option_id)
        return PermissionDecision(kind=kind, option_id=option_id, message="rejected by client")
