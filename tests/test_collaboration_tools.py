"""Agent-facing collaboration tools: trusted identity, role gating, idempotency."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from synapse.runtime.collaboration.coordinator import CollaborationCoordinator
from synapse.runtime.collaboration.model import (
    DeliveryState,
    MessageKind,
    RequestState,
    ScopeState,
)
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.collaboration.tools import build_collaboration_tools
from synapse.runtime.sessions.ref import SessionRef

A = SessionRef('p1', 'a')
B = SessionRef('p1', 'b')
OTHER = SessionRef('p2', 'c')


def _harness(tmp_path, *, allows=None):
    store = CollaborationStore(tmp_path / 'collab')
    coordinator = CollaborationCoordinator(store, allows=allows or (lambda _s, _t: True))
    contact, reply = build_collaboration_tools(coordinator, source_project='p1')
    return store, coordinator, contact, reply


def _turn(thread_id='a', tool_call_id='call-1', collaboration=None):
    configurable: dict = {'thread_id': thread_id}
    if collaboration is not None:
        configurable['collaboration'] = collaboration
    return SimpleNamespace(config={'configurable': configurable}, tool_call_id=tool_call_id)


def _delivery_metadata(role, request_id='r', delivery_id='d', anchor='turn-1'):
    return {
        'role': role,
        'request_id': request_id,
        'delivery_id': delivery_id,
        'anchor_turn_id': anchor,
    }


def test_contact_session_uses_the_trusted_source_and_is_idempotent(tmp_path):
    store, _coordinator, contact, _reply = _harness(tmp_path)
    try:
        runtime = _turn()
        first = contact.func(runtime, target='p1:b', request='核对接口', expect='结论')
        assert first.startswith('accepted')
        requests = store.deliveries(B, limit=10)
        assert len(requests) == 1
        request = store.get_request(requests[0].request_id, A)
        # Source identity comes from the daemon and the running turn, never from
        # the model's arguments.
        assert request.source == A
        assert request.target == B
        assert request.body == '核对接口\n\n期望交付：结论'
        assert requests[0].state is DeliveryState.PENDING

        # A retry of the same tool call must not create a second request.
        again = contact.func(runtime, target='p1:b', request='核对接口', expect='结论')
        assert again.startswith('accepted')
        assert len(store.deliveries(B, limit=10)) == 1
        assert len(store.messages(request.id, A, limit=10)) == 1
    finally:
        store.close()


def test_target_reports_progress_without_starting_the_commander(tmp_path):
    store, _, contact, reply = _harness(tmp_path)
    try:
        contact.func(_turn(), target='p1:b', request='核对接口')
        delivery = store.claim_next(B)
        store.transition_delivery(delivery.id, B, DeliveryState.RUNNING,
                                  expected_version=delivery.version, turn_id='b')
        runtime = _turn('b', 'progress', _delivery_metadata('target', delivery.request_id))
        assert reply.func(runtime, message='正在核对兼容性', kind='progress').startswith('recorded')
        assert store.claim_next(A) is None
        assert store.messages(delivery.request_id, A)[-1].kind is MessageKind.PROGRESS
        assert contact.func(runtime, target='p2:c', request='转派').startswith('failed')
        assert store.deliveries(OTHER) == ()
    finally:
        store.close()


def test_commander_can_group_multiple_assignments_by_explicit_identity(tmp_path):
    store, coordinator, contact, _ = _harness(tmp_path)
    try:
        group = coordinator.begin_work(A, 'shared-work')
        contact.func(_turn(tool_call_id='one'), target='p1:b', request='检查接口',
                     collaboration_id=group.id)
        coordinator.begin_work(A, 'other-work')
        contact.func(_turn(tool_call_id='two'), target='p2:c', request='检查客户端',
                     collaboration_id=group.id)
        assert store.deliveries(B)[0].scope_id == store.deliveries(OTHER)[0].scope_id == group.id
    finally:
        store.close()


def test_contact_session_accepts_the_composer_pill_token(tmp_path):
    store, _coordinator, contact, _reply = _harness(tmp_path)
    try:
        # The console's pill token and a bare ref name the same target.
        result = contact.func(_turn(), target='@session:p1:b', request='核对接口')
        assert result.startswith('accepted')
        assert len(store.deliveries(B, limit=10)) == 1
    finally:
        store.close()


def test_contact_session_rejects_self_malformed_and_empty_targets(tmp_path):
    store, _coordinator, contact, _reply = _harness(tmp_path)
    try:
        runtime = _turn()
        assert contact.func(runtime, target='p1:a', request='x').startswith('failed')
        assert contact.func(runtime, target='no-colon', request='x').startswith('failed')
        assert contact.func(runtime, target='p1:b', request='   ').startswith('failed')
        assert store.deliveries(B, limit=10) == ()
    finally:
        store.close()


def test_contact_session_honours_the_deployment_policy(tmp_path):
    store, _coordinator, contact, _reply = _harness(
        tmp_path, allows=lambda source, target: source.project_id == target.project_id
    )
    try:
        runtime = _turn()
        allowed = contact.func(runtime, target='p1:b', request='同项目')
        assert allowed.startswith('accepted')
        denied = contact.func(runtime, target='p2:c', request='跨项目')
        assert denied.startswith('failed: 当前部署不允许跨项目协作')
        assert store.deliveries(OTHER, limit=10) == ()
    finally:
        store.close()


def test_contact_session_names_an_unreachable_target(tmp_path):
    store = CollaborationStore(tmp_path / 'collab')
    coordinator = CollaborationCoordinator(
        store, reachable=lambda _target: 'project-unavailable'
    )
    contact, _reply = build_collaboration_tools(coordinator, source_project='p1')
    try:
        # "you may not" and "it is not there" are different answers on purpose.
        result = contact.func(_turn(), target='p2:c', request='跨项目')
        assert result.startswith('failed: 目标会话现在无法访问（project-unavailable）')
        assert store.deliveries(OTHER, limit=10) == ()
    finally:
        store.close()


def test_contact_session_without_a_session_identity_fails_closed(tmp_path):
    store, _coordinator, contact, _reply = _harness(tmp_path)
    try:
        runtime = SimpleNamespace(config={'configurable': {}}, tool_call_id='call-1')
        assert contact.func(runtime, target='p1:b', request='x').startswith('failed')
        assert store.deliveries(B, limit=10) == ()
    finally:
        store.close()


def test_reply_tool_requires_a_collaboration_turn(tmp_path):
    store, _coordinator, _contact, reply = _harness(tmp_path)
    try:
        plain = reply.func(_turn(), message='结论', kind='reply')
        assert plain.startswith('failed: this turn is not handling')
    finally:
        store.close()


def test_reply_tool_gates_the_role_and_follows_the_request_state(tmp_path):
    store, coordinator, contact, reply = _harness(tmp_path)
    try:
        assert contact.func(_turn(), target='p1:b', request='核对接口').startswith('accepted')
        target_delivery = store.deliveries(B, limit=10)[0]
        request_id = target_delivery.request_id

        target = _turn('b', 'call-target', _delivery_metadata('target', request_id, 'd1'))
        source = _turn('a', 'call-source', _delivery_metadata('source', request_id, 'd2'))
        assert reply.func(target, message='问', kind='supplement').startswith(
            'failed: role'
        )
        assert reply.func(source, message='答', kind='reply').startswith('failed: role')

        # The target's turn must be running before it may ask or answer.
        claimed = store.claim_next(B)
        store.transition_delivery(
            claimed.id, B, DeliveryState.RUNNING,
            expected_version=claimed.version, turn_id='turn-b',
        )
        assert reply.func(target, message='覆盖哪些客户端？', kind='clarification').startswith(
            'recorded'
        )
        assert store.get_request(request_id, A).state is RequestState.WAITING_INPUT

        assert reply.func(source, message='全部旧客户端', kind='supplement').startswith(
            'recorded'
        )
        assert store.get_request(request_id, A).state is RequestState.PENDING
        # A final reply cannot jump the queued supplement: the target answers
        # what it was asked before it declares the request done.
        assert reply.func(target, message='结论', kind='reply').startswith('failed')

        store.transition_delivery(
            claimed.id, B, DeliveryState.SETTLED,
            expected_version=claimed.version + 1, turn_id='turn-b',
        )
        pending = store.claim_next(B)
        store.transition_delivery(
            pending.id, B, DeliveryState.RUNNING,
            expected_version=pending.version, turn_id='turn-b2',
        )
        second_target = _turn('b', 'call-target-2', _delivery_metadata('target', request_id, 'd3'))
        assert reply.func(second_target, message='结论', kind='reply').startswith('recorded')
        assert store.get_request(request_id, A).state is RequestState.REPLIED
        assert coordinator is not None
    finally:
        store.close()


def test_reply_tool_is_idempotent_for_one_call(tmp_path):
    store, _coordinator, contact, reply = _harness(tmp_path)
    try:
        assert contact.func(_turn(), target='p1:b', request='核对接口').startswith('accepted')
        claimed = store.claim_next(B)
        store.transition_delivery(
            claimed.id, B, DeliveryState.RUNNING,
            expected_version=claimed.version, turn_id='turn-b',
        )
        target = _turn('b', 'call-target', _delivery_metadata('target', claimed.request_id))
        assert reply.func(target, message='结论', kind='reply').startswith('recorded')
        assert reply.func(target, message='结论', kind='reply').startswith('recorded')
        messages = store.messages(claimed.request_id, A, limit=10)
        assert [m.kind for m in messages] == [MessageKind.REQUEST, MessageKind.REPLY]
    finally:
        store.close()


def test_work_generation_rotates_only_for_a_new_user_turn(tmp_path):
    store, coordinator, _contact, _reply = _harness(tmp_path)
    try:
        first = coordinator.begin_work(A, 'turn-1')
        assert coordinator.current_work(A).id == first.id
        second = coordinator.begin_work(A, 'turn-2')
        assert second.id != first.id
        assert coordinator.current_work(A).id == second.id
        # A new user turn must not silently disable the previous assignment.
        assert store.get_scope(first.id, A).state is ScopeState.ACTIVE
        assert store.get_scope(second.id, A).state is ScopeState.ACTIVE
    finally:
        store.close()


@pytest.mark.parametrize('field', ['request_id', 'delivery_id'])
def test_reply_tool_fails_closed_on_incomplete_metadata(tmp_path, field):
    store, _coordinator, _contact, reply = _harness(tmp_path)
    try:
        metadata = _delivery_metadata('target')
        metadata[field] = ''
        runtime = _turn('b', 'call-1', metadata)
        assert reply.func(runtime, message='结论', kind='reply').startswith('failed')
    finally:
        store.close()


def test_contact_session_blocks_during_clarification_turn_to_same_target(tmp_path):
    store, _coordinator, contact, _reply = _harness(tmp_path)
    try:
        metadata = {
            'role': 'source',
            'kind': 'clarification',
            'sender_project': 'p1',
            'sender_thread': 'b',
            'request_id': 'req-1',
            'delivery_id': 'del-1',
        }
        runtime = _turn('a', 'call-1', metadata)
        res = contact.func(runtime, target='p1:b', request='补充上下文参数')
        assert res.startswith('failed: 当前轮次正在处理目标会话提出的澄清提问')
        assert "reply_to_collaboration(kind='supplement'" in res
        assert store.deliveries(B, limit=10) == ()
    finally:
        store.close()


def test_contact_session_allows_different_target_during_clarification_turn(tmp_path):
    store, _coordinator, contact, _reply = _harness(tmp_path)
    try:
        metadata = {
            'role': 'source',
            'kind': 'clarification',
            'sender_project': 'p1',
            'sender_thread': 'b',
            'request_id': 'req-1',
            'delivery_id': 'del-1',
        }
        runtime = _turn('a', 'call-1', metadata)
        res = contact.func(runtime, target='p2:c', request='请第三方协查')
        assert res.startswith('accepted')
        assert len(store.deliveries(OTHER, limit=10)) == 1
    finally:
        store.close()
