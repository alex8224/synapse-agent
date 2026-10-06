"""Host-side tests for the one-shot PTC runtime (``process.py``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from synapse.runtime.ptc import process
from synapse.runtime.ptc.protocol import PtcLimits


async def _unused_dispatch(name: str, arguments: dict[str, Any]) -> Any:
    raise AssertionError(f"dispatch must not be called: {name}")


def test_limits_defaults() -> None:
    limits = PtcLimits()
    assert limits.timeout_seconds == 120.0
    assert limits.max_calls == 100
    assert limits.max_parallel == 8
    assert limits.max_output_bytes == 64000
    assert limits.max_result_bytes == 4000000
    assert limits.max_code_bytes == 64000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": 10_000.0},
        {"max_calls": 0},
        {"max_calls": 1.5},
        {"max_calls": True},
        {"max_calls": 1_000_000},
        {"max_parallel": 0},
        {"max_parallel": 9999},
        {"max_output_bytes": 0},
        {"max_result_bytes": 10},
        {"max_code_bytes": 10},
    ],
)
def test_limits_reject_invalid(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PtcLimits(**kwargs)


def test_worker_command_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    command = process._worker_command()
    assert command[:2] == [sys.executable, "-I"]
    assert command[-1].endswith("worker.py")


def test_worker_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert process._worker_command() == [sys.executable, "--synapse-ptc-worker"]


def test_build_env_drops_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTC_TEST_SECRET", "do-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
    env = process._build_env()
    assert "PTC_TEST_SECRET" not in env
    assert "PYTHONPATH" not in env
    assert env["PYTHONUTF8"] == "1"


def test_code_too_large(tmp_path: Path) -> None:
    result = asyncio.run(
        process.run_code(
            code="x" * 2000,
            tool_names=[],
            dispatch=_unused_dispatch,
            cwd=tmp_path,
            limits=PtcLimits(max_code_bytes=1024),
        )
    )
    assert result["error"]["kind"] == "code_too_large"
    assert result["value"] is None


def test_malformed_frame_fails_host_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake_worker.py"
    fake.write_text(
        "import sys, time\n"
        "sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'not-json\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        process, "_worker_command", lambda: [sys.executable, "-u", str(fake)]
    )
    result = asyncio.run(
        process.run_code(
            code="return 1",
            tool_names=[],
            dispatch=_unused_dispatch,
            cwd=tmp_path,
            limits=PtcLimits(timeout_seconds=15.0),
        )
    )
    assert result["error"]["kind"] == "protocol_error"


def test_duplicate_call_id_fails_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "fake_worker.py"
    frame = b'{"type":"tool_call","id":"c1","name":"x","arguments":{}}\n'
    fake.write_text(
        "import sys, time\n"
        "sys.stdin.buffer.readline()\n"
        f"sys.stdout.buffer.write({frame!r})\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.stdout.buffer.write({frame!r})\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        process, "_worker_command", lambda: [sys.executable, "-u", str(fake)]
    )
    result = asyncio.run(
        process.run_code(
            code="return 1",
            tool_names=[],
            dispatch=_unused_dispatch,
            cwd=tmp_path,
            limits=PtcLimits(timeout_seconds=15.0),
        )
    )
    assert result["error"]["kind"] == "protocol_error"
    assert "duplicate" in result["error"]["message"]


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- #
# combined max_output_bytes cap
# --------------------------------------------------------------------------- #


def test_limits_allow_small_combined_output_cap() -> None:
    assert PtcLimits(max_output_bytes=256).max_output_bytes == 256
    with pytest.raises(ValueError):
        PtcLimits(max_output_bytes=255)


def test_finalize_output_keeps_small_result() -> None:
    result = {"logs": ["hi"], "value": "ok"}
    assert process._finalize_output(result, 256) == result


def test_finalize_output_enforces_combined_cap() -> None:
    result = {"logs": ["a" * 200], "value": "b" * 200}
    out = process._finalize_output(result, 256)
    assert out["error"]["kind"] == "output_limit"
    assert out["value"] is None
    assert len(_encode(out)) <= 256


def test_finalize_output_preserves_error_kind() -> None:
    result = {
        "logs": ["line" * 50],
        "value": None,
        "error": {"kind": "exception", "message": "x" * 500},
    }
    out = process._finalize_output(result, 256)
    assert out["error"]["kind"] == "exception"
    assert len(_encode(out)) <= 256


def test_finalize_output_bounds_control_char_message() -> None:
    result = {
        "logs": [],
        "value": None,
        "error": {"kind": "exception", "message": "\x01" * 500},
    }
    out = process._finalize_output(result, 256)
    assert out["error"]["kind"] == "exception"
    assert len(_encode(out)) <= 256


# --------------------------------------------------------------------------- #
# exception -> tool-error frame contract
# --------------------------------------------------------------------------- #


def test_exception_fields_extract_kind_and_name() -> None:
    class Boom(Exception):
        def __init__(self) -> None:
            super().__init__("nope")
            self.kind = "unknown"
            self.name = "ghost"
            self.message = "not registered"

    assert process._exception_fields(Boom()) == {
        "kind": "unknown",
        "name": "ghost",
        "message": "not registered",
    }


def test_exception_fields_accept_tool_name_alias() -> None:
    class Boom(Exception):
        def __init__(self) -> None:
            super().__init__("boom")
            self.tool_name = "aliased"

    fields = process._exception_fields(Boom())
    assert fields["kind"] == "tool_error"
    assert fields["name"] == "aliased"
    assert "boom" in fields["message"]


def test_exception_fields_default_for_plain_exception() -> None:
    fields = process._exception_fields(ValueError("boom"))
    assert fields["kind"] == "tool_error"
    assert fields["name"] == ""
    assert "boom" in fields["message"]


# --------------------------------------------------------------------------- #
# budget counting (no subprocess)
# --------------------------------------------------------------------------- #


def _session(tmp_path: Path, **limit_kwargs: Any) -> Any:
    return process._Session(
        code="",
        tool_names=["echo"],
        dispatch=_unused_dispatch,
        cwd=tmp_path,
        limits=PtcLimits(**limit_kwargs),
    )


def test_unknown_tool_calls_trip_budget_and_bound_seen(tmp_path: Path) -> None:
    session = _session(tmp_path, max_calls=2)
    async def feed() -> None:
        for index in range(10):
            frame = {
                "type": "tool_call",
                "id": f"c{index}",
                "name": f"ghost{index}",
                "arguments": {},
            }
            if await session._handle_tool_call(frame):
                return
        pytest.fail("budget never tripped")

    asyncio.run(feed())
    assert session._failure is not None
    assert session._failure[0] == "max_calls_exceeded"
    assert len(session._seen_ids) <= 2
def test_duplicate_ids_are_rejected_before_budget(tmp_path: Path) -> None:
    session = _session(tmp_path, max_calls=5)
    frame = {"type": "tool_call", "id": "c1", "name": "ghost", "arguments": {}}
    async def feed() -> bool:
        first = await session._handle_tool_call(frame)
        second = await session._handle_tool_call(frame)
        return first or second
    assert asyncio.run(feed()) is True
    assert session._failure is not None
    assert session._failure[0] == "protocol_error"
    assert "duplicate" in session._failure[1]


# --------------------------------------------------------------------------- #
# settlement guard: a forged success frame must not hide an in-flight dispatch
# --------------------------------------------------------------------------- #


def _register_inflight(session: Any, call_id: str) -> asyncio.Task[Any]:
    """Attach a never-completing task to the session as an in-flight dispatch."""
    task = asyncio.ensure_future(asyncio.sleep(3600))
    session._tasks.add(task)
    session._task_calls[task] = call_id
    return task


def test_success_result_with_unsettled_dispatch_is_unfinished(tmp_path: Path) -> None:
    session = _session(tmp_path)
    async def scenario() -> bool:
        task = _register_inflight(session, "c1")
        handled = session._handle_result({"type": "result", "value": "injected-ok"})
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return handled
    assert asyncio.run(scenario()) is True
    assert session._failure is not None
    assert session._failure[0] == "unfinished_calls"
    assembled = session._assemble()
    assert assembled["value"] is None
    assert assembled["error"]["kind"] == "unfinished_calls"
def test_success_result_with_settled_dispatch_is_accepted(tmp_path: Path) -> None:
    session = _session(tmp_path)
    async def scenario() -> bool:
        task = _register_inflight(session, "c1")
        # The tool returned and its response is only draining: not unfinished.
        session._settle("c1")
        handled = session._handle_result({"type": "result", "value": "ok"})
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return handled
    assert asyncio.run(scenario()) is True
    assert session._failure is None
    assert session._assemble() == {"logs": [], "value": "ok"}
def test_error_result_wins_over_inflight_dispatch(tmp_path: Path) -> None:
    session = _session(tmp_path)
    async def scenario() -> bool:
        task = _register_inflight(session, "c1")
        handled = session._handle_result(
            {
                "type": "result",
                "value": None,
                "error": {"kind": "exception", "message": "boom"},
            }
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return handled
    assert asyncio.run(scenario()) is True
    assert session._failure is None
    assert session._assemble()["error"]["kind"] == "exception"
def test_frame_budget_covers_combined_and_result_caps(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        max_output_bytes=256,
        max_result_bytes=1024,
        max_code_bytes=512,
    )
    # The largest frame the worker can emit is the result frame carrying a value
    # up to ``max_result_bytes``; the combined ``max_output_bytes`` cap must also
    # fit inside the read limit.
    assert session._max_frame_bytes() == 1024 + process._FRAME_SLACK_BYTES
    assert session._max_frame_bytes() >= 256


# --------------------------------------------------------------------------- #
# available_names init-frame field
# --------------------------------------------------------------------------- #


def _capture_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_names: list[str],
    available_names: list[str] | None,
) -> dict[str, Any]:
    """Build an init frame for a session without spawning a worker."""
    captured: dict[str, Any] = {}

    async def capture(self: Any, frame: dict[str, Any]) -> None:
        captured.update(frame)

    monkeypatch.setattr(process._Session, "_send_frame", capture)
    session = process._Session(
        code="return 1",
        tool_names=tool_names,
        available_names=available_names,
        dispatch=_unused_dispatch,
        cwd=tmp_path,
        limits=PtcLimits(),
    )
    asyncio.run(session._send_init())
    return captured


def test_send_init_carries_available_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = _capture_init(
        tmp_path,
        monkeypatch,
        tool_names=["echo"],
        available_names=["echo", "read", "write"],
    )
    assert frame["tool_names"] == ["echo"]
    assert frame["available_names"] == ["echo", "read", "write"]


def test_send_init_available_names_falls_back_to_tool_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = _capture_init(
        tmp_path,
        monkeypatch,
        tool_names=["echo", "read"],
        available_names=None,
    )
    assert frame["tool_names"] == ["echo", "read"]
    assert frame["available_names"] == ["echo", "read"]


def test_run_code_available_names_end_to_end(tmp_path: Path) -> None:
    result = asyncio.run(
        process.run_code(
            code="return list(tools.available)",
            tool_names=["echo"],
            available_names=["alpha", "beta"],
            dispatch=_unused_dispatch,
            cwd=tmp_path,
            limits=PtcLimits(timeout_seconds=30.0),
        )
    )
    assert result.get("error") is None
    assert result["value"] == ["alpha", "beta"]


def test_run_code_available_names_defaults_to_tool_names(tmp_path: Path) -> None:
    result = asyncio.run(
        process.run_code(
            code="return list(tools.available)",
            tool_names=["echo", "read"],
            dispatch=_unused_dispatch,
            cwd=tmp_path,
            limits=PtcLimits(timeout_seconds=30.0),
        )
    )
    assert result.get("error") is None
    assert result["value"] == ["echo", "read"]
