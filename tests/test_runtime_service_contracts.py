"""S1 Agent Runtime Service: DTO contracts and architecture guards."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime
import decimal
import enum
import json
import os
import pathlib
import uuid
from collections.abc import Mapping

import pytest

from synapse.runtime.service.commands import CommandReceipt, SubmitTurnCommand
from synapse.runtime.service.errors import (
    ArtifactChangedError,
    ArtifactForbiddenError,
    ArtifactNotFoundError,
    ArtifactOverflowError,
    ArtifactUnavailableError,
    ClosedError,
    ConflictError,
    EventOverflowError,
    InvalidArtifactCursorError,
    InvalidArtifactPathError,
    InvalidCursorError,
    InvalidEventPayloadError,
    InvalidRequestError,
    InvalidSessionError,
    NotFoundError,
    ReplayGapError,
    RuntimeServiceError,
)
from synapse.runtime.service.events import (
    EventCursor,
    EventPage,
    RuntimeEvent,
    project_payload,
)
from synapse.runtime.service.queries import GetSessionQuery, SessionView, UsageView
from synapse.runtime.sessions.ref import SessionRef

_PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "synapse"
    / "runtime"
    / "service"
)

_REF = SessionRef(project_id="p1", thread_id="t1")


def _module_sources() -> list[str]:
    return [path.read_text(encoding="utf-8") for path in sorted(_PACKAGE.glob("*.py"))]


def _import_lines() -> list[str]:
    lines: list[str] = []
    for source in _module_sources():
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                lines.append(stripped.casefold())
    return lines


def test_submit_turn_command_is_frozen_and_guards_config_mutation() -> None:
    overrides = {"model": "fast", "threads": 2}
    command = SubmitTurnCommand(
        session=_REF,
        text="hello",
        config_overrides=overrides,
        attachments=("note.txt", 42),
    )

    # External mutation of the source dict must not reach the command.
    overrides["model"] = "slow"
    assert command.config_overrides["model"] == "fast"
    assert command.config_overrides["threads"] == 2

    # The stored mapping itself is read-only.
    with pytest.raises(TypeError):
        command.config_overrides["model"] = "slow"  # type: ignore[index]
    with pytest.raises(TypeError):
        command.config_overrides["new"] = 1  # type: ignore[index]

    assert dataclasses.is_dataclass(command)
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.text = "mutated"  # type: ignore[misc]


def test_submit_turn_command_id_is_stable_and_unique() -> None:
    first = SubmitTurnCommand(session=_REF, text="a")
    second = SubmitTurnCommand(session=_REF, text="b")

    assert isinstance(first.command_id, str)
    assert first.command_id == first.command_id
    assert first.command_id != second.command_id


def test_command_receipt_exposes_no_runtime_objects() -> None:
    receipt = CommandReceipt(command_id="c1", session=_REF, turn_id="t1")

    assert {field.name for field in dataclasses.fields(receipt)} == {
        "command_id",
        "session",
        "turn_id",
        "accepted",
    }
    assert not hasattr(receipt, "handle")
    assert not hasattr(receipt, "future")
    assert not hasattr(receipt, "task")
    assert receipt.accepted is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.turn_id = "other"  # type: ignore[misc]


def test_read_dtos_are_frozen_and_json_safe() -> None:
    view = SessionView(
        project_id="p1",
        thread_id="t1",
        status="running",
        active_turn_id="turn-1",
        latest_sequence=3,
        usage=UsageView(input_tokens=10, output_tokens=4),
        last_error=None,
        last_activity_at="2025-01-01T00:00:00+00:00",
    )
    event = RuntimeEvent(
        sequence=3,
        turn_sequence=2,
        turn_id="turn-1",
        kind="answer_delta",
        payload={"text": "hi"},
        version=1,
    )
    page = EventPage(
        session=_REF,
        events=(event,),
        cursor=EventCursor(sequence=3),
        latest_sequence=3,
    )

    assert dataclasses.is_dataclass(view) and dataclasses.is_dataclass(page)
    assert json.loads(json.dumps(dataclasses.asdict(view)))["status"] == "running"
    assert json.loads(json.dumps(dataclasses.asdict(event)))["kind"] == "answer_delta"
    assert json.loads(json.dumps(dataclasses.asdict(page)))["cursor"]["sequence"] == 3
    assert json.loads(json.dumps(dataclasses.asdict(GetSessionQuery(session=_REF))))

    with pytest.raises(dataclasses.FrozenInstanceError):
        view.status = "idle"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        page.events = ()  # type: ignore[misc]


def test_session_view_never_exposes_goal() -> None:
    view = SessionView(
        project_id="p1",
        thread_id="t1",
        status="idle",
        active_turn_id=None,
        latest_sequence=0,
        usage=UsageView(),
        last_error=None,
        last_activity_at="2025-01-01T00:00:00+00:00",
    )
    assert not hasattr(view, "goal")
    assert {field.name for field in dataclasses.fields(view)} == {
        "project_id",
        "thread_id",
        "status",
        "active_turn_id",
        "latest_sequence",
        "usage",
        "last_error",
        "last_activity_at",
    }


def test_error_codes_are_stable() -> None:
    assert NotFoundError("x").code == "not_found"
    assert ConflictError("x").code == "conflict"
    assert ReplayGapError("x").code == "replay_gap"
    assert ClosedError("x").code == "closed"
    assert InvalidSessionError("x").code == "invalid_session"
    assert EventOverflowError("x").code == "event_overflow"
    assert ArtifactNotFoundError("x").code == "artifact_not_found"
    assert ArtifactForbiddenError("x").code == "artifact_forbidden"
    assert ArtifactChangedError("x").code == "artifact_changed"
    assert ArtifactUnavailableError("x").code == "artifact_unavailable"
    assert ArtifactOverflowError("x").code == "artifact_overflow"
    assert InvalidArtifactCursorError("x").code == "invalid_artifact_cursor"
    assert InvalidArtifactPathError("x").code == "invalid_artifact_path"
    assert RuntimeServiceError("x").code == "runtime_error"
    assert RuntimeServiceError("x", code="custom").code == "custom"

    for error in (
        NotFoundError("m"),
        ConflictError("m"),
        ReplayGapError("m"),
        ClosedError("m"),
        InvalidSessionError("m"),
        EventOverflowError("m"),
        ArtifactNotFoundError("m"),
        ArtifactForbiddenError("m"),
        ArtifactChangedError("m"),
        ArtifactUnavailableError("m"),
        ArtifactOverflowError("m"),
        InvalidArtifactCursorError("m"),
        InvalidArtifactPathError("m"),
    ):
        assert isinstance(error, RuntimeServiceError)
        assert error.message == "m"
        assert str(error) == "m"


def test_service_package_imports_no_transport_or_ui_frameworks() -> None:
    banned = (
        "synapse.ui",
        "textual",
        "typer",
        "synapse.acp",
        "rich",
        "http",
        "websocket",
        "langchain",
    )
    for line in _import_lines():
        for token in banned:
            assert token not in line, f"banned import token {token!r} in: {line}"


def test_service_never_instantiates_turn_runtime_or_streams_directly() -> None:
    for source in _module_sources():
        assert "AgentTurnRuntime(" not in source
        assert "stream_agent" not in source
        assert "ainvoke" not in source


class _SampleMode(enum.StrEnum):
    FAST = "fast"
    SLOW = "slow"


@dataclasses.dataclass
class _NestedPayload:
    path: pathlib.Path
    mode: _SampleMode
    stamp: datetime.datetime
    when: datetime.date
    at: datetime.time
    uid: uuid.UUID
    amount: decimal.Decimal
    blob: bytes
    items: tuple[int, str]
    tags: frozenset[str]
    seen: set[int]
    mapping: dict[str, object]


def _nested_payload() -> _NestedPayload:
    return _NestedPayload(
        path=pathlib.Path("/tmp/x.txt"),
        mode=_SampleMode.FAST,
        stamp=datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        when=datetime.date(2024, 1, 2),
        at=datetime.time(3, 4, 5),
        uid=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        amount=decimal.Decimal("12.50"),
        blob=b"hello",
        items=(1, "two"),
        tags=frozenset({"b", "a"}),
        seen={3, 1, 2},
        mapping={"nested": {"deep": True}},
    )


def test_payload_projection_is_strict_json_and_deterministic() -> None:
    event = RuntimeEvent(
        sequence=1,
        turn_sequence=1,
        turn_id="turn-1",
        kind="answer_delta",
        payload=project_payload(_nested_payload()),
        version=1,
    )
    # The whole DTO must survive strict JSON serialization.
    encoded = json.dumps(dataclasses.asdict(event), allow_nan=False)
    payload = json.loads(encoded)["payload"]
    assert payload == {
        "path": str(pathlib.Path("/tmp/x.txt")),
        "mode": "fast",
        "stamp": "2024-01-02T03:04:05+00:00",
        "when": "2024-01-02",
        "at": "03:04:05",
        "uid": "12345678-1234-5678-1234-567812345678",
        "amount": "12.50",
        "blob": {"$base64": base64.b64encode(b"hello").decode("ascii")},
        "items": [1, "two"],
        "tags": ["a", "b"],
        "seen": [1, 2, 3],
        "mapping": {"nested": {"deep": True}},
    }
    # Deterministic: re-encoding yields the identical document.
    assert json.dumps(dataclasses.asdict(event), allow_nan=False, sort_keys=True) == (
        json.dumps(json.loads(encoded), sort_keys=True)
    )
    # The projected payload is a fresh object graph, not the producer's.
    assert event.payload is not _nested_payload()
    assert isinstance(event.payload, dict)
    assert event.payload["mapping"] is not _nested_payload().mapping


def test_payload_projection_rejects_non_json_safe_structures() -> None:
    with pytest.raises(InvalidEventPayloadError):
        project_payload(float("nan"))
    with pytest.raises(InvalidEventPayloadError):
        project_payload(float("inf"))
    with pytest.raises(InvalidEventPayloadError):
        project_payload(float("-inf"))
    with pytest.raises(InvalidEventPayloadError):
        project_payload(object())
    with pytest.raises(InvalidEventPayloadError):
        project_payload({1: "non-string-key"})

    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    with pytest.raises(InvalidEventPayloadError):
        project_payload(cycle)

    deep: object = 0
    for _ in range(105):
        deep = [deep]
    with pytest.raises(InvalidEventPayloadError):
        project_payload(deep)

    # A nested NaN anywhere in the structure is rejected, never rounded.
    with pytest.raises(InvalidEventPayloadError):
        project_payload({"nested": {"value": float("nan")}})


def test_payload_projection_errors_never_echo_secret_values() -> None:
    secret = b"super-secret-bytes-key"
    with pytest.raises(InvalidEventPayloadError) as key_exc:
        project_payload({secret: "value"})
    key_message = str(key_exc.value)
    # Only the safe type name is reported; the raw key must never be echoed.
    assert "bytes" in key_message
    assert "super-secret" not in key_message

    for non_finite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(InvalidEventPayloadError) as float_exc:
            project_payload(non_finite)
        float_message = str(float_exc.value)
        assert "nan" not in float_message.lower()
        assert "inf" not in float_message.lower()

    with pytest.raises(InvalidEventPayloadError) as unknown_exc:
        project_payload(object())
    # Unknown types may still report their type name.
    assert "object" in str(unknown_exc.value)


class _EvilMappingItems(Mapping):
    """Mapping whose ``items()`` raises a secret-bearing ``RuntimeError``."""

    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self):
        return iter(("k",))

    def __len__(self) -> int:
        return 1

    def items(self):
        raise RuntimeError("secret=mapping-items")


@dataclasses.dataclass
class _EvilDataclassGetter:
    value: int = 1

    def __getattribute__(self, name: str) -> object:
        if name == "value":
            raise RuntimeError("secret=dataclass-getattr")
        return super().__getattribute__(name)


class _EvilListIter(list):
    def __iter__(self):
        raise RuntimeError("secret=list-iter")


class _EvilInvalidEventPayload(Mapping):
    """Producer hook that deliberately raises ``InvalidEventPayloadError``.

    The producer may construct the service error itself (possibly embedding
    secret text); the public boundary must treat it as an untrusted producer
    exception, not as an internal rejection, and sanitize it.
    """

    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self):
        return iter(("k",))

    def __len__(self) -> int:
        return 1

    def items(self):
        raise InvalidEventPayloadError("secret=producer-invalid-event-payload")


class _EvilSetIter(set):
    def __iter__(self):
        raise RuntimeError("secret=set-iter")


class _EvilEnumValue(enum.Enum):
    A = 1

    def __getattribute__(self, name: str) -> object:
        if name == "value":
            raise RuntimeError("secret=enum-value")
        return super().__getattribute__(name)


class _EvilFspath(os.PathLike):
    def __fspath__(self):
        raise RuntimeError("secret=fspath")


class _EvilDatetimeIsoformat(datetime.datetime):
    def isoformat(self) -> str:
        raise RuntimeError("secret=datetime-isoformat")


class _EvilDecimalStr(decimal.Decimal):
    def __str__(self) -> str:
        raise RuntimeError("secret=decimal-str")


class _EvilUUIDStr(uuid.UUID):
    def __str__(self) -> str:
        raise RuntimeError("secret=uuid-str")


@pytest.mark.parametrize(
    ("payload", "type_name"),
    [
        pytest.param(_EvilMappingItems(), "_EvilMappingItems", id="mapping-items"),
        pytest.param(_EvilDataclassGetter(), "_EvilDataclassGetter", id="dataclass-getattr"),
        pytest.param(_EvilListIter(), "_EvilListIter", id="list-iteration"),
        pytest.param(_EvilSetIter(), "_EvilSetIter", id="set-iteration"),
        pytest.param(_EvilEnumValue.A, "_EvilEnumValue", id="enum-value"),
        pytest.param(_EvilFspath(), "_EvilFspath", id="pathlike-fspath"),
        pytest.param(
            _EvilDatetimeIsoformat(2024, 1, 2), "_EvilDatetimeIsoformat", id="datetime-isoformat"
        ),
        pytest.param(_EvilDecimalStr("1.5"), "_EvilDecimalStr", id="decimal-str"),
        pytest.param(_EvilUUIDStr(int=1), "_EvilUUIDStr", id="uuid-str"),
    ],
)
def test_payload_projection_sanitizes_producer_exceptions(
    payload: object, type_name: str
) -> None:
    """Any ordinary Exception raised by producer code while projecting is
    converted at the public boundary into a sanitized InvalidEventPayloadError:
    the message names only the safe top-level type, never the original
    exception text, and ``__cause__`` is suppressed so no secret leaks."""
    with pytest.raises(InvalidEventPayloadError) as excinfo:
        project_payload(payload)
    message = str(excinfo.value)
    assert type_name in message
    assert "secret" not in message
    assert "secret=" not in message
    assert excinfo.value.code == "invalid_event_payload"
    assert excinfo.value.__cause__ is None


def test_payload_projection_sanitizes_released_memoryview_bytes_conversion() -> None:
    released = memoryview(bytearray(b"abc"))
    released.release()
    with pytest.raises(InvalidEventPayloadError) as excinfo:
        project_payload(released)
    assert "memoryview" in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_payload_projection_sanitizes_nested_producer_exceptions() -> None:
    """A producer exception deep inside the structure never bypasses the
    boundary; the sanitized message names only the safe top-level type."""
    payload: object = {"nested": _EvilMappingItems()}
    with pytest.raises(InvalidEventPayloadError) as excinfo:
        project_payload(payload)
    message = str(excinfo.value)
    assert "dict" in message
    assert "secret" not in message
    assert excinfo.value.__cause__ is None


def test_payload_projection_preserves_internal_rejection_messages() -> None:
    """Internally produced InvalidEventPayloadError (unknown type) keeps its
    own safe message; it is never re-wrapped by the boundary."""
    with pytest.raises(InvalidEventPayloadError) as excinfo:
        project_payload(object())
    assert "has no JSON projection" in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_payload_projection_sanitizes_producer_raised_invalid_event_payload() -> None:
    """A producer hook that deliberately raises InvalidEventPayloadError is an
    untrusted producer exception, not an internal rejection: the public
    boundary builds a fresh sanitized error naming only the safe top-level
    type, with the secret-bearing message and any __cause__ suppressed."""
    with pytest.raises(InvalidEventPayloadError) as excinfo:
        project_payload(_EvilInvalidEventPayload())
    message = str(excinfo.value)
    assert "_EvilInvalidEventPayload" in message
    assert "secret" not in message
    assert excinfo.value.code == "invalid_event_payload"
    assert excinfo.value.__cause__ is None


def test_payload_projection_sanitizes_nested_producer_raised_invalid_event_payload() -> None:
    """A producer-raised InvalidEventPayloadError at any nesting depth still
    crosses the public boundary and is sanitized (message names only the safe
    top-level type; no secret leaks)."""
    payload: object = {"nested": _EvilInvalidEventPayload()}
    with pytest.raises(InvalidEventPayloadError) as excinfo:
        project_payload(payload)
    message = str(excinfo.value)
    assert "dict" in message
    assert "secret" not in message
    assert excinfo.value.__cause__ is None


def test_payload_projection_internal_rejections_use_exact_public_type() -> None:
    """Internal rejections (depth/cycle/key/NaN/unknown) surface as exactly the
    public InvalidEventPayloadError type — never the private trusted marker —
    with the safe message preserved and no __cause__."""
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    deep: object = 0
    for _ in range(105):
        deep = [deep]
    cases = [
        (object(), "has no JSON projection"),
        ({1: "k"}, "non-string mapping key"),
        (cycle, "cyclic reference"),
        (deep, "maximum projection depth"),
        (float("nan"), "non-finite float"),
    ]
    for value, fragment in cases:
        with pytest.raises(InvalidEventPayloadError) as excinfo:
            project_payload(value)
        assert type(excinfo.value) is InvalidEventPayloadError
        assert fragment in str(excinfo.value)
        assert excinfo.value.__cause__ is None


def test_payload_projection_never_swallows_base_exceptions() -> None:
    class _EvilKeyboard(Mapping):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self):
            return iter(("k",))

        def __len__(self) -> int:
            return 1

        def items(self):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        project_payload(_EvilKeyboard())

    class _EvilCancelled(Mapping):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self):
            return iter(("k",))

        def __len__(self) -> int:
            return 1

        def items(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        project_payload(_EvilCancelled())


class _HashableDict(Mapping):
    """Hashable str-keyed mapping wrapper.

    Projection yields a dict whose insertion order follows the wrapper's
    iteration order, so two wrappers with equal content but different
    insertion orders exercise the canonical set sort key.
    """

    def __init__(self, data: dict[str, object]) -> None:
        self._data = dict(data)
        # Canonical JSON text: equal content always hashes equal, and dict
        # insertion order never influences the hash.
        self._hash = hash(
            json.dumps(self._data, sort_keys=True, separators=(",", ":"))
        )

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _HashableDict) and self._data == other._data


@dataclasses.dataclass(frozen=True)
class _FrozenKey:
    name: str
    rank: int


def test_set_projection_uses_canonical_json_sort_not_repr() -> None:
    left = _HashableDict({"z": 1, "a": 2})  # projected dict keeps insertion order
    right = _HashableDict({"m": 1})
    projected = project_payload(frozenset({left, right}))
    # Canonical JSON keys ('{"a":2,"z":1}' vs '{"m":1}') sort the left element
    # first even though its repr '{"z": 1, "a": 2}' sorts after '{"m": 1}'.
    assert projected == [{"z": 1, "a": 2}, {"m": 1}]
    for _ in range(5):
        assert project_payload(frozenset({left, right})) == projected


@pytest.mark.parametrize(
    "payload",
    [
        frozenset({"c", "a", "b"}),
        frozenset({3, 1, 2}),
        frozenset({1, "two", 3.5}),
        frozenset({_FrozenKey("a", 1), _FrozenKey("b", 2)}),
        frozenset(
            {_HashableDict({"x": 1}), _HashableDict({"y": {"deep": [1, 2]}})}
        ),
    ],
)
def test_set_projection_is_deterministic(payload: object) -> None:
    first = project_payload(payload)
    for _ in range(5):
        assert project_payload(payload) == first
    # Canonical JSON text is injective over projected values: distinct set
    # elements never collapse into one projected entry.
    assert len(first) == len(payload)


def test_new_error_codes_are_stable_and_exported() -> None:
    from synapse.runtime import service as service_pkg
    from synapse.runtime.service import errors as service_errors

    assert service_errors.InvalidCursorError is InvalidCursorError
    assert service_errors.InvalidEventPayloadError is InvalidEventPayloadError
    assert service_errors.InvalidRequestError is InvalidRequestError
    assert service_pkg.InvalidCursorError is InvalidCursorError
    assert service_pkg.InvalidEventPayloadError is InvalidEventPayloadError
    assert service_pkg.InvalidRequestError is InvalidRequestError

    assert InvalidCursorError("x").code == "invalid_cursor"
    assert InvalidEventPayloadError("x").code == "invalid_event_payload"
    assert InvalidRequestError("x").code == "invalid_request"
    for error in (
        InvalidCursorError("m"),
        InvalidEventPayloadError("m"),
        InvalidRequestError("m"),
    ):
        assert isinstance(error, RuntimeServiceError)
        assert error.message == "m"


def test_watch_protocol_is_context_only_lease() -> None:
    from synapse.runtime.service.local import LocalEventWatch
    from synapse.runtime.service.ports import AgentRuntimeService, EventStream, EventWatch

    # The lease is an async context manager only: no iterator methods exist at
    # the protocol or concrete level, so bare `async for watch_events(...)` is
    # structurally impossible.
    assert not hasattr(EventWatch, "__aiter__")
    assert not hasattr(EventWatch, "__anext__")
    assert hasattr(EventWatch, "__aenter__")
    assert hasattr(EventWatch, "__aexit__")
    assert not hasattr(LocalEventWatch, "__aiter__")
    assert not hasattr(LocalEventWatch, "__anext__")

    # The stream is the async iterator returned by entering the lease.
    assert hasattr(EventStream, "__aiter__")
    assert hasattr(EventStream, "__anext__")
    assert AgentRuntimeService.watch_events.__annotations__["return"] == "EventWatch"


def test_submit_turn_command_deep_copies_nested_overrides() -> None:
    nested = {"model": {"temperature": 0.2, "tags": ["a"]}}
    overrides = {"nested": nested}
    command = SubmitTurnCommand(session=_REF, text="hello", config_overrides=overrides)

    # Mutating the caller's nested containers after construction must not
    # reach the command.
    nested["model"]["temperature"] = 0.9
    nested["model"]["tags"].append("b")
    overrides["extra"] = 1
    assert command.config_overrides == {
        "nested": {"model": {"temperature": 0.2, "tags": ["a"]}}
    }
    assert "extra" not in command.config_overrides
    assert dataclasses.is_dataclass(command)
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.text = "mutated"  # type: ignore[misc]
