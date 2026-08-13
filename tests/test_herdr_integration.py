"""Tests for the optional herdr lifecycle-state integration."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.integrations import herdr


def _snapshot(status: str, last_error: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=SimpleNamespace(value=status), last_error=last_error)


def test_herdr_disabled_without_env(monkeypatch) -> None:
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)

    assert herdr.herdr_enabled() is False
    assert herdr.attach_status_observer(None) is None


def test_herdr_enabled_with_env(monkeypatch) -> None:
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)

    assert herdr.herdr_enabled() is True


def test_herdr_enabled_requires_pane_id(monkeypatch) -> None:
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    assert herdr.herdr_enabled() is False


def test_resolve_bin_path_prefers_env(monkeypatch) -> None:
    monkeypatch.setenv("HERDR_BIN_PATH", "/opt/herdr")

    assert herdr._resolve_bin_path() == "/opt/herdr"


def test_resolve_bin_path_falls_back_to_which(monkeypatch) -> None:
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
    monkeypatch.setattr(
        herdr.shutil, "which", lambda name: "/found/herdr" if name == "herdr" else None
    )

    assert herdr._resolve_bin_path() == "/found/herdr"


def test_resolve_bin_path_falls_back_to_bare(monkeypatch) -> None:
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
    monkeypatch.setattr(herdr.shutil, "which", lambda name: None)

    assert herdr._resolve_bin_path() == "herdr"


def test_map_status_to_state() -> None:
    assert herdr.map_status_to_state("queued") == "working"
    assert herdr.map_status_to_state("starting") == "working"
    assert herdr.map_status_to_state("running") == "working"
    assert herdr.map_status_to_state("cancelling") == "working"
    assert herdr.map_status_to_state("waiting_approval") == "blocked"
    assert herdr.map_status_to_state("failed") == "blocked"
    assert herdr.map_status_to_state("idle") == "idle"
    assert herdr.map_status_to_state("cold") == "idle"
    assert herdr.map_status_to_state("cancelled") == "idle"
    assert herdr.map_status_to_state("closed") is None
    assert herdr.map_status_to_state("unknown") is None


def test_build_report_args_with_message() -> None:
    args = herdr.build_report_args(
        bin_path="/h",
        pane_id="w1:p1",
        source="custom:synapse",
        agent="synapse",
        state="blocked",
        seq=7,
        message="needs approval",
    )
    assert args == (
        "/h",
        "pane",
        "report-agent",
        "w1:p1",
        "--source",
        "custom:synapse",
        "--agent",
        "synapse",
        "--state",
        "blocked",
        "--seq",
        "7",
        "--message",
        "needs approval",
    )


def test_build_report_args_without_message() -> None:
    args = herdr.build_report_args(
        bin_path="/h",
        pane_id="w1:p1",
        source="custom:synapse",
        agent="synapse",
        state="working",
        seq=1,
    )
    assert "--message" not in args
    assert args[-2:] == ("--seq", "1")


def test_build_release_args() -> None:
    args = herdr.build_release_args(
        bin_path="/h",
        pane_id="w1:p1",
        source="custom:synapse",
        agent="synapse",
    )
    assert args == (
        "/h",
        "pane",
        "release-agent",
        "w1:p1",
        "--source",
        "custom:synapse",
        "--agent",
        "synapse",
    )


def test_attach_observer_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(herdr, "herdr_enabled", lambda: False)
    callback = lambda snapshot: None  # noqa: E731
    assert herdr.attach_status_observer(callback) is callback


def test_attach_observer_composes_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(herdr, "herdr_enabled", lambda: True)

    reported: list[tuple[str, str | None]] = []

    class FakeReporter:
        def report(self, state: str, message: str | None = None) -> None:
            reported.append((state, message))

    monkeypatch.setattr(herdr, "_get_reporter", lambda: FakeReporter())

    user_seen: list[object] = []
    observer = herdr.attach_status_observer(user_seen.append)
    assert observer is not None

    snapshot = _snapshot("waiting_approval")
    observer(snapshot)  # type: ignore[misc]

    assert reported == [("blocked", None)]
    assert user_seen == [snapshot]


def test_attach_observer_reports_failure_message(monkeypatch) -> None:
    monkeypatch.setattr(herdr, "herdr_enabled", lambda: True)

    reported: list[tuple[str, str | None]] = []

    class FakeReporter:
        def report(self, state: str, message: str | None = None) -> None:
            reported.append((state, message))

    monkeypatch.setattr(herdr, "_get_reporter", lambda: FakeReporter())

    observer = herdr.attach_status_observer(None)
    assert observer is not None
    observer(_snapshot("failed", last_error="boom"))  # type: ignore[misc]

    assert reported == [("blocked", "boom")]


def test_reporter_queues_and_runs_command(monkeypatch) -> None:
    executed: list[list[str]] = []

    def fake_run(args, **kwargs) -> None:  # noqa: ANN001
        executed.append(list(args))

    monkeypatch.setattr(herdr.subprocess, "run", fake_run)

    reporter = herdr.HerdrReporter(pane_id="w1:p1", bin_path="/h")
    try:
        reporter.report("working")
        reporter.flush()

        assert executed
        assert executed[0][:4] == ["/h", "pane", "report-agent", "w1:p1"]
        assert "--state" in executed[0]
        assert "working" in executed[0]
    finally:
        reporter.close()


def test_reporter_close_is_idempotent(monkeypatch) -> None:
    executed: list[list[str]] = []

    def fake_run(args, **kwargs) -> None:  # noqa: ANN001
        executed.append(list(args))

    monkeypatch.setattr(herdr.subprocess, "run", fake_run)

    reporter = herdr.HerdrReporter(pane_id="w1:p1", bin_path="/h")
    reporter.close()
    count_after_first = len(executed)
    reporter.close()
    assert len(executed) == count_after_first


def test_first_reporter_announces_idle(monkeypatch) -> None:
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_BIN_PATH", "/h")

    executed: list[list[str]] = []

    def fake_run(args, **kwargs) -> None:  # noqa: ANN001
        executed.append(list(args))

    monkeypatch.setattr(herdr.subprocess, "run", fake_run)

    original = herdr._reporter
    herdr._reporter = None
    reporter: herdr.HerdrReporter | None = None
    try:
        reporter = herdr._get_reporter()
        assert reporter is not None
        reporter.flush()
        assert any("--state" in args and "idle" in args for args in executed)
    finally:
        if reporter is not None:
            reporter.close()
        herdr._reporter = original
