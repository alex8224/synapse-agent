"""Collaboration business operations over the store, plus worker wakeups.

This is the only place that decides what a collaboration action *means*; the
store owns the durable facts and the delivery runtime owns execution. Neither
the browser nor an Agent tool calls the store directly for these actions.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from synapse.runtime.collaboration.model import (
    ContinuationScope,
    Message,
    MessageKind,
    NotPermittedError,
    Request,
    ScopeState,
    TargetUnavailableError,
)
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.sessions.ref import SessionRef

__all__ = ['CancelTurn', 'CollaborationCoordinator', 'Wake']

#: ``(recipient, turn_id) -> cancellation requested``.
CancelTurn = Callable[[SessionRef, str], bool]
#: Ask the delivery runtime to look at one recipient now instead of on its poll.
Wake = Callable[[SessionRef], None]
#: Deployment policy: may this source session contact that target session?
Allows = Callable[[SessionRef, SessionRef], bool]
#: ``None`` when the target is reachable, else a short machine-readable reason.
#: A reason and not a bool on purpose: "not registered" and "registered but
#: unusable" need different answers, and the reader deserves to see which.
Reachable = Callable[[SessionRef], str | None]


class CollaborationCoordinator:
    """Write collaboration facts, then wake the delivery runtime.

    ``cancel_turn`` and ``wake`` are injected so the daemon supplies the real
    runtime while an offline test supplies doubles. A failing cancellation must
    never undo the durable stop fence, so it is swallowed after the fence lands.
    """

    def __init__(
        self,
        store: CollaborationStore,
        *,
        wake: Wake | None = None,
        cancel_turn: CancelTurn | None = None,
        allows: Allows | None = None,
        reachable: Reachable | None = None,
    ) -> None:
        self._store = store
        self._wake = wake
        self._cancel_turn = cancel_turn
        self._allows = allows
        self._reachable = reachable

    def _notify(self, recipient: SessionRef) -> None:
        if self._wake is not None:
            self._wake(recipient)

    def begin_work(self, source: SessionRef, work_id: str) -> ContinuationScope:
        """Open (or re-join) one work generation for a source session."""
        return self._store.begin_scope(source, work_id)

    def work(self, collaboration_id: str, source: SessionRef) -> ContinuationScope:
        """Resolve the commander's explicit collaboration identity."""
        return self._store.get_scope(collaboration_id, source)

    def current_work(self, source: SessionRef) -> ContinuationScope:
        """The generation a tool call belongs to, created on first use.

        A tool call carries no turn identity, so it joins whatever generation the
        session is in. Creating one here covers a session whose first turn
        predates the feature; it never rotates an existing generation, which is
        what keeps two calls in one turn inside the same request scope.
        """
        scope = self._store.active_scope(source)
        if scope is not None:
            return scope
        return self._store.begin_scope(source, f'adhoc-{uuid.uuid4().hex}')

    def request(
        self,
        scope_id: str,
        source: SessionRef,
        target: SessionRef,
        *,
        operation_key: str,
        body: str,
        anchor_turn_id: str,
    ) -> Request:
        """Save one request and its first delivery, then wake the target."""
        if self._allows is not None and not self._allows(source, target):
            raise NotPermittedError('this deployment does not allow that target')
        if self._reachable is not None:
            reason = self._reachable(target)
            if reason is not None:
                # A different failure from the policy one on purpose: "you may
                # not" and "it is not there" need different answers.
                raise TargetUnavailableError(reason)
        request = self._store.create_request(
            scope_id,
            source,
            target,
            operation_key=operation_key,
            body=body,
            anchor_turn_id=anchor_turn_id,
        )
        self._notify(request.target)
        return request

    def respond(
        self,
        request_id: str,
        actor: SessionRef,
        *,
        kind: MessageKind,
        body: str,
        operation_key: str,
        anchor_turn_id: str,
    ) -> Message:
        """Save one clarification, supplement or final reply, then wake the peer."""
        request = self._store.get_request(request_id, actor)
        message = self._store.post_message(
            request_id,
            actor,
            kind=kind,
            body=body,
            operation_key=operation_key,
            anchor_turn_id=anchor_turn_id,
        )
        recipient = request.target if actor == request.source else request.source
        self._notify(recipient)
        return message

    def stop(
        self,
        scope_id: str,
        source: SessionRef,
        *,
        expected_version: int,
        receive_only: bool = False,
    ) -> ContinuationScope:
        """Fence the generation first, then cancel the turns it already started.

        The order is the point: once the fence is durable no new turn of this
        generation may start, so a turn that won the start race is the only one
        left to cancel. The target's own work is deliberately untouched.
        """
        scope = self._store.restrict_scope(
            scope_id,
            source,
            ScopeState.RECEIVE_ONLY if receive_only else ScopeState.REVOKED,
            expected_version=expected_version,
        )
        if self._cancel_turn is not None:
            for delivery in self._store.running_deliveries(scope.id):
                if delivery.recipient != source or delivery.turn_id is None:
                    continue
                try:
                    self._cancel_turn(delivery.recipient, delivery.turn_id)
                except Exception:  # noqa: BLE001 - the fence is already durable
                    continue
        self._notify(source)
        return scope

    def stop_assignment(
        self, request_id: str, source: SessionRef, *, expected_version: int,
        receive_only: bool = False,
    ) -> Request:
        """Fence one assignment, then cancel only its linked execution turns."""
        request = self._store.control_request(
            request_id, source, expected_version=expected_version, receive_only=receive_only,
        )
        for delivery in self._store.request_deliveries(request.id, source):
            if delivery.state.value != 'running' or delivery.turn_id is None:
                continue
            if receive_only and delivery.recipient != source:
                continue
            if self._cancel_turn is not None:
                self._cancel_turn(delivery.recipient, delivery.turn_id)
        self._notify(source)
        self._notify(request.target)
        return request
