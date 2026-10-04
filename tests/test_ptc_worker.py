"""Real-subprocess tests for the one-shot PTC worker.

Each test spawns the actual ``worker.py`` through :func:`run_code`, so these
exercise the NDJSON protocol, tool dispatch, log capture, limits and teardown
end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import synapse
import synapse.runtime.ptc.protocol as protocol_module
from synapse.runtime.ptc import process
from synapse.runtime.ptc import worker as worker_module
from synapse.runtime.ptc.process import run_code
from synapse.runtime.ptc.protocol import PtcLimits


async def _no_tools(name: str, arguments: dict[str, Any]) -> Any:
    raise AssertionError(f"dispatch must not be called: {name}")


async def _echo(name: str, arguments: dict[str, Any]) -> Any:
    return arguments["value"]


def _encode(result: Any) -> bytes:
    """Serialise a result exactly like the host/middleware combined cap does."""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def run(
    code: str,
    *,
    dispatch: Any = None,
    tool_names: tuple[str, ...] = (),
    limits: PtcLimits | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    async def _invoke() -> dict[str, Any]:
        return await run_code(
            code=code,
            tool_names=list(tool_names),
            dispatch=dispatch or _no_tools,
            cwd=cwd or Path.cwd(),
            limits=limits or PtcLimits(timeout_seconds=30.0),
        )

    return asyncio.run(_invoke())


def test_unicode_value_and_logs(tmp_path: Path) -> None:
    text = "h\u00e9llo \u4e16\u754c \U0001f680"
    code = f"print({text!r})\nreturn {text!r}"
    result = run(code, cwd=tmp_path)
    assert result.get("error") is None
    assert result["value"] == text
    assert any(text in line for line in result["logs"])


def test_await_gather_and_logs(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        assert name == "echo"
        return arguments["value"]

    code = (
        "print('start')\n"
        "results = await asyncio.gather(tools.echo(value=1), tools.echo(value=2))\n"
        "return results"
    )
    result = run(code, dispatch=dispatch, tool_names=("echo",), cwd=tmp_path)
    assert result.get("error") is None
    assert result["value"] == [1, 2]
    assert "start" in "\n".join(result["logs"])


def test_tools_call_with_mapping(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        return arguments["value"]

    code = "return await tools.call('echo', {'value': 7})"
    result = run(code, dispatch=dispatch, tool_names=("echo",), cwd=tmp_path)
    assert result["value"] == 7


def test_tool_error_is_catchable(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        raise ValueError("boom")

    code = (
        "try:\n"
        "    await tools.fail()\n"
        "except ToolCallError as exc:\n"
        "    return exc.message\n"
        "return 'not raised'"
    )
    result = run(code, dispatch=dispatch, tool_names=("fail",), cwd=tmp_path)
    assert result.get("error") is None
    assert "boom" in str(result["value"])


def test_unknown_tool_name(tmp_path: Path) -> None:
    code = (
        "try:\n"
        "    await tools.nope()\n"
        "except ToolCallError as exc:\n"
        "    return exc.message\n"
        "return 'not raised'"
    )
    result = run(code, tool_names=("echo",), cwd=tmp_path)
    assert result.get("error") is None
    assert "nope" in str(result["value"])


def test_tools_call_non_mapping_arguments(tmp_path: Path) -> None:
    code = (
        "try:\n"
        "    await tools.call('echo', [1, 2])\n"
        "except ToolCallError as exc:\n"
        "    return exc.message\n"
        "return 'not raised'"
    )
    result = run(code, tool_names=("echo",), cwd=tmp_path)
    assert result.get("error") is None
    assert "mapping" in str(result["value"])


def test_logging_is_captured(tmp_path: Path) -> None:
    code = "import logging\nlogging.warning('ptc-log-line')\nreturn 'ok'"
    result = run(code, cwd=tmp_path)
    assert result["value"] == "ok"
    assert any("ptc-log-line" in line for line in result["logs"])


def test_syntax_error(tmp_path: Path) -> None:
    result = run("return (", cwd=tmp_path)
    assert result["value"] is None
    assert result["error"]["kind"] == "syntax_error"


def test_script_exception(tmp_path: Path) -> None:
    result = run("raise RuntimeError('kaboom')", cwd=tmp_path)
    assert result["error"]["kind"] == "exception"
    assert "kaboom" in result["error"]["message"]


def test_infinite_loop_timeout(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=2.0)
    code = "while True:\n    await asyncio.sleep(0.02)"
    result = run(code, limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "timeout"


def test_idle_await_timeout(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=2.0)
    result = run("await asyncio.Event().wait()", limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "timeout"


def test_output_cap(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=30.0, max_output_bytes=2048)
    code = "for _ in range(5000):\n    print('y' * 40)\nreturn 'done'"
    result = run(code, limits=limits, cwd=tmp_path)
    # ``max_output_bytes`` is the *combined* cap on ``{logs, value, error?}``:
    # a run that overflows it is an explicit ``output_limit`` error, never a
    # silent success with a truncated log.
    assert result["error"]["kind"] == "output_limit"
    assert result["value"] is None
    encoded = _encode(result)
    assert len(encoded) <= limits.max_output_bytes
    assert result["logs"]


def test_too_large_tool_result(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=30.0, max_result_bytes=2048)

    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        return "x" * 5000

    code = (
        "try:\n"
        "    return await tools.big()\n"
        "except ToolCallError as exc:\n"
        "    return ['caught', exc.message]"
    )
    result = run(code, dispatch=dispatch, tool_names=("big",), limits=limits, cwd=tmp_path)
    assert result.get("error") is None
    assert result["value"][0] == "caught"
    assert "max_result_bytes" in result["value"][1]


def test_result_too_large(tmp_path: Path) -> None:
    limits = PtcLimits(max_result_bytes=2048)
    result = run("return 'z' * 5000", limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "result_too_large"


def test_non_serializable_result(tmp_path: Path) -> None:
    result = run("return {1, 2, 3}", cwd=tmp_path)
    assert result["error"]["kind"] == "invalid_result"


def test_nan_result_rejected(tmp_path: Path) -> None:
    result = run("return float('nan')", cwd=tmp_path)
    assert result["error"]["kind"] == "invalid_result"


def test_non_finite_tool_result_becomes_tool_error(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        return float("inf")

    code = (
        "try:\n"
        "    return await tools.bad()\n"
        "except ToolCallError as exc:\n"
        "    return exc.message\n"
        "return 'not raised'"
    )
    result = run(code, dispatch=dispatch, tool_names=("bad",), cwd=tmp_path)
    assert result.get("error") is None
    assert "JSON-serializable" in str(result["value"])


def test_max_calls_budget(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        return 1

    limits = PtcLimits(timeout_seconds=30.0, max_calls=2)
    code = "total = 0\nfor _ in range(5):\n    total += await tools.inc()\nreturn total"
    result = run(code, dispatch=dispatch, tool_names=("inc",), limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "max_calls_exceeded"


def test_cancellation_cancels_pending_call(tmp_path: Path) -> None:
    async def scenario() -> bool:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"

        task = asyncio.ensure_future(
            run_code(
                code="return await tools.slow()",
                tool_names=["slow"],
                dispatch=dispatch,
                cwd=tmp_path,
                limits=PtcLimits(timeout_seconds=30.0),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return cancelled.is_set()

    assert asyncio.run(scenario()) is True


def test_fresh_state_between_runs(tmp_path: Path) -> None:
    first = run(
        "import sys\nsys._ptc_marker = 1\nreturn getattr(sys, '_ptc_marker', 0)",
        cwd=tmp_path,
    )
    assert first["value"] == 1
    second = run("import sys\nreturn getattr(sys, '_ptc_marker', 0)", cwd=tmp_path)
    assert second["value"] == 0


def test_scrubbed_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTC_TEST_SECRET", "do-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
    code = (
        "import os\n"
        "return {\n"
        "    'secret': os.environ.get('PTC_TEST_SECRET'),\n"
        "    'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "    'has_path': bool(os.environ.get('PATH')),\n"
        "}"
    )
    result = run(code, cwd=tmp_path)
    assert result["value"]["secret"] is None
    assert result["value"]["pythonpath"] is None
    assert result["value"]["has_path"] is True


# --------------------------------------------------------------------------- #
# max_calls budget counts every tool_call frame (unknown names included)
# --------------------------------------------------------------------------- #


def test_unknown_tool_calls_count_against_budget(tmp_path: Path) -> None:
    """Spamming distinct *unknown* names must still trip ``max_calls``."""
    limits = PtcLimits(timeout_seconds=30.0, max_calls=3)
    code = (
        "for i in range(50):\n"
        "    try:\n"
        "        await tools.call(f'ghost_{i}')\n"
        "    except ToolCallError:\n"
        "        pass\n"
        "return 'finished'\n"
    )
    result = run(code, tool_names=(), limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "max_calls_exceeded"


def test_unknown_tool_calls_are_catchable_within_budget(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=30.0, max_calls=5)
    code = (
        "seen = []\n"
        "for i in range(3):\n"
        "    try:\n"
        "        await tools.call(f'ghost_{i}')\n"
        "    except ToolCallError as exc:\n"
        "        seen.append(exc.kind)\n"
        "return seen\n"
    )
    result = run(code, tool_names=(), limits=limits, cwd=tmp_path)
    assert result.get("error") is None
    assert result["value"] == ["unknown", "unknown", "unknown"]


# --------------------------------------------------------------------------- #
# combined max_output_bytes cap (logs + value + error)
# --------------------------------------------------------------------------- #


def test_combined_log_and_value_over_cap(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=30.0, max_output_bytes=1024)
    code = "print('log-' + 'x' * 400)\nreturn 'v' * 900"
    result = run(code, limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "output_limit"
    assert result["value"] is None
    assert len(_encode(result)) <= limits.max_output_bytes


def test_large_value_alone_over_cap_is_dropped(tmp_path: Path) -> None:
    # The value fits ``max_result_bytes`` but not the combined outer cap: it must
    # not leak into the model context as a "successful" result.
    limits = PtcLimits(timeout_seconds=30.0, max_output_bytes=1024)
    result = run("return 'z' * 4000", limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "output_limit"
    assert result["value"] is None
    assert len(_encode(result)) <= limits.max_output_bytes


def test_multibyte_output_counts_utf8_bytes(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=30.0, max_output_bytes=1024)
    # 400 three-byte characters = 1200 UTF-8 bytes, over the combined cap.
    code = "return '\u4e16' * 400"
    result = run(code, limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "output_limit"
    assert len(_encode(result)) <= limits.max_output_bytes


def test_control_char_escaping_counts_toward_cap(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=30.0, max_output_bytes=512)
    # Each control char escapes to ``\u0001`` (6 bytes), so 200 of them blow the
    # 512-byte cap even though the raw string is only 200 characters.
    code = "return '\\x01' * 200"
    result = run(code, limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "output_limit"
    assert len(_encode(result)) <= limits.max_output_bytes


def test_tiny_output_cap_still_bounded(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=30.0, max_output_bytes=256)
    result = run("print('a' * 400)\nreturn 'b' * 400", limits=limits, cwd=tmp_path)
    assert result["error"]["kind"] == "output_limit"
    assert len(_encode(result)) <= limits.max_output_bytes


# --------------------------------------------------------------------------- #
# ToolCallError kind / name end to end
# --------------------------------------------------------------------------- #


class _BridgeLikeToolError(Exception):
    """Stand-in for ``bridge.ToolCallError`` (same ``kind``/``name`` contract)."""

    def __init__(self, message: str, *, kind: str = "tool_error", name: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.name = name
        self.message = message


def test_tool_call_error_kind_and_name_end_to_end(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        raise _BridgeLikeToolError("boom", kind="denied", name="secret_tool")

    code = (
        "try:\n"
        "    await tools.secret_tool()\n"
        "except ToolCallError as exc:\n"
        "    return {\n"
        "        'kind': exc.kind,\n"
        "        'name': exc.name,\n"
        "        'tool_name': exc.tool_name,\n"
        "        'message': exc.message,\n"
        "    }\n"
        "return 'not raised'"
    )
    result = run(code, dispatch=dispatch, tool_names=("secret_tool",), cwd=tmp_path)
    assert result.get("error") is None
    assert result["value"] == {
        "kind": "denied",
        "name": "secret_tool",
        "tool_name": "secret_tool",
        "message": "boom",
    }


def test_plain_exception_becomes_tool_error_kind(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        raise ValueError("plain failure")

    code = (
        "try:\n"
        "    await tools.boom()\n"
        "except ToolCallError as exc:\n"
        "    return {'kind': exc.kind, 'name': exc.name, 'message': exc.message}\n"
        "return 'not raised'"
    )
    result = run(code, dispatch=dispatch, tool_names=("boom",), cwd=tmp_path)
    assert result["value"]["kind"] == "tool_error"
    assert result["value"]["name"] == "boom"
    assert "plain failure" in result["value"]["message"]


# --------------------------------------------------------------------------- #
# returning with tool calls still in flight
# --------------------------------------------------------------------------- #


def test_early_return_with_inflight_call_is_unfinished(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        await asyncio.sleep(3600)
        return "never"

    code = (
        "import asyncio\n"
        "task = asyncio.create_task(tools.slow())\n"
        "await asyncio.sleep(0.2)\n"
        "return 'done'\n"
    )
    result = run(
        code,
        dispatch=dispatch,
        tool_names=("slow",),
        limits=PtcLimits(timeout_seconds=30.0),
        cwd=tmp_path,
    )
    assert result["error"]["kind"] == "unfinished_calls"
    assert result["value"] is None


def test_unstarted_create_task_is_unfinished(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:  # pragma: no cover
        return "never"

    code = (
        "import asyncio\n"
        "asyncio.create_task(tools.slow())\n"
        "return 'done'\n"
    )
    result = run(
        code,
        dispatch=dispatch,
        tool_names=("slow",),
        limits=PtcLimits(timeout_seconds=30.0),
        cwd=tmp_path,
    )
    assert result["error"]["kind"] == "unfinished_calls"


def test_awaited_calls_do_not_report_unfinished(tmp_path: Path) -> None:
    async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        return arguments["value"]

    code = (
        "import asyncio\n"
        "a = await tools.echo(value=1)\n"
        "b = await tools.echo(value=2)\n"
        "return a + b\n"
    )
    result = run(code, dispatch=dispatch, tool_names=("echo",), cwd=tmp_path)
    assert result.get("error") is None
    assert result["value"] == 3


# --------------------------------------------------------------------------- #
# timeout / hard kill
# --------------------------------------------------------------------------- #


def test_busy_loop_timeout_hardkills_worker(tmp_path: Path) -> None:
    limits = PtcLimits(timeout_seconds=1.0)
    started = time.monotonic()
    result = run("while True:\n    pass", limits=limits, cwd=tmp_path)
    elapsed = time.monotonic() - started
    assert result["error"]["kind"] == "timeout"
    # A killed worker cannot keep the host waiting past the timeout + cleanup.
    assert elapsed < 15.0


# --------------------------------------------------------------------------- #
# forged result frame must not fake success while a tool is in flight
# --------------------------------------------------------------------------- #


def test_forged_success_frame_with_inflight_call_is_unfinished(tmp_path: Path) -> None:
    """A raw ``result`` frame cannot fake success while a tool is in flight.

    Model code can write straight to fd 1, bypassing the worker's own
    settlement guard, so the host must reject the false success and report the
    still-running call as ``unfinished_calls`` (cancelling it in the process).
    """
    cancelled = asyncio.Event()

    async def scenario() -> dict[str, Any]:
        async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"

        code = (
            "import os\n"
            "import asyncio\n"
            "async def _call():\n"
            "    return await tools.slow()\n"
            "asyncio.create_task(_call())\n"
            "await asyncio.sleep(0.3)\n"
            "os.write(1, b'{\"type\":\"result\",\"value\":\"injected-ok\"}\\n')\n"
            "return 'normal'\n"
        )
        return await run_code(
            code=code,
            tool_names=["slow"],
            dispatch=dispatch,
            cwd=tmp_path,
            limits=PtcLimits(timeout_seconds=30.0),
        )

    result = asyncio.run(scenario())
    assert result["error"]["kind"] == "unfinished_calls"
    assert result["value"] is None
    assert cancelled.is_set()


def test_awaited_calls_are_not_mistaken_for_unfinished(tmp_path: Path) -> None:
    """A completed awaited call is never reported as an in-flight one."""
    code = (
        "import asyncio\n"
        "results = await asyncio.gather(tools.echo(value=1), tools.echo(value=2))\n"
        "return results\n"
    )
    result = run(code, dispatch=_echo, tool_names=("echo",), cwd=tmp_path)
    assert result.get("error") is None
    assert result["value"] == [1, 2]


# --------------------------------------------------------------------------- #
# spawned grandchildren are reaped with the worker
# --------------------------------------------------------------------------- #


def _pid_running(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie nobody has reaped yet is not really still running.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            state = handle.read().split(") ", 1)[1].split(" ", 1)[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def _wait_pid_gone(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return True
        time.sleep(0.1)
    return not _pid_running(pid)


def _spawn_grandchild(pid_file: Path, *, hard_exit: bool) -> str:
    tail = "os._exit(0)\n" if hard_exit else "return 'spawned'\n"
    return (
        "import os, subprocess, sys\n"
        "proc = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(proc.pid))\n"
        f"{tail}"
    )


def test_normal_return_reaps_spawned_grandchild(tmp_path: Path) -> None:
    """A run that leaves a background process behind still cleans it up."""
    pid_file = tmp_path / "grandchild.pid"
    result = run(
        _spawn_grandchild(pid_file, hard_exit=False),
        cwd=tmp_path,
        limits=PtcLimits(timeout_seconds=30.0),
    )
    assert result.get("error") is None
    assert result["value"] == "spawned"
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert _wait_pid_gone(pid), f"spawned grandchild {pid} survived the run"


@pytest.mark.skipif(
    os.name == "nt",
    reason="taskkill cannot reach children of an exited parent on Windows",
)
def test_hard_exit_reaps_spawned_grandchild(tmp_path: Path) -> None:
    """``os._exit`` bypasses the worker's normal path; the tree still dies.

    The worker exits without a result frame and the background process keeps the
    protocol pipe open, so the run ends on the timeout -- the host must still
    kill the whole process group.
    """
    pid_file = tmp_path / "grandchild.pid"
    result = run(
        _spawn_grandchild(pid_file, hard_exit=True),
        cwd=tmp_path,
        limits=PtcLimits(timeout_seconds=2.0),
    )
    assert result["error"]["kind"] == "timeout"
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert _wait_pid_gone(pid), f"spawned grandchild {pid} survived the run"


# --------------------------------------------------------------------------- #
# packaged (frozen) worker route and protocol loading
# --------------------------------------------------------------------------- #


def test_frozen_worker_route_runs_real_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packaged ``--synapse-ptc-worker`` route drives the real worker.

    A frozen build has no ``worker.py`` on disk; routing ``_worker_command``
    through ``python -m synapse.entry --synapse-ptc-worker`` exercises the same
    entry point the packaged executable uses.
    """
    src = Path(synapse.__file__).resolve().parent.parent
    monkeypatch.setattr(
        process,
        "_worker_command",
        lambda: [sys.executable, "-m", "synapse.entry", "--synapse-ptc-worker"],
    )
    env = process._build_env()
    env["PYTHONPATH"] = str(src)
    monkeypatch.setattr(process, "_build_env", lambda: dict(env))

    result = run(
        "a = await tools.echo(value=5)\nreturn a + 1",
        dispatch=_echo,
        tool_names=("echo",),
        cwd=tmp_path,
    )
    assert result.get("error") is None
    assert result["value"] == 6


def test_load_protocol_uses_package_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(worker_module.sys, "frozen", raising=False)
    monkeypatch.setattr(worker_module, "__package__", "synapse.runtime.ptc")
    monkeypatch.setattr(
        worker_module,
        "_load_sibling_protocol",
        lambda: pytest.fail("package import must not read the sibling file"),
    )
    assert worker_module._load_protocol() is protocol_module


def test_load_protocol_sibling_fallback_without_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(worker_module.sys, "frozen", raising=False)
    monkeypatch.setattr(worker_module, "__package__", "")
    try:
        module = worker_module._load_protocol()
    finally:
        sys.modules.pop("_synapse_ptc_protocol", None)
    assert module is not protocol_module
    assert module.FRAME_RESULT == "result"


def test_frozen_worker_does_not_need_protocol_source_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The packaged route imports ``protocol`` from the bundle, not a ``.py``."""
    monkeypatch.setattr(worker_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        worker_module,
        "_load_sibling_protocol",
        lambda: pytest.fail("a frozen build has no sibling protocol.py"),
    )
    assert worker_module._load_protocol() is protocol_module
