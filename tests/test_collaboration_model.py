"""Pure collaboration invariants, with no runtime, graph or filesystem."""

from dataclasses import FrozenInstanceError

import pytest

from synapse.runtime.collaboration.model import (
    MAX_BODY_BYTES,
    CollaborationError,
    ConflictError,
    ContinuationScope,
    DeliveryState,
    RequestState,
    ScopeState,
    bounded_text,
    check_transition,
    page_size,
    validate_ref,
)
from synapse.runtime.sessions.ref import SessionRef


@pytest.mark.parametrize(('before', 'after'), [
    (RequestState.PENDING, RequestState.WORKING),
    (RequestState.WORKING, RequestState.WAITING_INPUT),
    (RequestState.WAITING_INPUT, RequestState.PENDING),
    (RequestState.WORKING, RequestState.REPLIED),
    (DeliveryState.PENDING, DeliveryState.CLAIMED),
    (DeliveryState.CLAIMED, DeliveryState.REVIEW),
    (DeliveryState.REVIEW, DeliveryState.SETTLED),
    (ScopeState.ACTIVE, ScopeState.RECEIVE_ONLY),
    (ScopeState.RECEIVE_ONLY, ScopeState.REVOKED),
])
def test_allowed_transitions(before, after):
    check_transition(before, after)


@pytest.mark.parametrize(('before', 'after'), [
    (RequestState.REPLIED, RequestState.WORKING),
    (RequestState.CANCELLED, RequestState.PENDING),
    (DeliveryState.CLAIMED, DeliveryState.PENDING),
    (DeliveryState.REVIEW, DeliveryState.PENDING),
    (DeliveryState.SETTLED, DeliveryState.RUNNING),
    (ScopeState.REVOKED, ScopeState.ACTIVE),
    (ScopeState.RECEIVE_ONLY, ScopeState.ACTIVE),
    (RequestState.PENDING, DeliveryState.CLAIMED),
    ('pending', 'working'),
])
def test_forbidden_transitions(before, after):
    with pytest.raises(ConflictError):
        check_transition(before, after)


@pytest.mark.parametrize('value', ['', ' ', '\x00', '\ud800', 1, None, '密' * 100])
def test_invalid_identity_is_value_free(value):
    with pytest.raises(CollaborationError):
        bounded_text(value)


def test_text_is_exact_not_normalized_or_truncated():
    assert bounded_text(' id ') == ' id '
    assert bounded_text('x' * MAX_BODY_BYTES, maximum=MAX_BODY_BYTES) == 'x' * MAX_BODY_BYTES
    with pytest.raises(CollaborationError):
        bounded_text('x' * (MAX_BODY_BYTES + 1), maximum=MAX_BODY_BYTES)


@pytest.mark.parametrize('limit', [0, -1, True, 101, 1.2, '5'])
def test_bounded_pages(limit):
    with pytest.raises(CollaborationError):
        page_size(limit)


def test_reference_and_frozen_scope():
    ref = SessionRef('project', 'thread')
    assert validate_ref(ref) == ref
    with pytest.raises(CollaborationError):
        validate_ref(SessionRef('', 'thread'))
    with pytest.raises(CollaborationError):
        validate_ref('project:thread')
    scope = ContinuationScope('s', ref, 'work', ScopeState.ACTIVE, 0)
    with pytest.raises(FrozenInstanceError):
        scope.state = ScopeState.REVOKED
