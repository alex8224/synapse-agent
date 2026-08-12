"""Tests for the process entry point (startup trace before CLI import)."""

from __future__ import annotations

import io

import synapse.entry as entry
from synapse.observability.startup_trace import StartupTrace


def test_entry_main_delegates_to_cli(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("synapse.cli.main", lambda: calls.append("cli"))
    entry.main()
    assert calls == ["cli"]


def test_entry_records_stages_on_trace(monkeypatch) -> None:
    """With an enabled trace, entry:start and cli:imported are recorded."""
    trace = StartupTrace(enabled=True)
    trace.ensure_started()
    monkeypatch.setattr(
        "synapse.observability.startup_trace._current_trace", lambda: trace
    )
    monkeypatch.setattr("synapse.cli.main", lambda: None)
    entry.main()
    names = [m[0] for m in trace.marks]
    assert "entry:start" in names
    assert "cli:imported" in names
    assert names.index("entry:start") < names.index("cli:imported")
    out = io.StringIO()
    trace.dump(file=out)
    assert "entry:start" in out.getvalue()
    assert "cli:imported" in out.getvalue()
