"""SQLite collaboration facts, recovery and concurrency without a runtime."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from synapse.runtime.collaboration.model import (
    MAX_MESSAGES,
    CapacityError,
    CollaborationError,
    ConflictError,
    DeliveryState,
    MessageKind,
    NotFoundError,
    RequestState,
    SchemaVersionError,
    ScopeState,
)
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.sessions.ref import SessionRef

A = SessionRef('p', 'a')
B = SessionRef('p', 'b')
C = SessionRef('other', 'c')


def make_request(store, source=A, target=B, work='w', key='request'):
    scope = store.begin_scope(source, work)
    request = store.create_request(
        scope.id, source, target, operation_key=key, body='help', anchor_turn_id='turn',
    )
    return scope, request


def post(store, request, actor, kind, key):
    return store.post_message(
        request.id, actor, kind=kind, body=key, operation_key=key, anchor_turn_id='turn',
    )


def test_idempotency_conflicts_and_stopped_retry(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        scope, request = make_request(store)
        assert make_request(store) == (scope, request)
        with pytest.raises(ConflictError):
            store.create_request(scope.id, A, B, operation_key='request', body='different',
                                 anchor_turn_id='turn')
        store.restrict_scope(scope.id, A, ScopeState.REVOKED, expected_version=0)
        retried_scope, retried_request = make_request(store)
        assert retried_scope.state is ScopeState.REVOKED
        assert retried_scope.id == scope.id
        assert retried_request.id == request.id
        assert store.get_request(request.id, A).state is RequestState.CANCELLED
        with pytest.raises(ConflictError):
            store.create_request(scope.id, A, B, operation_key='new', body='help',
                                 anchor_turn_id='turn')
        assert len(store.messages(request.id, A)) == 1
        assert len(store.deliveries(B)) == 1
        assert store.deliveries(B)[0].state is DeliveryState.CANCELLED


def test_generations_and_participant_checks(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        old, request = make_request(store)
        new = store.begin_scope(A, 'next')
        assert new.state is ScopeState.ACTIVE
        # Independent user work does not revoke an existing collaboration.
        assert store.get_scope(old.id, A).state is ScopeState.ACTIVE
        store.create_request(old.id, A, B, operation_key='late', body='help',
                             anchor_turn_id='turn')
        assert store.get_request(request.id, B).state is RequestState.PENDING
        assert store.deliveries(B)[0].state is DeliveryState.PENDING
        with pytest.raises(NotFoundError):
            store.get_request(request.id, C)
        with pytest.raises(NotFoundError):
            store.messages(request.id, C)
        with pytest.raises(NotFoundError):
            store.get_scope(old.id, B)
        with pytest.raises(NotFoundError):
            store.create_request(new.id, B, A, operation_key='x', body='help',
                                 anchor_turn_id='turn')
        with pytest.raises(ConflictError):
            post(store, request, A, MessageKind.REPLY, 'bad')
        assert store.deliveries(C) == ()


def test_atomic_creation_and_response_on_sql_failure(tmp_path):
    path = tmp_path / 'db'
    with CollaborationStore(path) as store:
        scope = store.begin_scope(A, 'w')
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER reject_delivery BEFORE INSERT ON deliveries "
                       "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_request(scope.id, A, B, operation_key='x', body='help',
                                 anchor_turn_id='turn')
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
            db.execute('DROP TRIGGER reject_delivery')
        _, request = make_request(store)
        delivery = store.claim_next(B)
        store.transition_delivery(delivery.id, B, DeliveryState.RUNNING,
                                  expected_version=delivery.version, turn_id='t')
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER reject_delivery BEFORE INSERT ON deliveries "
                       "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            post(store, request, B, MessageKind.REPLY, 'answer')
        assert store.get_request(request.id, A).state is RequestState.WORKING
        assert len(store.messages(request.id, A)) == 1
        with sqlite3.connect(path) as db:
            db.execute('DROP TRIGGER reject_delivery')
        reply = post(store, request, B, MessageKind.REPLY, 'answer')
        assert post(store, request, B, MessageKind.REPLY, 'answer') == reply
        with pytest.raises(ConflictError):
            store.post_message(request.id, B, kind=MessageKind.REPLY, body='changed',
                               operation_key='answer', anchor_turn_id='turn')
        assert len(store.deliveries(A)) == 1


def test_capacity_rolls_back_and_final_slot_reserved(tmp_path):
    with CollaborationStore(tmp_path / 'db', max_pending=1) as store:
        scope, request = make_request(store)
        with pytest.raises(CapacityError):
            store.create_request(scope.id, A, B, operation_key='second', body='help',
                                 anchor_turn_id='turn')
        assert len(store.deliveries(B)) == 1
        claimed = store.claim_next(B)
        store.transition_delivery(claimed.id, B, DeliveryState.RUNNING,
                                  expected_version=claimed.version, turn_id='t')
        with pytest.raises(CapacityError):
            make_request(store, work='w', key='second')
        store.transition_delivery(claimed.id, B, DeliveryState.SETTLED,
                                  expected_version=claimed.version + 1, turn_id='t')
        assert store.get_request(request.id, A).state is RequestState.WORKING
        for n in range((MAX_MESSAGES - 2) // 2):
            post(store, request, B, MessageKind.CLARIFICATION, f'question{n}')
            # Clear each outbound intent to leave bounded recipient capacity.
            incoming = store.claim_next(A)
            store.transition_delivery(incoming.id, A, DeliveryState.RUNNING,
                                      expected_version=incoming.version, turn_id=f'a{n}')
            store.transition_delivery(incoming.id, A, DeliveryState.SETTLED,
                                      expected_version=incoming.version + 1, turn_id=f'a{n}')
            post(store, request, A, MessageKind.SUPPLEMENT, f'supplement{n}')
            incoming = store.claim_next(B)
            store.transition_delivery(incoming.id, B, DeliveryState.RUNNING,
                                      expected_version=incoming.version, turn_id=f'b{n}')
            store.transition_delivery(incoming.id, B, DeliveryState.SETTLED,
                                      expected_version=incoming.version + 1, turn_id=f'b{n}')
        # 1 + 2*31 = 63; no non-final message can consume the reserved slot.
        with pytest.raises(CapacityError):
            post(store, request, B, MessageKind.CLARIFICATION, 'extra')
        assert len(store.messages(request.id, A, limit=100)) == MAX_MESSAGES - 1
        post(store, request, B, MessageKind.REPLY, 'final')
        assert len(store.messages(request.id, A, limit=100)) == MAX_MESSAGES


def test_claim_fifo_two_connections_and_independent_recipient(tmp_path):
    path = tmp_path / 'db'
    with CollaborationStore(path) as left, CollaborationStore(path) as right:
        _, first = make_request(left)
        _, second = make_request(left, source=C, work='c')
        make_request(left, source=B, target=C, work='b')
        gate = Barrier(2)

        def claim(store):
            gate.wait(timeout=5)
            return store.claim_next(B)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(claim, left)
            f2 = pool.submit(claim, right)
            results = (f1.result(), f2.result())
        assert sum(item is not None for item in results) == 1
        owner = next(item for item in results if item is not None)
        assert owner.request_id == first.id
        assert left.claim_next(B) is None
        assert left.claim_next(C) is not None
        assert second.id != first.id


def test_version_turn_and_explicit_failure(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        scope, request = make_request(store)
        for bad in (True, -1, 0.0):
            with pytest.raises(CollaborationError):
                store.restrict_scope(scope.id, A, ScopeState.REVOKED, expected_version=bad)
        delivery = store.claim_next(B)
        for bad in (True, -1, 1.0):
            with pytest.raises(CollaborationError):
                store.transition_delivery(delivery.id, B, DeliveryState.RUNNING,
                                          expected_version=bad, turn_id='t')
        with pytest.raises(CollaborationError):
            store.transition_delivery(delivery.id, B, DeliveryState.RUNNING,
                                      expected_version=delivery.version)
        running = store.transition_delivery(delivery.id, B, DeliveryState.RUNNING,
                                            expected_version=delivery.version, turn_id='t')
        with pytest.raises(ConflictError):
            store.transition_delivery(delivery.id, B, DeliveryState.SETTLED,
                                      expected_version=delivery.version)
        with pytest.raises(ConflictError):
            store.transition_delivery(delivery.id, B, DeliveryState.SETTLED,
                                      expected_version=running.version, turn_id='other')
        store.transition_delivery(delivery.id, B, DeliveryState.SETTLED,
                                  expected_version=running.version, turn_id='t')
        assert store.get_request(request.id, A).state is RequestState.WORKING
        with pytest.raises(ConflictError):
            store.fail_request(request.id, A, expected_version=1)
        assert store.fail_request(request.id, B, expected_version=1).state is RequestState.FAILED


def test_clarification_supplement_reply_and_stop_orders(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        scope, request = make_request(store)
        claimed = store.claim_next(B)
        store.transition_delivery(claimed.id, B, DeliveryState.RUNNING,
                                  expected_version=claimed.version, turn_id='b')
        post(store, request, B, MessageKind.CLARIFICATION, 'q')
        post(store, request, A, MessageKind.SUPPLEMENT, 's')
        assert store.get_request(request.id, B).state is RequestState.PENDING
        target = store.deliveries(B)[-1]
        assert target.state is DeliveryState.PENDING
        store.transition_delivery(claimed.id, B, DeliveryState.SETTLED,
                                  expected_version=claimed.version + 1, turn_id='b')
        target = store.claim_next(B)
        store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                  expected_version=target.version, turn_id='b2')
        post(store, request, B, MessageKind.REPLY, 'r')
        assert store.get_request(request.id, A).state is RequestState.REPLIED
        store.restrict_scope(scope.id, A, ScopeState.REVOKED, expected_version=0)
        assert store.deliveries(A)[-1].state is DeliveryState.CANCELLED
        store.transition_delivery(target.id, B, DeliveryState.SETTLED,
                                  expected_version=target.version + 1, turn_id='b2')
        # Stop wins first: no target execution; a claimed target remains uncertain.
        late_scope, late = make_request(store, work='late')
        target = store.claim_next(B)
        store.restrict_scope(late_scope.id, A, ScopeState.REVOKED, expected_version=0)
        assert store.deliveries(B)[-1].state is DeliveryState.REVIEW
        with pytest.raises(ConflictError):
            store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                      expected_version=target.version + 1, turn_id='late')
        # A target already running when stop commits may still return a reply.
        # Review holds B's mailbox; use a separate target for the running case.
        other_scope, other = make_request(store, source=C, target=A, work='other')
        # A has a pending clarification receipt, so drain or cancel it first.
        started = store.claim_next(A)
        assert started is not None
        if started.request_id != other.id:
            store.transition_delivery(started.id, A, DeliveryState.RUNNING,
                                      expected_version=started.version, turn_id='prior')
            store.transition_delivery(started.id, A, DeliveryState.SETTLED,
                                      expected_version=started.version + 1, turn_id='prior')
            started = store.claim_next(A)
        store.transition_delivery(started.id, A, DeliveryState.RUNNING,
                                  expected_version=started.version, turn_id='other')
        store.restrict_scope(other_scope.id, C, ScopeState.REVOKED, expected_version=0)
        post(store, other, A, MessageKind.REPLY, 'late')
        assert store.get_request(other.id, C).state is RequestState.REPLIED
        assert store.deliveries(C)[-1].state is DeliveryState.CANCELLED


def test_reopen_recovery_and_pages(tmp_path):
    path = tmp_path / 'db'
    with CollaborationStore(path) as store:
        _, first = make_request(store)
        make_request(store, source=C, work='other')
        claimed = store.claim_next(B)
    with CollaborationStore(path) as store:
        assert store.claim_next(B) is None
        assert store.recover_uncertain() == 1
        assert store.recover_uncertain() == 0
        assert store.claim_next(B) is None  # review is an exclusive owner, not a retry
        assert store.get_request(first.id, A).state is RequestState.PENDING
        assert len(store.messages(first.id, B, limit=1)) == 1
        assert store.messages(first.id, B, after=1) == ()
        assert store.deliveries(B, after=claimed.sequence, limit=1)[0].sequence > claimed.sequence
        for bad in (-1, True, 1.0, MAX_MESSAGES + 1):
            with pytest.raises(CollaborationError):
                store.messages(first.id, A, after=bad)
        for bad in (-1, True, 1.0, 2**63):
            with pytest.raises(CollaborationError):
                store.deliveries(B, after=bad)
        with pytest.raises(CollaborationError):
            store.deliveries(B, limit=101)
    store.close()
    with pytest.raises(CollaborationError):
        store.claim_next(B)


def test_pending_reopen_and_stop_before_target_claim(tmp_path):
    path = tmp_path / 'db'
    with CollaborationStore(path) as store:
        old_scope, old_request = make_request(store)
        store.restrict_scope(old_scope.id, A, ScopeState.REVOKED, expected_version=0)
        new_scope, new_request = make_request(store, work='new')
    with CollaborationStore(path) as store:
        assert store.recover_uncertain() == 0
        assert store.deliveries(B)[0].state is DeliveryState.CANCELLED
        assert store.get_request(old_request.id, A).state is RequestState.CANCELLED
        assert store.get_scope(new_scope.id, A).state is ScopeState.ACTIVE
        assert store.claim_next(B).request_id == new_request.id


def test_final_reply_delivery_runs_after_target_settles(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        _, request = make_request(store)
        target = store.claim_next(B)
        running = store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                            expected_version=target.version, turn_id='b')
        post(store, request, B, MessageKind.REPLY, 'final')
        store.transition_delivery(target.id, B, DeliveryState.SETTLED,
                                  expected_version=running.version, turn_id='b')
        source = store.claim_next(A)
        assert source.request_id == request.id
        running = store.transition_delivery(source.id, A, DeliveryState.RUNNING,
                                            expected_version=source.version, turn_id='a')
        store.transition_delivery(source.id, A, DeliveryState.SETTLED,
                                  expected_version=running.version, turn_id='a')
        assert store.get_request(request.id, A).state is RequestState.REPLIED
        assert store.deliveries(A)[0].state is DeliveryState.SETTLED


def test_stale_work_retry_does_not_revoke_new_active_scope(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        old = store.begin_scope(A, 'old')
        new = store.begin_scope(A, 'new')
        assert store.begin_scope(A, 'old').id == old.id
        # Retrying a group neither revokes other groups nor makes it the newest.
        assert store.get_scope(old.id, A).state is ScopeState.ACTIVE
        assert store.get_scope(new.id, A).state is ScopeState.ACTIVE
        assert store.create_request(new.id, A, B, operation_key='new', body='work',
                                    anchor_turn_id='turn').state is RequestState.PENDING


@pytest.mark.parametrize('stop_first', [True, False])
def test_stop_races_final_reply_across_connections(tmp_path, stop_first):
    path = tmp_path / 'db'
    with CollaborationStore(path) as left, CollaborationStore(path) as right:
        scope, request = make_request(left)
        claimed = left.claim_next(B)
        left.transition_delivery(claimed.id, B, DeliveryState.RUNNING,
                                 expected_version=claimed.version, turn_id='b')
        gate = Barrier(2)
        from threading import Event

        first_done = Event()

        def stop():
            gate.wait(timeout=5)
            if not stop_first:
                assert first_done.wait(timeout=5)
            try:
                return left.restrict_scope(scope.id, A, ScopeState.REVOKED,
                                           expected_version=scope.version)
            finally:
                if stop_first:
                    first_done.set()

        def reply():
            gate.wait(timeout=5)
            if stop_first:
                assert first_done.wait(timeout=5)
            try:
                return post(right, request, B, MessageKind.REPLY, 'final')
            finally:
                if not stop_first:
                    first_done.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            stopped = pool.submit(stop)
            replied = pool.submit(reply)
            stopped.result(timeout=10)
            replied.result(timeout=10)
        assert [m.kind for m in left.messages(request.id, A)] == [
            MessageKind.REQUEST, MessageKind.REPLY,
        ]
        assert left.get_request(request.id, A).state is RequestState.REPLIED
        assert left.deliveries(A)[0].state is DeliveryState.CANCELLED
        assert left.claim_next(A) is None
        reply = post(right, request, B, MessageKind.REPLY, 'final')
        assert reply == left.messages(request.id, A)[1]


def test_receive_only_allows_target_reply_but_not_source_start(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        scope, request = make_request(store)
        store.restrict_scope(scope.id, A, ScopeState.RECEIVE_ONLY, expected_version=0)
        target = store.claim_next(B)
        running = store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                            expected_version=target.version, turn_id='b')
        post(store, request, B, MessageKind.REPLY, 'final')
        store.transition_delivery(target.id, B, DeliveryState.SETTLED,
                                  expected_version=running.version, turn_id='b')
        assert store.get_request(request.id, A).state is RequestState.REPLIED
        assert store.deliveries(A)[0].state is DeliveryState.CANCELLED
        assert store.claim_next(A) is None


def test_failed_request_retires_pending_clarification_and_unblocks_source(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        _, request = make_request(store)
        target = store.claim_next(B)
        store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                  expected_version=target.version, turn_id='b')
        post(store, request, B, MessageKind.CLARIFICATION, 'q')
        waiting = store.get_request(request.id, B)
        store.fail_request(request.id, B, expected_version=waiting.version)
        assert store.deliveries(A)[0].state is DeliveryState.CANCELLED
        assert store.claim_next(A) is None
        _, other = make_request(store, source=C, target=A, work='other')
        assert store.claim_next(A).request_id == other.id


@pytest.mark.parametrize('answer', [MessageKind.SUPPLEMENT, MessageKind.REPLY])
def test_superseded_pending_clarification_does_not_block_reply(tmp_path, answer):
    with CollaborationStore(tmp_path / 'db') as store:
        _, request = make_request(store)
        target = store.claim_next(B)
        store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                  expected_version=target.version, turn_id='b')
        post(store, request, B, MessageKind.CLARIFICATION, 'q')
        post(store, request, A if answer is MessageKind.SUPPLEMENT else B, answer, 'answer')
        assert store.deliveries(A)[0].state is DeliveryState.CANCELLED
        if answer is MessageKind.SUPPLEMENT:
            assert store.claim_next(A) is None
            # The queued supplement owns the target's next turn, so the target
            # settles its first turn and processes it before answering.
            store.transition_delivery(target.id, B, DeliveryState.SETTLED,
                                      expected_version=target.version + 1, turn_id='b')
            pending = store.claim_next(B)
            assert pending.message_id == store.messages(request.id, B)[-1].id
            running = store.transition_delivery(pending.id, B, DeliveryState.RUNNING,
                                                expected_version=pending.version, turn_id='b2')
            assert store.get_request(request.id, B).state is RequestState.WORKING
            store.transition_delivery(pending.id, B, DeliveryState.SETTLED,
                                      expected_version=running.version, turn_id='b2')
            post(store, request, B, MessageKind.REPLY, 'final')
        source = store.claim_next(A)
        assert source.message_id == store.messages(request.id, A)[-1].id
        running = store.transition_delivery(source.id, A, DeliveryState.RUNNING,
                                            expected_version=source.version, turn_id='a')
        store.transition_delivery(source.id, A, DeliveryState.SETTLED,
                                  expected_version=running.version, turn_id='a')
        assert store.get_request(request.id, A).state is RequestState.REPLIED


def test_claimed_stale_clarification_keeps_review_ownership(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        _, request = make_request(store)
        target = store.claim_next(B)
        store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                  expected_version=target.version, turn_id='b')
        post(store, request, B, MessageKind.CLARIFICATION, 'q')
        stale = store.claim_next(A)
        post(store, request, B, MessageKind.REPLY, 'final')
        assert store.deliveries(A)[0].state is DeliveryState.CLAIMED
        with pytest.raises(ConflictError):
            store.transition_delivery(stale.id, A, DeliveryState.RUNNING,
                                      expected_version=stale.version, turn_id='a')
        assert store.claim_next(A) is None
        assert store.recover_uncertain() >= 1
        assert store.deliveries(A)[0].state is DeliveryState.REVIEW
        assert store.claim_next(A) is None


@pytest.mark.parametrize('version', [0, 3])
def test_unknown_schema_does_not_modify_database(tmp_path, version):
    path = tmp_path / 'db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE marker (value TEXT)')
        db.execute("INSERT INTO marker VALUES ('preserved')")
        db.execute(f'PRAGMA user_version={version}')
    with pytest.raises(SchemaVersionError):
        CollaborationStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('SELECT value FROM marker').fetchone()[0] == 'preserved'


def test_schema_one_migrates_preserving_assignments_and_stop_fences(tmp_path):
    path = tmp_path / 'db'
    with CollaborationStore(path) as store:
        scope, request = make_request(store)
        store.restrict_scope(scope.id, A, ScopeState.RECEIVE_ONLY, expected_version=0)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE requests DROP COLUMN continuation')
        db.execute('ALTER TABLE requests DROP COLUMN control_version')
        db.execute("CREATE UNIQUE INDEX one_active_scope ON scopes(project,thread) "
                   "WHERE state='active'")
        db.execute('PRAGMA user_version=1')
    with CollaborationStore(path) as store:
        assert store.get_request(request.id, A).body == 'help'
        assert store.get_scope(scope.id, A).state is ScopeState.RECEIVE_ONLY
        assert store.get_request(request.id, A).control_version == 0
        assert len(store.messages(request.id, A)) == 1
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2


def test_progress_is_durable_notification_without_a_new_source_turn(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        _, request = make_request(store)
        delivery = store.claim_next(B)
        store.transition_delivery(delivery.id, B, DeliveryState.RUNNING,
                                  expected_version=delivery.version, turn_id='b')
        post(store, request, B, MessageKind.PROGRESS, '检查到阻塞，正在核对')
        assert store.get_request(request.id, A).state is RequestState.WORKING
        assert store.messages(request.id, A)[-1].kind is MessageKind.PROGRESS
        assert store.deliveries(A)[0].state is DeliveryState.SETTLED
        assert store.claim_next(A) is None


def test_assignment_control_does_not_fence_its_siblings(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        scope, first = make_request(store)
        _, second = make_request(store, key='second')
        store.control_request(first.id, A, expected_version=0, receive_only=False)
        assert store.get_request(first.id, A).state is RequestState.CANCELLED
        assert store.get_request(second.id, A).continuation is ScopeState.ACTIVE
        assert store.get_scope(scope.id, A).state is ScopeState.ACTIVE
        assert store.claim_next(B).request_id == second.id


def test_receive_only_fences_a_claimed_source_delivery(tmp_path):
    with CollaborationStore(tmp_path / 'db') as store:
        _, request = make_request(store)
        target = store.claim_next(B)
        running = store.transition_delivery(target.id, B, DeliveryState.RUNNING,
                                            expected_version=target.version, turn_id='b')
        post(store, request, B, MessageKind.REPLY, 'final')
        store.transition_delivery(target.id, B, DeliveryState.SETTLED,
                                  expected_version=running.version, turn_id='b')
        source = store.claim_next(A)
        store.control_request(request.id, A, expected_version=0, receive_only=True)
        with pytest.raises(ConflictError):
            store.transition_delivery(source.id, A, DeliveryState.RUNNING,
                                      expected_version=source.version, turn_id='a')
        assert store.get_delivery(source.id, A).state is DeliveryState.REVIEW
