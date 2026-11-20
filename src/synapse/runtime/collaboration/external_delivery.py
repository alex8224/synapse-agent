"""Deliver a collaboration message to an ACP peer bound to the target session.

A collaboration target is normally a Synapse session: the delivery becomes an
ordinary turn and the target's Agent answers through ``reply_to_collaboration``.
A session bound to an external ACP Agent (``sessions.external_agent_id``) has no
Synapse Agent and no Synapse tools at all, so this module routes the delivery to
the ACP peer instead and treats the peer's own final answer as the reply.

The ACP capability arrives as an injected port, so this module never imports
``synapse.acp``: the daemon hands in its external session service.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from synapse.runtime.collaboration.runtime import (
    CollaborationMessage,
    DeliveryExecutor,
    TargetUnavailableError,
)
from synapse.runtime.sessions.errors import SessionBusyError
from synapse.runtime.sessions.ref import SessionRef

__all__ = ["ExternalDeliveryExecutor", "ExternalDeliveryPort"]

#: ACP stop reasons mapped to a delivery failure reason. ``None`` is success.
_STOP_REASON_FAILURE = {
    "end_turn": None,
    "cancelled": "execution_cancelled",
    "max_tokens": "execution_failed",
    "max_turn_requests": "execution_failed",
    "refusal": "execution_failed",
}


class ExternalDeliveryPort(Protocol):
    """The ACP-side capabilities one delivery needs.

    Implemented by the daemon's external session service; kept structural so the
    collaboration package stays free of ``synapse.acp`` imports.
    """

    def binding(self, settings: object, thread_id: str) -> tuple[str, str | None] | None:
        """Return the configured Agent id for *thread_id*, or ``None``."""

    def active_turn_id(self, ref: SessionRef) -> str | None:
        """Return the peer's active turn id, or ``None`` when it is idle."""

    async def deliver_turn(
        self,
        ref: SessionRef,
        settings: object,
        workspace: Path,
        *,
        turn_id: str,
        text: str,
    ) -> tuple[str | None, str | None]:
        """Run one turn on the peer; return ``(stop_reason, answer)``."""

    async def cancel(self, ref: SessionRef) -> bool:
        """Ask the peer to stop its active turn."""


@dataclass(slots=True)
class _Run:
    """Delivery token: either the native turn or the running ACP turn."""

    turn_id: str
    external: bool = False
    task: asyncio.Task[Any] | None = None
    answer: str | None = field(default=None)


def _consume(task: asyncio.Task[Any]) -> None:
    """Never leave an unretrieved exception on a delivery task."""
    with contextlib.suppress(BaseException):
        task.exception()


class ExternalDeliveryExecutor:
    """Route each delivery to the ACP peer when the recipient is bound to one."""

    def __init__(
        self,
        native: DeliveryExecutor,
        *,
        provider: Callable[[], Any | None],
        manager_for: Callable[[str], Any | None],
    ) -> None:
        self._native = native
        self._provider = provider
        self._manager_for = manager_for

    # -- routing ------------------------------------------------------------

    def _external_target(
        self, recipient: SessionRef
    ) -> tuple[Any, object, Path] | None:
        """Return ``(provider, settings, workspace)`` when *recipient* is a peer.

        ``None`` means "not an external session, use the native path".  A bound
        session whose workspace is gone is a named failure instead: silently
        running a native turn in that thread would mix two backends.
        """
        provider = self._provider()
        if provider is None:
            return None
        manager = self._manager_for(recipient.project_id)
        settings = None if manager is None else getattr(manager, "settings", None)
        if settings is None:
            return None
        try:
            binding = provider.binding(settings, recipient.thread_id)
        except Exception:  # noqa: BLE001 - an unreadable store is "not external"
            return None
        if binding is None:
            return None
        workspace = Path(str(getattr(settings, "workspace", "") or "")).expanduser()
        if not workspace.is_dir():
            raise TargetUnavailableError("workspace_unavailable")
        return provider, settings, workspace

    def tool_reply(self, recipient: SessionRef) -> bool:
        """False when the recipient is an ACP peer with no Synapse tools."""
        try:
            return self._external_target(recipient) is None
        except TargetUnavailableError:
            # Bound but unusable: keep the native wording; ``start`` reports it.
            return True

    # -- delivery -----------------------------------------------------------

    async def start(self, message: CollaborationMessage) -> Any:
        """Start the delivery; the token is ``_Run`` for a peer, native otherwise."""
        target = self._external_target(message.recipient)
        if target is None:
            return await self._native.start(message)
        provider, settings, workspace = target
        if provider.active_turn_id(message.recipient) is not None:
            # The peer is busy with its own work: keep the claim so the target's
            # queue stays FIFO, exactly like a busy native session.
            raise SessionBusyError("external session already has an active turn")
        turn_id = uuid.uuid4().hex
        task = asyncio.create_task(
            provider.deliver_turn(
                message.recipient,
                settings,
                workspace,
                turn_id=turn_id,
                text=message.text,
            ),
            name=f"collaboration-external-{turn_id}",
        )
        task.add_done_callback(_consume)
        return _Run(turn_id=turn_id, external=True, task=task)

    async def settled(
        self, message: CollaborationMessage, token: Any
    ) -> str | None:
        if not getattr(token, "external", False):
            return await self._native.settled(message, token)
        assert token.task is not None
        try:
            stop_reason, answer = await token.task
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed turn is a recorded outcome
            return "execution_failed"
        token.answer = answer
        return _STOP_REASON_FAILURE.get(stop_reason, "execution_failed")

    def answer(self, message: CollaborationMessage, token: Any) -> str | None:
        """The peer's final answer, which becomes the assignment's reply."""
        return getattr(token, "answer", None) if getattr(token, "external", False) else None

    async def cancel(self, message: CollaborationMessage, token: Any) -> None:
        if not getattr(token, "external", False):
            await self._native.cancel(message, token)
            return
        provider = self._provider()
        if provider is None:
            return
        try:
            # Asking the peer to stop lets the turn settle with a ``cancelled``
            # stop reason, so the delivery outcome stays observable.
            await provider.cancel(message.recipient)
        except Exception:  # noqa: BLE001 - the turn may already have ended
            return
