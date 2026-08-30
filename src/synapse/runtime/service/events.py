"""S1-S3 event DTOs: cursor, filters, projection, pagination, and size limits."""

from __future__ import annotations

import base64
import dataclasses
import datetime as _datetime
import decimal
import enum
import json
import math
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from synapse.runtime.service.errors import InvalidEventPayloadError, InvalidRequestError
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import TurnEventKind

__all__ = [
    "JSONValue",
    "EventCursor",
    "EventFilter",
    "EventPage",
    "DEFAULT_MAX_EVENT_BYTES",
    "MIN_EVENT_BYTES",
    "MAX_EVENT_BYTES",
    "DEFAULT_SCAN_LIMIT",
    "MIN_SCAN_LIMIT",
    "MAX_SCAN_LIMIT",
    "ReadEventsQuery",
    "RuntimeEvent",
    "project_payload",
]

#: Strict JSON-safe projection of one event payload.  The service guarantees
#: ``json.dumps(dataclasses.asdict(event), allow_nan=False)`` succeeds for
#: every ``RuntimeEvent`` it produces.
type JSONValue = None | bool | str | int | float | list["JSONValue"] | dict[str, "JSONValue"]

_MAX_PROJECTION_DEPTH = 100
_KNOWN_EVENT_KINDS = frozenset(kind.value for kind in TurnEventKind)
DEFAULT_MAX_EVENT_BYTES = 1024 * 1024
MIN_EVENT_BYTES = 1024
MAX_EVENT_BYTES = 8 * 1024 * 1024
DEFAULT_SCAN_LIMIT = 1024
MIN_SCAN_LIMIT = 1
MAX_SCAN_LIMIT = 4096


class _ProjectionRejected(InvalidEventPayloadError):
    """Trusted internal rejection raised by the projection helpers.

    Only code inside this module raises this type; it marks rejections whose
    message is already value-free (depth, cycles, non-string keys, non-finite
    floats, unknown types).  The public boundary converts it into a plain
    :class:`InvalidEventPayloadError` so the private marker never escapes, and
    — crucially — a producer hook that deliberately raises
    :class:`InvalidEventPayloadError` (which is *not* this type) is treated as
    an untrusted producer exception and sanitized like any other ``Exception``.
    """


def project_payload(value: object) -> JSONValue:
    """Recursively project an arbitrary producer payload to strict JSON data.

    Supported and deterministically projected:
    ``None``/``bool``/``str``/``int``/finite ``float``; ``Enum`` values;
    dataclasses; str-key ``Mapping``; ``list``/``tuple``; ``set``/``frozenset``
    (stable sort); ``os.PathLike`` and ``uuid.UUID`` as strings;
    ``datetime``/``date``/``time`` as ISO-8601; ``Decimal`` as a string; and
    bytes as an explicit base64-tagged object ``{"$base64": "..."}``.

    Rejected with :class:`InvalidEventPayloadError`: non-string mapping keys,
    NaN/Infinity, cyclic references, structures deeper than
    ``_MAX_PROJECTION_DEPTH``, and unknown object types.  The result is a new
    object graph that never shares mutable containers with the producer.

    This is the public safety boundary.  The service's own rejections are
    raised internally as the trusted ``_ProjectionRejected`` marker and surface
    as a plain :class:`InvalidEventPayloadError` with their safe message.  Any
    ``Exception`` raised by producer code while projecting (``Mapping.items()``,
    dataclass getters, iterators, ``Enum.value``, ``os.fspath``,
    ``isoformat``, ...) — *including* an ``InvalidEventPayloadError`` a
    producer hook raises itself — is converted into a fresh
    :class:`InvalidEventPayloadError` whose message names only the safe
    top-level type, never the original exception text, its ``repr``, payload
    values, or ``__cause__`` (raised ``from None``), so no secret leaks at any
    nesting depth.  ``BaseException`` (``KeyboardInterrupt``/``SystemExit``/
    ``asyncio.CancelledError``) always propagates untouched.
    """
    try:
        return _project(value, depth=0, active=set())
    except _ProjectionRejected as exc:
        # Trusted internal rejection: the message is already value-free and
        # stable (depth/cycle/key/NaN/unknown); surface it as the exact public
        # type with no chain so nothing internal ever escapes.
        raise InvalidEventPayloadError(exc.message) from None
    except Exception:
        raise InvalidEventPayloadError(
            f"event payload value of type {type(value).__name__!r} raised an "
            "unexpected error while being projected to strict JSON data"
        ) from None


def _project(value: object, *, depth: int, active: set[int]) -> JSONValue:
    if depth > _MAX_PROJECTION_DEPTH:
        raise _ProjectionRejected(
            f"event payload exceeds the maximum projection depth "
            f"{_MAX_PROJECTION_DEPTH}"
        )
    if value is None or isinstance(value, (bool, str, int)):
        return value  # type: ignore[return-value]
    if isinstance(value, float):
        if not math.isfinite(value):
            # Never echo the value: it is not a secret, but keeping the
            # message value-free makes the leak surface uniform.
            raise _ProjectionRejected(
                f"non-finite float of type {type(value).__name__!r} is not JSON-safe"
            )
        return value
    if isinstance(value, enum.Enum):
        return _project(value.value, depth=depth + 1, active=active)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _project(getattr(value, field.name), depth=depth + 1, active=active)
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return _project_mapping(value, depth=depth, active=active)
    if isinstance(value, (list, tuple)):
        return _project_sequence(value, depth=depth, active=active)
    if isinstance(value, (set, frozenset)):
        return _project_set(value, depth=depth, active=active)
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {"$base64": encoded}
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, os.PathLike):
        path = os.fspath(value)
        if isinstance(path, str):
            return path
        encoded = base64.b64encode(os.fsencode(path)).decode("ascii")
        return {"$base64": encoded}
    raise _ProjectionRejected(
        f"event payload value of type {type(value).__name__!r} has no JSON projection"
    )


def _project_mapping(
    value: Mapping[object, object], *, depth: int, active: set[int]
) -> dict[str, JSONValue]:
    marker = id(value)
    if marker in active:
        raise _ProjectionRejected("event payload contains a cyclic reference")
    active.add(marker)
    try:
        projected: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                # Only the type name is reported; the key itself may carry
                # secret bytes and must never be echoed back.
                raise _ProjectionRejected(
                    f"event payload contains a non-string mapping key of type "
                    f"{type(key).__name__!r}"
                )
            projected[key] = _project(item, depth=depth + 1, active=active)
        return projected
    finally:
        active.discard(marker)


def _project_sequence(
    value: list[object] | tuple[object, ...], *, depth: int, active: set[int]
) -> list[JSONValue]:
    marker = id(value)
    if marker in active:
        raise _ProjectionRejected("event payload contains a cyclic reference")
    active.add(marker)
    try:
        return [_project(item, depth=depth + 1, active=active) for item in value]
    finally:
        active.discard(marker)


def _project_set(
    value: set[object] | frozenset[object], *, depth: int, active: set[int]
) -> list[JSONValue]:
    marker = id(value)
    if marker in active:
        raise _ProjectionRejected("event payload contains a cyclic reference")
    active.add(marker)
    try:
        projected = [_project(item, depth=depth + 1, active=active) for item in value]
    finally:
        active.discard(marker)
    # Canonical JSON text is the sort key: ``sort_keys`` makes dict insertion
    # order irrelevant, and the JSON grammar is injective over JSON values, so
    # equivalent projected values always order identically.  Never fall back
    # to ``repr`` (insertion-order dependent for dicts).
    return sorted(
        projected,
        key=lambda item: json.dumps(
            item,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    )


@dataclass(frozen=True, slots=True)
class EventCursor:
    """Client cursor over session events.

    The cursor value is always the session sequence
    (``SessionEventEnvelope.sequence``), never the turn-local sequence.
    """

    sequence: int


@dataclass(frozen=True, slots=True, init=False)
class EventFilter:
    """Immutable AND filter for event kind and turn identity."""

    kinds: frozenset[str]
    turn_ids: frozenset[str]

    def __init__(
        self,
        kinds: object = frozenset(),
        turn_ids: object = frozenset(),
    ) -> None:
        object.__setattr__(self, "kinds", _canonical_filter_values(kinds, "kinds"))
        object.__setattr__(self, "turn_ids", _canonical_filter_values(turn_ids, "turn_ids"))


def _canonical_filter_values(value: object, field: str) -> frozenset[str]:
    """Validate and copy filter collections without echoing their values."""
    if not isinstance(value, (set, frozenset, list, tuple)):
        raise InvalidRequestError(
            f"{field} must be a collection of strings, got {type(value).__name__!r}"
        )
    result: set[str] = set()
    try:
        for item in value:
            if not isinstance(item, str):
                raise InvalidRequestError(
                    f"{field} items must be strings, got {type(item).__name__!r}"
                )
            if field == "turn_ids" and not item:
                raise InvalidRequestError("turn_ids items must be non-empty strings")
            if field == "kinds" and item not in _KNOWN_EVENT_KINDS:
                allowed = ", ".join(sorted(_KNOWN_EVENT_KINDS))
                raise InvalidRequestError(
                    f"kinds contains an unknown event kind; allowed kinds: {allowed}"
                )
            result.add(item)
    except InvalidRequestError:
        raise
    except Exception:
        raise InvalidRequestError(
            f"{field} must be a collection of strings, got {type(value).__name__!r}"
        ) from None
    return frozenset(result)


def matches_event(envelope: object, event_filter: EventFilter) -> bool:
    """Match raw envelope metadata before projecting its potentially hostile payload."""
    event = envelope.event  # type: ignore[attr-defined]
    return (
        (not event_filter.kinds or event.kind.value in event_filter.kinds)
        and (not event_filter.turn_ids or event.turn_id in event_filter.turn_ids)
    )


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One session event as pure data; never wraps a ``TurnEvent`` instance.

    ``sequence`` is the session cursor; ``turn_sequence`` preserves the
    turn-local sequence as an independent field (ADR-005).  ``payload`` is a
    strict JSON projection of the producer payload (see ``project_payload``),
    so the event is safe to serialize and replay: the whole dataclass passes
    ``json.dumps(dataclasses.asdict(event), allow_nan=False)``.
    """

    sequence: int
    turn_sequence: int
    turn_id: str
    kind: str
    payload: JSONValue
    version: int


@dataclass(frozen=True, slots=True)
class ReadEventsQuery:
    """Poll one page of session events after a session cursor."""

    session: SessionRef
    after: int = 0
    limit: int = 256
    filter: EventFilter = EventFilter()
    scan_limit: int = DEFAULT_SCAN_LIMIT
    max_event_bytes: int = 1024 * 1024


@dataclass(frozen=True, slots=True)
class EventPage:
    """One page of session events plus the next cursor to resume from."""

    session: SessionRef
    events: tuple[RuntimeEvent, ...]
    cursor: EventCursor
    latest_sequence: int
    has_more: bool = False
    scanned_through: EventCursor | None = None
