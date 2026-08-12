from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time

import synapse.observability.startup_trace as startup_trace
from synapse.observability.startup_trace import StartupTrace


def test_disabled_trace_is_silent() -> None:
    trace = StartupTrace(enabled=False)
    out = io.StringIO()
    trace.mark("stage")
    with trace.span("work"):
        pass
    started = time.perf_counter()
    trace.duration("event", started, file=out)
    trace.dump(file=out)
    assert out.getvalue() == ""
    assert trace.marks == []


def test_mark_accumulates_monotonic_stages() -> None:
    trace = StartupTrace(enabled=True)
    out = io.StringIO()
    time.sleep(0.001)
    trace.mark("stage-1")
    time.sleep(0.001)
    trace.mark("stage-2")
    assert [m[0] for m in trace.marks] == ["stage-1", "stage-2"]
    # ``since`` is relative to trace start and must be monotonic.
    assert trace.marks[1][1] >= trace.marks[0][1]
    assert trace.marks[1][2] > 0  # step between the two marks
    trace.dump(file=out)
    text = out.getvalue()
    assert "stage-1" in text
    assert "stage-2" in text
    assert "total=" in text


def test_span_records_stage_duration() -> None:
    trace = StartupTrace(enabled=True)
    with trace.span("work"):
        time.sleep(0.005)
    assert len(trace.marks) == 1
    name, _since, step = trace.marks[0]
    assert name == "work"
    assert step >= 5.0  # ~5ms sleep must show up as the step cost


def test_duration_emits_one_shot_line() -> None:
    trace = StartupTrace(enabled=True)
    out = io.StringIO()
    started = time.perf_counter()
    trace.duration("agent.ready", started, phase="startup", file=out)
    text = out.getvalue()
    assert "event=agent.ready" in text
    assert "elapsed_ms=" in text
    assert "phase=startup" in text
    # One-shot durations must not accumulate into the stage report.
    assert trace.marks == []


def test_dump_is_incremental() -> None:
    trace = StartupTrace(enabled=True)
    trace.mark("stage-a")
    first = io.StringIO()
    trace.dump(file=first)
    assert "stage-a" in first.getvalue()
    # Nothing new -> second dump stays silent.
    second = io.StringIO()
    trace.dump(file=second)
    assert second.getvalue() == ""
    # Later stages are reported without repeating earlier ones.
    trace.mark("stage-b")
    third = io.StringIO()
    trace.dump(file=third)
    text = third.getvalue()
    assert "stage-b" in text
    assert "stage-a" not in text


def test_dump_force_repeats_reported_stages() -> None:
    trace = StartupTrace(enabled=True)
    trace.mark("stage-a")
    first = io.StringIO()
    trace.dump(file=first)
    assert "stage-a" in first.getvalue()
    forced = io.StringIO()
    trace.dump(file=forced, force=True)
    assert "stage-a" in forced.getvalue()


def test_worker_thread_trace_is_registered() -> None:
    startup_trace.reset()
    seen: list[StartupTrace] = []

    def work() -> None:
        startup_trace.mark("worker-stage")
        with startup_trace._THREAD_TRACES_LOCK:
            seen.extend(startup_trace._THREAD_TRACES)

    thread = threading.Thread(target=work, name="build-worker")
    thread.start()
    thread.join()
    assert any(
        trace.thread_name == "build-worker" and trace.marks
        for trace in seen
    )


def test_global_mark_uses_process_trace_from_worker(monkeypatch) -> None:
    """Cross-thread milestones share the main process-relative clock."""
    monkeypatch.setattr(startup_trace.TRACE, "enabled", True)
    startup_trace.TRACE.reset()

    def work() -> None:
        startup_trace.global_mark("worker-global-stage")

    thread = threading.Thread(target=work, name="global-worker")
    thread.start()
    thread.join()

    assert [name for name, _since, _step in startup_trace.TRACE.marks] == [
        "trace-enabled",
        "worker-global-stage",
    ]


def test_reset_clears_marks_and_dump_state() -> None:
    trace = StartupTrace(enabled=True)
    trace.mark("a")
    trace.reset()
    out = io.StringIO()
    trace.dump(file=out)
    assert out.getvalue() == ""
    assert trace.marks == []
    assert trace._dumped == 0


def test_ensure_started_seeds_t0_only_when_enabled_and_empty() -> None:
    trace = StartupTrace(enabled=True)
    trace.ensure_started()
    assert [m[0] for m in trace.marks] == ["trace-enabled"]
    # Already-started traces must not be reset again.
    trace.mark("a")
    trace.ensure_started()
    assert [m[0] for m in trace.marks] == ["trace-enabled", "a"]

    disabled = StartupTrace(enabled=False)
    disabled.ensure_started()
    assert disabled.marks == []


def test_atexit_dump_is_safe_when_empty() -> None:
    # The registered handler must never break shutdown on an empty trace.
    startup_trace._atexit_dump()


def test_exit_prints_startup_report_to_stderr() -> None:
    # End-to-end: with AGENT_STARTUP_TRACE enabled, the accumulated startup
    # stages are printed to stderr after the process exits (via atexit).
    code = (
        "import synapse.observability.startup_trace as st\n"
        "st.mark('stage-a')\n"
        "st.mark('stage-b')\n"
    )
    env = {**os.environ, "AGENT_STARTUP_TRACE": "1"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "stage-a" in proc.stderr
    assert "stage-b" in proc.stderr
    assert "startup-trace" in proc.stderr


def test_exit_prints_worker_thread_report_to_stderr() -> None:
    # The deferred-TUI path builds the agent on a worker thread; its stages
    # must still land on stderr after exit (thread traces are merged at exit).
    code = (
        "import threading\n"
        "import synapse.observability.startup_trace as st\n"
        "st.mark('main-stage')\n"
        "def work():\n"
        "    st.mark('worker-stage')\n"
        "t = threading.Thread(target=work, name='build-worker')\n"
        "t.start()\n"
        "t.join()\n"
    )
    env = {**os.environ, "AGENT_STARTUP_TRACE": "1"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "main-stage" in proc.stderr
    assert "worker-stage" in proc.stderr
    assert "startup-trace:build-worker" in proc.stderr