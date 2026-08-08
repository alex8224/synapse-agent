from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from synapse.ui.chrome.controller import ChromeController


class _App:
    def __init__(self) -> None:
        self._codex = SimpleNamespace(
            refresh_usage=lambda: None,
            fetch_reset_credits=lambda: None,
            consume_reset=lambda _credit_id: SimpleNamespace(outcome="reset"),
            loading=False,
            consuming=False,
            reset_credits=None,
        )
        self.calls: list[str] = []
        self._ui_thread = True

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        if self._ui_thread:
            raise RuntimeError("must run in a different thread from the app")
        callback(*args, **kwargs)


def test_fetch_codex_usage_bg_falls_back_inline_on_ui_thread() -> None:
    app = _App()
    controller = ChromeController(app)
    app._on_codex_usage_ready = lambda: app.calls.append("ready")

    # Regression: on_mount runs on the UI thread; the usage fetch must not
    # raise RuntimeError from call_from_thread.
    controller.fetch_codex_usage_bg()

    assert app.calls == ["ready"]


def test_reload_tool_output_stats_schedules_worker_without_reading_inline() -> None:
    app = _App()
    app.thread_id = "thread-1"
    app.is_running = True
    app._tool_output_refresh_pending = False
    app._tool_output_refresh_dirty = False
    app._tool_output_repo = SimpleNamespace(
        chrome_stats=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("inline read"))
    )
    scheduled: list[str] = []
    app._refresh_tool_output_stats_bg = scheduled.append

    ChromeController(app).reload_tool_output_stats()

    assert scheduled == ["thread-1"]
    assert app._tool_output_refresh_pending is True


def test_apply_tool_output_stats_reloads_once_when_dirty() -> None:
    app = _App()
    app.thread_id = "thread-1"
    app.is_running = True
    app._tool_output_refresh_pending = True
    app._tool_output_refresh_dirty = True
    app._tool_output_stats = {}
    app._tool_output_stats_thread_id = None
    app._refresh_topbar = lambda: app.calls.append("topbar")
    scheduled: list[str] = []
    app._refresh_tool_output_stats_bg = scheduled.append
    controller = ChromeController(app)

    controller.apply_tool_output_stats("thread-1", {"transformed": 1})

    assert app._tool_output_stats == {"transformed": 1}
    assert app.calls == ["topbar"]
    assert scheduled == ["thread-1"]


def test_metrics_changed_marks_dirty_when_refresh_pending() -> None:
    """F4: pending refresh coalesces later signals into one follow-up refresh."""
    app = _App()
    app.thread_id = "thread-1"
    app.is_running = True
    app._tool_output_refresh_pending = True
    app._tool_output_refresh_dirty = False
    scheduled: list[str] = []
    app._refresh_tool_output_stats_bg = scheduled.append
    controller = ChromeController(app)

    controller.on_tool_output_metrics_changed("thread-1")
    controller.on_tool_output_metrics_changed("thread-1")

    assert app._tool_output_refresh_dirty is True
    assert scheduled == []  # no duplicate worker while a refresh is pending


def test_metrics_changed_schedules_refresh_when_idle() -> None:
    """F4: no pending refresh -> exactly one worker is scheduled."""
    app = _App()
    app.thread_id = "thread-1"
    app.is_running = True
    app._tool_output_refresh_pending = False
    app._tool_output_refresh_dirty = False
    scheduled: list[str] = []
    app._refresh_tool_output_stats_bg = scheduled.append
    controller = ChromeController(app)

    controller.on_tool_output_metrics_changed("thread-1")

    assert app._tool_output_refresh_pending is True
    assert scheduled == ["thread-1"]


def test_metrics_changed_falls_back_inline_on_ui_thread() -> None:
    """F4: on the UI thread call_from_thread raises; handler runs inline."""
    app = _App()
    app.thread_id = "thread-1"
    app.is_running = True
    app._tool_output_refresh_pending = False
    app._tool_output_refresh_dirty = False
    scheduled: list[str] = []
    app._refresh_tool_output_stats_bg = scheduled.append
    controller = ChromeController(app)

    controller.on_tool_output_metrics_changed("thread-1")

    assert app._tool_output_refresh_pending is True
    assert scheduled == ["thread-1"]


def test_metrics_changed_ignores_other_thread() -> None:
    """F4: a stale session's signal must not touch the current chrome."""
    app = _App()
    app.thread_id = "thread-1"
    app.is_running = True
    app._tool_output_refresh_pending = False
    app._tool_output_refresh_dirty = False
    scheduled: list[str] = []
    app._refresh_tool_output_stats_bg = scheduled.append
    controller = ChromeController(app)

    controller.on_tool_output_metrics_changed("old-thread")

    assert app._tool_output_refresh_pending is False
    assert scheduled == []


def test_apply_tool_output_stats_discards_stale_thread() -> None:
    """F4: results from an old session never overwrite the current chrome."""
    app = _App()
    app.thread_id = "thread-1"
    app.is_running = True
    app._tool_output_refresh_pending = True
    app._tool_output_refresh_dirty = False
    app._tool_output_stats = {"transformed": 0}
    app._tool_output_stats_thread_id = "thread-1"
    scheduled: list[str] = []
    app._refresh_tool_output_stats_bg = scheduled.append
    controller = ChromeController(app)

    controller.apply_tool_output_stats("old-thread", {"transformed": 999})

    assert app._tool_output_stats == {"transformed": 0}
    assert app._tool_output_stats_thread_id == "thread-1"
    # A refresh for the current session was scheduled instead.
    assert scheduled == ["thread-1"]


def test_fetch_codex_reset_credits_bg_falls_back_inline_on_ui_thread() -> None:
    app = _App()
    controller = ChromeController(app)
    app._open_codex_reset_dialog = lambda: app.calls.append("open-dialog")

    controller.fetch_codex_reset_credits_for_dialog_bg()

    assert app.calls == ["open-dialog"]


def test_consume_codex_reset_bg_falls_back_inline_on_ui_thread() -> None:
    app = _App()
    controller = ChromeController(app)
    app._on_codex_reset_consumed = lambda result: app.calls.append(result.outcome)
    app._on_codex_reset_consume_done = lambda: app.calls.append("done")

    controller.consume_codex_reset_bg("credit-1")

    assert app.calls == ["reset"]


def test_fetch_codex_usage_bg_uses_call_from_thread_off_ui_thread() -> None:
    app = _App()
    app._ui_thread = False
    controller = ChromeController(app)
    app._on_codex_usage_ready = lambda: app.calls.append("ready")

    controller.fetch_codex_usage_bg()

    assert app.calls == ["ready"]
