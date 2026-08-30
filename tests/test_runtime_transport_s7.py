from __future__ import annotations

# Wire-boundary fixtures intentionally keep some long JSON-shaped cases readable.
# ruff: noqa: E501
import asyncio
import dataclasses
import json
import os
import subprocess
import sys

import pytest

from synapse.runtime.service import SessionView, UsageView
from synapse.runtime.service.errors import RuntimeServiceError
from synapse.runtime.transport import (
    JSONRPC_VERSION,
    RUNTIME_WIRE_VERSION,
    ProtocolError,
    decode_params,
    encode_error,
    encode_response,
    parse_request,
    project_result,
)
from synapse.runtime.transport.protocol import WireProjectionError


def _request(params: object, *, method: str = "runtime.session.get", request_id: object = 1) -> str:
    return json.dumps(
        {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method, "params": params},
        ensure_ascii=False,
    )


def test_parser_rejects_duplicate_keys_nonfinite_depth_and_invalid_ids() -> None:
    cases = (
        '{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"method":"x","params":{}}',
        '{"jsonrpc":"2.0","id":1,"method":"x","params":{"x":NaN}}',
        '{"jsonrpc":"2.0","id":true,"method":"x","params":{}}',
        '{"jsonrpc":"2.0","id":null,"method":"x","params":{}}',
        '{"jsonrpc":"2.0","id":1,"method":"x","params":[]}',
        '{"jsonrpc":"1.0","id":1,"method":"x","params":{}}',
        '{"jsonrpc":"2.0","id":1,"method":"x","params":{},"extra":1}',
    )
    for value in cases:
        with pytest.raises(ProtocolError):
            parse_request(value)

    deep = "{" * 66 + "}" * 66
    with pytest.raises(ProtocolError):
        parse_request(deep)
    with pytest.raises(ProtocolError):
        parse_request(b"not-json")


def test_parser_validates_method_and_session_shape_without_evaluating_repr() -> None:
    with pytest.raises(ProtocolError):
        parse_request(_request({"session": {"project_id": "p", "thread_id": "t"}}, method=""))
    parsed = parse_request(_request({"session": {"project_id": "p", "thread_id": "t"}}))
    query = decode_params(parsed.method, parsed.params)
    assert query.session.project_id == "p"
    assert query.session.thread_id == "t"


def test_canonical_response_and_strict_result_projection() -> None:
    view = SessionView("项目", "thread", "idle", None, 0, UsageView(), None, "now")
    expected = (
        '{"id":7,"jsonrpc":"2.0","meta":{"wire_version":"1"},'
        '"result":{"active_turn_id":null,"last_activity_at":"now","last_error":null,'
        '"latest_sequence":0,"project_id":"项目","status":"idle","thread_id":"thread",'
        '"usage":{"cache_tokens":0,"input_tokens":0,"output_tokens":0}}}'
    )
    assert encode_response(7, view) == expected
    assert json.loads(encode_error(7, -32602, "invalid_params"))["meta"] == {
        "wire_version": RUNTIME_WIRE_VERSION
    }
    projected = project_result((1, frozenset({"a", "b"})))
    assert projected[0] == 1
    assert sorted(projected[1]) == ["a", "b"]
    with pytest.raises(WireProjectionError):
        project_result(float("nan"))
    with pytest.raises(WireProjectionError):
        project_result(object())


def test_all_business_parameter_conversions_are_strict() -> None:
    session = {"project_id": "p", "thread_id": "t"}
    values = {
        "runtime.session.open": {"session": session},
        "runtime.turn.submit": {"session": session, "text": "hi"},
        "runtime.turn.cancel": {"session": session, "expected_turn_id": "turn"},
        "runtime.turn.steer": {"session": session, "expected_turn_id": "turn", "text": "hi"},
        "runtime.session.close": {"session": session},
        "runtime.session.get": {"session": session},
        "runtime.events.read": {"session": session},
        "runtime.events.watch": {"session": session},
        "runtime.events.unwatch": {"subscription_id": "sub"},
        "runtime.artifacts.stat": {"ref": {"session": session, "path": "a.txt"}},
        "runtime.artifacts.list": {"session": session},
        "runtime.artifacts.read": {"ref": {"session": session, "path": "a.txt"}},
    }
    for method, params in values.items():
        assert decode_params(method, params) is not None

    with pytest.raises(ProtocolError):
        decode_params(
            "runtime.turn.submit",
            {"session": session, "text": "hi", "attachments": ["x"]},
        )
    with pytest.raises(ProtocolError):
        decode_params("runtime.session.get", {"session": session, "principal": "attacker"})


def test_dataclass_result_projection_is_not_default_str_fallback() -> None:
    @dataclasses.dataclass
    class Unknown:
        value: object

    with pytest.raises(WireProjectionError):
        project_result(Unknown(object()))


@pytest.mark.parametrize("value, valid", [("x" * 256, True), ("x" * 257, False), ("x\x00y", False)])
def test_protocol_identifier_boundaries(value: str, valid: bool) -> None:
    request = _request({"session": {"project_id": "p", "thread_id": "t"}}, request_id=value)
    if valid:
        assert parse_request(request).id == value
    else:
        with pytest.raises(ProtocolError):
            parse_request(request)


def test_protocol_exact_nesting_and_primitive_boundaries() -> None:
    def nested_dicts(count: int) -> object:
        value: object = 0
        for _ in range(count):
            value = {"nested": value}
        return value

    assert parse_request(_request({"nested": nested_dicts(61)}))
    with pytest.raises(ProtocolError):
        parse_request(_request({"nested": nested_dicts(63)}))
    assert parse_request(_request({"items": list(range(4096))}), max_bytes=2 * 1024 * 1024)
    with pytest.raises(ProtocolError):
        parse_request(_request({"items": list(range(4097))}), max_bytes=2 * 1024 * 1024)
    assert parse_request(_request({"value": 2**63 - 1}))
    with pytest.raises(ProtocolError):
        parse_request(_request({"value": 2**63}))


def test_protocol_canonical_set_is_hash_seed_independent() -> None:
    expected = encode_response(1, {"z", "a", "m", "b"})
    assert expected == encode_response(1, {"b", "m", "a", "z"})
    code = (
        "from synapse.runtime.transport import encode_response; "
        "print(encode_response(1, {'z','a','m','b'}), end='')"
    )
    outputs = []
    for seed in ("1", "987654"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "src"}
        outputs.append(subprocess.check_output([sys.executable, "-c", code], env=env, text=True))
    assert outputs[0] == outputs[1] == expected


@pytest.mark.parametrize("error", [ValueError("secret"), RuntimeError("secret")])
def test_projection_exception_is_sanitized(error: Exception) -> None:
    class BadMapping(dict[str, object]):
        def items(self):
            raise error

    with pytest.raises(WireProjectionError) as caught:
        project_result(BadMapping())
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(), asyncio.CancelledError()])
def test_projection_base_exception_propagates(error: BaseException) -> None:
    class BadMapping(dict[str, object]):
        def items(self):
            raise error

    with pytest.raises(type(error)) as caught:
        project_result(BadMapping())
    assert caught.value is error


@pytest.mark.parametrize(
    "method, params, expected",
    [
        (
            "runtime.session.open",
            {"session": {"project_id": "p", "thread_id": "t"}},
            "OpenSessionCommand",
        ),
        (
            "runtime.turn.submit",
            {"session": {"project_id": "p", "thread_id": "t"}, "text": "x"},
            "SubmitTurnCommand",
        ),
        (
            "runtime.turn.cancel",
            {"session": {"project_id": "p", "thread_id": "t"}, "expected_turn_id": "t"},
            "CancelTurnCommand",
        ),
        (
            "runtime.turn.steer",
            {"session": {"project_id": "p", "thread_id": "t"}, "expected_turn_id": "t", "text": "x"},
            "SteerTurnCommand",
        ),
        (
            "runtime.session.close",
            {"session": {"project_id": "p", "thread_id": "t"}},
            "CloseSessionCommand",
        ),
        (
            "runtime.session.get",
            {"session": {"project_id": "p", "thread_id": "t"}},
            "GetSessionQuery",
        ),
        (
            "runtime.events.read",
            {"session": {"project_id": "p", "thread_id": "t"}},
            "ReadEventsQuery",
        ),
        (
            "runtime.events.watch",
            {"session": {"project_id": "p", "thread_id": "t"}},
            "WatchSpec",
        ),
        ("runtime.events.unwatch", {"subscription_id": "s"}, "str"),
        (
            "runtime.artifacts.stat",
            {"ref": {"session": {"project_id": "p", "thread_id": "t"}, "path": "a"}},
            "StatArtifactQuery",
        ),
        (
            "runtime.artifacts.list",
            {"session": {"project_id": "p", "thread_id": "t"}},
            "ListArtifactsQuery",
        ),
        (
            "runtime.artifacts.read",
            {"ref": {"session": {"project_id": "p", "thread_id": "t"}, "path": "a"}},
            "ReadArtifactQuery",
        ),
    ],
)
def test_protocol_all_methods_decode(method: str, params: dict[str, object], expected: str) -> None:
    assert type(decode_params(method, params)).__name__ == expected


class _DispatchSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __getattr__(self, name: str):
        async def invoke(dto: object) -> object:
            self.calls.append((name, dto))
            return {"ok": True}

        return invoke


def test_protocol_dispatch_and_service_error_mapping() -> None:
    async def run() -> None:
        spy = _DispatchSpy()
        from synapse.runtime.transport.protocol import dispatch, service_error

        assert await dispatch(spy, "runtime.session.get", {"session": {"project_id": "p", "thread_id": "t"}}) == {"ok": True}
        assert spy.calls[0][0] == "get_session"
        with pytest.raises(ProtocolError) as caught:
            await dispatch(spy, "unknown", {})
        assert caught.value.service_code == "method_not_found"
        error = RuntimeServiceError("secret", code="safe_code")
        assert service_error(error) == (-32000, "runtime service error", "safe_code")
        assert "secret" not in encode_error(1, -32000, "safe_code")

    asyncio.run(run())
