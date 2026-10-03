"""Bridge the collaboration domain to the neutral service DTOs.

The service layer stays free of the domain (and of SQLite): the daemon injects
this adapter, which reads through the store and maps domain failures onto the
service's own error types. Reads and the stop control only -- starting a request
is an Agent action and deliberately has no wire method, so a client can never
make one session contact another.
"""

from __future__ import annotations

from collections.abc import Callable

from synapse.runtime.collaboration.coordinator import CollaborationCoordinator
from synapse.runtime.collaboration.model import (
    CollaborationError,
    ConflictError,
    DeliveryState,
    MessageKind,
    Request,
    RequestState,
    ScopeState,
)
from synapse.runtime.collaboration.model import NotFoundError as DomainNotFoundError
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.service.collaboration import (
    CollaborationMessageView,
    CollaborationPage,
    CollaborationRequestView,
    ListCollaborationsQuery,
    StopCollaborationCommand,
    StopCollaborationResult,
)
from synapse.runtime.service.errors import (
    ConflictError as ServiceConflictError,
)
from synapse.runtime.service.errors import (
    InvalidRequestError,
)
from synapse.runtime.service.errors import (
    NotFoundError as ServiceNotFoundError,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = ['CollaborationServiceAdapter']


class CollaborationServiceAdapter:
    """Duck-typed provider: ``list(query)`` and ``stop(command)``."""

    def __init__(
        self, store: CollaborationStore, coordinator: CollaborationCoordinator,
        *, execution_status: Callable[[SessionRef, str], str | None] | None = None,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._execution_status = execution_status

    def list(self, query: ListCollaborationsQuery) -> CollaborationPage:
        try:
            requests = self._store.requests_for_session(query.session, limit=query.limit)
            views = tuple(
                self._view(request, query) for request in requests
            )
            scope = self._store.active_scope(query.session)
        except DomainNotFoundError:
            raise ServiceNotFoundError('session collaboration not found') from None
        except CollaborationError:
            raise InvalidRequestError('collaboration request is invalid') from None
        return CollaborationPage(
            requests=views,
            total=len(views),
            scope_id=None if scope is None else scope.id,
            scope_state=None if scope is None else scope.state.value,
            scope_version=None if scope is None else scope.version,
        )

    def _view(
        self, request: Request, query: ListCollaborationsQuery,
    ) -> CollaborationRequestView:
        messages = (
            self._store.recent_messages(
                request.id, query.session, limit=query.messages_per_request
            )
            if query.messages_per_request
            else ()
        )
        message_total, unresolved = self._store.request_counts(request.id)
        group = self._store.get_scope(request.scope_id, request.source)
        continuation = request.continuation
        if group.state is ScopeState.REVOKED:
            continuation = ScopeState.REVOKED
        elif group.state is ScopeState.RECEIVE_ONLY and continuation is ScopeState.ACTIVE:
            continuation = ScopeState.RECEIVE_ONLY
        deliveries = self._store.request_deliveries(request.id, query.session)
        target_runs = [d for d in deliveries if d.recipient == request.target]
        execution = target_runs[-1] if target_runs else None
        execution_state = 'pending' if execution is None else execution.state.value
        if execution is not None and execution.reason == 'waiting_approval':
            execution_state = 'waiting_approval'
        if (execution is not None and execution.state is DeliveryState.RUNNING
                and execution.turn_id and self._execution_status is not None):
            execution_state = self._execution_status(
                request.target, execution.turn_id,
            ) or execution_state
        result_state = 'not_received'
        if request.state is RequestState.REPLIED:
            reply = next((m for m in self._store.recent_messages(
                request.id, query.session, limit=1,
            ) if m.kind is MessageKind.REPLY), None)
            result = next((d for d in deliveries if reply and d.message_id == reply.id), None)
            result_state = 'received'
            if result is not None:
                if result.state is DeliveryState.RUNNING:
                    result_state = 'processing'
                elif result.state is DeliveryState.SETTLED:
                    result_state = ('waiting_approval' if result.reason == 'waiting_approval'
                                    else 'interrupted' if result.reason else 'processed')
                elif result.state is DeliveryState.REVIEW:
                    result_state = 'review'
        return CollaborationRequestView(
            request_id=request.id,
            scope_id=request.scope_id,
            source_project=request.source.project_id,
            source_thread=request.source.thread_id,
            target_project=request.target.project_id,
            target_thread=request.target.thread_id,
            state=request.state.value,
            version=request.version,
            created_at_ms=request.created_at_ms,
            updated_at_ms=request.updated_at_ms,
            messages=tuple(
                CollaborationMessageView(
                    message_id=message.id,
                    request_id=message.request_id,
                    sequence=message.sequence,
                    sender_project=message.sender.project_id,
                    sender_thread=message.sender.thread_id,
                    kind=message.kind.value,
                    body=message.body,
                    created_at_ms=message.created_at_ms,
                )
                for message in messages
            ),
            message_total=message_total,
            pending=unresolved,
            collaboration_id=request.scope_id,
            assignment_id=request.id,
            continuation_state=continuation.value,
            control_version=request.control_version,
            execution_state=execution_state,
            execution_turn_id=None if execution is None else execution.turn_id,
            execution_reason=None if execution is None else execution.reason,
            result_state=result_state,
        )

    def stop(self, command: StopCollaborationCommand) -> StopCollaborationResult:
        try:
            if command.assignment_id is not None:
                request = self._store.get_request(command.assignment_id, command.session)
                if request.scope_id != command.scope_id:
                    raise ConflictError('assignment does not belong to collaboration')
                request = self._coordinator.stop_assignment(
                    request.id, command.session, expected_version=command.expected_version,
                    receive_only=command.receive_only,
                )
                return StopCollaborationResult(
                    command_id=command.command_id, session=command.session,
                    scope_id=request.scope_id, state=request.continuation.value,
                    version=request.control_version,
                )
            scope = self._coordinator.stop(
                command.scope_id,
                command.session,
                expected_version=command.expected_version,
                receive_only=command.receive_only,
            )
        except DomainNotFoundError:
            raise ServiceNotFoundError('session collaboration not found') from None
        except ConflictError:
            raise ServiceConflictError('collaboration generation changed') from None
        except CollaborationError:
            raise InvalidRequestError('stop collaboration request is invalid') from None
        return StopCollaborationResult(
            command_id=command.command_id,
            session=command.session,
            scope_id=scope.id,
            state=scope.state.value,
            version=scope.version,
        )
