"""The collaboration read/stop surface: wire decoding, service, and access."""

from __future__ import annotations

import asyncio

import pytest

from synapse.runtime.collaboration.coordinator import CollaborationCoordinator
from synapse.runtime.collaboration.model import DeliveryState, MessageKind, ScopeState
from synapse.runtime.collaboration.service_adapter import CollaborationServiceAdapter
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.service.access import (
    COLLABORATION_CONTROL,
    COLLABORATION_READ,
    DaemonAuthorizer,
    Principal,
    bind_access,
)
from synapse.runtime.service.collaboration import (
    ListCollaborationsQuery,
    StopCollaborationCommand,
)
from synapse.runtime.service.errors import ConflictError, InvalidRequestError
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import ProtocolError, decode_params

A = SessionRef('p1', 'a')
B = SessionRef('p1', 'b')

SESSION_WIRE = {'project_id': 'p1', 'thread_id': 'a'}


def _wire_session(session: SessionRef) -> dict[str, str]:
    return {'project_id': session.project_id, 'thread_id': session.thread_id}


def _harness(tmp_path):
    store = CollaborationStore(tmp_path / 'collab')
    coordinator = CollaborationCoordinator(store)
    adapter = CollaborationServiceAdapter(store, coordinator)
    service = LocalAgentRuntimeService(lambda _project: None, collaboration_provider=adapter)
    return store, coordinator, adapter, service


def test_decode_list_and_stop_params():
    query = decode_params('runtime.collaboration.list', {'session': SESSION_WIRE})
    assert isinstance(query, ListCollaborationsQuery)
    assert query.session == A
    assert query.limit > 0
    assert query.messages_per_request > 0

    command = decode_params(
        'runtime.collaboration.stop',
        {'session': SESSION_WIRE, 'scope_id': 'scope', 'expected_version': 2},
    )
    assert isinstance(command, StopCollaborationCommand)
    assert command.receive_only is False
    assert command.expected_version == 2
    assignment = decode_params('runtime.collaboration.stop', {
        'session': SESSION_WIRE, 'scope_id': 'scope', 'expected_version': 0,
        'assignment_id': 'assignment',
    })
    assert assignment.assignment_id == 'assignment'


@pytest.mark.parametrize(('method', 'params'), [
    ('runtime.collaboration.list', {}),
    ('runtime.collaboration.list', {'session': SESSION_WIRE, 'limit': 0}),
    ('runtime.collaboration.list', {'session': SESSION_WIRE, 'messages_per_request': 99}),
    ('runtime.collaboration.stop', {'session': SESSION_WIRE, 'scope_id': 'scope'}),
    (
        'runtime.collaboration.stop',
        {'session': SESSION_WIRE, 'scope_id': 'scope', 'expected_version': -1},
    ),
    (
        'runtime.collaboration.stop',
        {
            'session': SESSION_WIRE,
            'scope_id': 'scope',
            'expected_version': 0,
            'receive_only': 'yes',
        },
    ),
])
def test_decode_rejects_malformed_collaboration_params(method, params):
    with pytest.raises(ProtocolError):
        decode_params(method, params)


def test_list_projects_the_requests_and_the_generation(tmp_path):
    store, coordinator, _adapter, service = _harness(tmp_path)
    try:
        scope = coordinator.begin_work(A, 'turn-1')
        request = coordinator.request(
            scope.id, A, B, operation_key='k1', body='核对接口', anchor_turn_id='turn-1',
        )
        claimed = store.claim_next(B)
        store.transition_delivery(
            claimed.id, B, DeliveryState.RUNNING,
            expected_version=claimed.version, turn_id='turn-b',
        )
        coordinator.respond(
            request.id, B, kind=MessageKind.REPLY, body='结论',
            operation_key='k2', anchor_turn_id='turn-2',
        )
        page = asyncio.run(
            service.list_collaborations(ListCollaborationsQuery(session=A))
        )
        assert page.total == 1
        view = page.requests[0]
        assert view.request_id == request.id
        assert (view.source_project, view.source_thread) == ('p1', 'a')
        assert (view.target_project, view.target_thread) == ('p1', 'b')
        assert [message.kind for message in view.messages] == ['request', 'reply']
        assert view.message_total == 2
        assert page.scope_id == scope.id
        assert page.scope_state == ScopeState.ACTIVE.value
        assert page.scope_version == scope.version

        # The target sees the same request from its own side.
        other = asyncio.run(
            service.list_collaborations(ListCollaborationsQuery(session=B))
        )
        assert [item.request_id for item in other.requests] == [request.id]
    finally:
        store.close()


def test_assignment_stop_is_separate_from_other_assignments(tmp_path):
    store, coordinator, adapter, _ = _harness(tmp_path)
    try:
        scope = coordinator.begin_work(A, 'work')
        first = coordinator.request(scope.id, A, B, operation_key='one', body='one',
                                    anchor_turn_id='turn')
        second = coordinator.request(scope.id, A, B, operation_key='two', body='two',
                                     anchor_turn_id='turn')
        result = adapter.stop(StopCollaborationCommand(
            session=A, scope_id=scope.id, assignment_id=first.id, expected_version=0,
        ))
        assert result.state == 'revoked'
        assert store.get_request(second.id, A).continuation is ScopeState.ACTIVE
        assert store.get_scope(scope.id, A).state is ScopeState.ACTIVE
    finally:
        store.close()


def test_execution_and_result_processing_are_distinct(tmp_path):
    store, coordinator, _, _ = _harness(tmp_path)
    adapter = CollaborationServiceAdapter(
        store, coordinator, execution_status=lambda _ref, _turn: 'waiting_approval',
    )
    try:
        scope = coordinator.begin_work(A, 'work')
        request = coordinator.request(scope.id, A, B, operation_key='one', body='one',
                                      anchor_turn_id='turn')
        target = store.claim_next(B)
        run = store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                        expected_version=target.version, turn_id='target')
        waiting = adapter.list(ListCollaborationsQuery(session=A)).requests[0]
        assert waiting.execution_state == 'waiting_approval'
        assert waiting.assignment_id == request.id
        assert waiting.collaboration_id == scope.id
        coordinator.respond(request.id, B, kind=MessageKind.REPLY, body='result',
                            operation_key='reply', anchor_turn_id='target')
        store.transition_delivery(target.id, B, DeliveryState.SETTLED,
                                  expected_version=run.version, turn_id='target')
        assert (
            adapter.list(ListCollaborationsQuery(session=A)).requests[0].result_state == 'received'
        )
        source = store.claim_next(A)
        source_run = store.transition_delivery(source.id, A, DeliveryState.RUNNING,
                                               expected_version=source.version, turn_id='source')
        processing = adapter.list(ListCollaborationsQuery(session=A)).requests[0]
        assert processing.result_state == 'processing'
        store.transition_delivery(source.id, A, DeliveryState.SETTLED,
                                  expected_version=source_run.version, turn_id='source')
        assert (
            adapter.list(ListCollaborationsQuery(session=A)).requests[0].result_state == 'processed'
        )
    finally:
        store.close()


def test_list_never_shows_a_request_the_session_is_not_part_of(tmp_path):
    store, coordinator, _adapter, service = _harness(tmp_path)
    try:
        scope = coordinator.begin_work(A, 'turn-1')
        coordinator.request(
            scope.id, A, B, operation_key='k1', body='核对接口', anchor_turn_id='turn-1',
        )
        stranger = asyncio.run(
            service.list_collaborations(
                ListCollaborationsQuery(session=SessionRef('p2', 'c'))
            )
        )
        assert stranger.requests == ()
    finally:
        store.close()


def test_stop_fences_the_generation_and_reports_the_new_version(tmp_path):
    store, coordinator, _adapter, service = _harness(tmp_path)
    try:
        scope = coordinator.begin_work(A, 'turn-1')
        result = asyncio.run(
            service.stop_collaboration(
                StopCollaborationCommand(
                    session=A, scope_id=scope.id, expected_version=scope.version
                )
            )
        )
        assert result.state == ScopeState.REVOKED.value
        assert result.version == scope.version + 1

        # A stale version is a conflict, never a silent second stop.
        with pytest.raises(ConflictError):
            asyncio.run(
                service.stop_collaboration(
                    StopCollaborationCommand(
                        session=A, scope_id=scope.id, expected_version=scope.version
                    )
                )
            )
    finally:
        store.close()


def test_unavailable_collaboration_reports_an_invalid_request(tmp_path):
    store = CollaborationStore(tmp_path / 'collab')
    try:
        service = LocalAgentRuntimeService(lambda _project: None)
        with pytest.raises(InvalidRequestError):
            asyncio.run(
                service.list_collaborations(ListCollaborationsQuery(session=A))
            )
        with pytest.raises(InvalidRequestError):
            asyncio.run(
                service.stop_collaboration(
                    StopCollaborationCommand(
                        session=A, scope_id='scope', expected_version=0
                    )
                )
            )
    finally:
        store.close()


def test_access_layer_authorizes_and_delegates(tmp_path):
    store, coordinator, adapter, service = _harness(tmp_path)
    try:
        scope = coordinator.begin_work(A, 'turn-1')
        wrapper = bind_access(service, Principal('runtime-daemon'), DaemonAuthorizer())
        page = asyncio.run(
            wrapper.list_collaborations(ListCollaborationsQuery(session=A))
        )
        assert page.scope_id == scope.id
        result = asyncio.run(
            wrapper.stop_collaboration(
                StopCollaborationCommand(
                    session=A, scope_id=scope.id, expected_version=scope.version
                )
            )
        )
        assert result.state == ScopeState.REVOKED.value

        # A delegate without the surface reports it as unavailable rather than
        # failing the wrapper: the two capabilities are additive.
        bare = bind_access(
            LocalAgentRuntimeService(lambda _project: None),
            Principal('runtime-daemon'),
            DaemonAuthorizer(),
        )
        with pytest.raises(InvalidRequestError):
            asyncio.run(bare.list_collaborations(ListCollaborationsQuery(session=A)))
        with pytest.raises(InvalidRequestError):
            asyncio.run(
                bare.stop_collaboration(
                    StopCollaborationCommand(
                        session=A, scope_id='scope', expected_version=0
                    )
                )
            )
        assert COLLABORATION_READ != COLLABORATION_CONTROL
        assert adapter is not None
    finally:
        store.close()
