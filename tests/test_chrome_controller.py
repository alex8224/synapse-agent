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


def test_mcp_snapshot_prefers_current_session_agent() -> None:
    """MCP chrome must reflect the active session, not the last global build."""
    app = _App()
    app.thread_id = "thread-1"
    app.agent = SimpleNamespace(_coding_mcp_attached=True)
    app.settings = SimpleNamespace(enable_mcp=True)
    app._turn = SimpleNamespace(
        runtime_for=lambda thread_id: SimpleNamespace(
            agent=SimpleNamespace(_coding_mcp_attached=False)
        )
        if thread_id == "thread-1"
        else None
    )

    enabled, servers, tools, warnings, deferred = ChromeController(app).mcp_snapshot()

    # The frozen session agent never attached MCP -> off, regardless of any
    # global pool or last-build snapshot.
    assert enabled is True
    assert servers == [] and tools == [] and warnings == []
    assert deferred is True


def test_mcp_snapshot_uses_agent_metadata_for_attached_session() -> None:
    """Attached sessions report the tool set they compiled in, not the pool."""
    app = _App()
    app.thread_id = "thread-1"
    app.agent = None
    app.settings = SimpleNamespace(enable_mcp=True)
    app._turn = SimpleNamespace(
        runtime_for=lambda thread_id: SimpleNamespace(
            agent=SimpleNamespace(
                _coding_mcp_attached=True,
                _coding_mcp_servers=["git"],
                _coding_mcp_tool_names=["git_status"],
            )
        )
        if thread_id == "thread-1"
        else None
    )

    enabled, servers, tools, warnings, deferred = ChromeController(app).mcp_snapshot()

    assert enabled is True
    assert servers == ["git"]
    assert tools == ["git_status"]
    assert warnings == []
    assert deferred is False


def test_mcp_snapshot_ignores_newer_pool_when_agent_built_with_older_tools() -> None:
    """A reload in another session must not change this session's MCP label."""
    app = _App()
    app.thread_id = "thread-1"
    app.agent = None
    app.settings = SimpleNamespace(enable_mcp=True)
    app._turn = SimpleNamespace(
        runtime_for=lambda thread_id: SimpleNamespace(
            agent=SimpleNamespace(
                _coding_mcp_attached=True,
                _coding_mcp_servers=["old-server"],
                _coding_mcp_tool_names=["old_tool"],
            )
        )
        if thread_id == "thread-1"
        else None
    )

    enabled, servers, tools, warnings, deferred = ChromeController(app).mcp_snapshot()

    assert enabled is True
    assert servers == ["old-server"]
    assert tools == ["old_tool"]
    assert deferred is False


def test_current_session_model_label_uses_frozen_agent_profile() -> None:
    app = _App()
    app.thread_id = "thread-1"
    app.settings = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        openai_base_url=None,
    )
    fake_profile = SimpleNamespace(
        name="claude", model="claude-sonnet-4", enable_thinking=True, reasoning_effort="high"
    )
    fake_registry = SimpleNamespace(profiles={"claude": fake_profile})
    fake_registry.get = lambda name: fake_profile if name == "claude" else (_ for _ in ()).throw(
        KeyError(name)
    )
    app._turn = SimpleNamespace(
        runtime_for=lambda thread_id: SimpleNamespace(
            agent=SimpleNamespace(
                _coding_model_profile="claude",
                _coding_model_registry=fake_registry,
            )
        )
        if thread_id == "thread-1"
        else None
    )

    label = ChromeController(app).current_session_model_label()

    assert "claude-sonnet-4" in label
    assert "high" in label


def test_current_session_model_label_falls_back_to_settings_without_runtime() -> None:
    app = _App()
    app.thread_id = "thread-1"
    app.agent = None
    app._turn = SimpleNamespace(runtime_for=lambda thread_id: None)
    app.settings = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        openai_base_url=None,
    )

    label = ChromeController(app).current_session_model_label()

    assert "gpt-4.1" in label
    assert "high" in label


def test_refresh_git_chrome_schedules_worker_without_probing_inline() -> None:
    """The git subprocess probe must run off-thread, never inline on submit/turn end."""
    app = _App()
    app._git_chrome_refresh_pending = False
    app._git_chrome_refresh_dirty = False
    scheduled: list[str] = []
    app._refresh_git_chrome_bg = lambda: scheduled.append("probe")
    controller = ChromeController(app)

    controller.refresh_git_chrome()

    assert app._git_chrome_refresh_pending is True
    assert scheduled == ["probe"]


def test_refresh_git_chrome_coalesces_when_pending() -> None:
    """Repeated requests while a probe is in flight coalesce into one follow-up."""
    app = _App()
    app._git_chrome_refresh_pending = True
    app._git_chrome_refresh_dirty = False
    scheduled: list[str] = []
    app._refresh_git_chrome_bg = lambda: scheduled.append("probe")
    controller = ChromeController(app)

    controller.refresh_git_chrome()
    controller.refresh_git_chrome()

    assert app._git_chrome_refresh_dirty is True
    assert scheduled == []


def test_apply_git_chrome_updates_state_and_reloads_when_dirty() -> None:
    """The applied snapshot refreshes the topbar and re-runs when marked dirty."""
    app = _App()
    app._git_chrome_refresh_pending = True
    app._git_chrome_refresh_dirty = True
    app._git_chrome = None
    app._git_branch = None
    app._refresh_topbar = lambda: app.calls.append("topbar")
    bar = SimpleNamespace(
        invalidate_files_cache=lambda: None,
        dismiss=lambda: None,
    )
    app.query_one = lambda selector, widget_cls: bar
    scheduled: list[str] = []
    app._refresh_git_chrome_bg = lambda: scheduled.append("probe")
    controller = ChromeController(app)

    info = SimpleNamespace(name="main", dirty=True)
    controller.apply_git_chrome(info)

    assert app._git_chrome is info
    assert app._git_branch == "main"
    # The dirty flag is consumed; it re-schedules one follow-up probe, which
    # re-arms the pending flag for that next worker.
    assert app._git_chrome_refresh_dirty is False
    assert app.calls == ["topbar"]
    assert scheduled == ["probe"]


def _turn_stats_app(
    *,
    turn: int = 0,
    ttft: float | None = None,
    rate: float | None = None,
    steps: int = 0,
) -> _App:
    app = _App()
    app._last_ttft_s = ttft
    app._output_tokens_per_second = rate
    app._token_rate_estimated = False
    app._last_model_calls = steps
    app._current_turn = turn
    return app


def test_turn_stats_label_empty_without_any_data() -> None:
    app = _turn_stats_app()
    assert ChromeController(app).turn_stats_label() == ""


def test_turn_stats_label_hides_turn_zero() -> None:
    app = _turn_stats_app(ttft=1.2, rate=42.0, steps=5)
    label = ChromeController(app).turn_stats_label()
    assert "回合" not in label
    assert "TTFT 1.2s" in label
    assert "5 steps" in label


def test_turn_stats_label_shows_turn_with_stats() -> None:
    app = _turn_stats_app(turn=3, ttft=1.2, rate=42.0, steps=5)
    label = ChromeController(app).turn_stats_label()
    assert label.startswith("回合 3")
    assert "TTFT 1.2s" in label
    assert "5 steps" in label


def test_turn_stats_label_shows_turn_alone_after_restore() -> None:
    """Restored sessions have no live TTFT/rate yet; turn alone must render."""
    app = _turn_stats_app(turn=7)
    assert ChromeController(app).turn_stats_label() == "回合 7"


def test_turn_stats_label_defaults_missing_turn_field() -> None:
    """Compatibility hosts without _current_turn must not crash the chrome."""
    app = _turn_stats_app(turn=0)
    del app._current_turn
    label = ChromeController(app).turn_stats_label()
    assert label == "" or "回合" not in label


def test_reset_session_token_chrome_resets_turn() -> None:
    """Session switches (incl. /new) reset turn chrome before restore reseeds."""
    app = _turn_stats_app(turn=5)
    app._input_tokens = 100
    app._cache_tokens = 50
    app._output_tokens = 60
    app._context_tokens = 20
    app._last_out_tokens = 30
    app._last_model_calls = 4
    app._usage_base_input = 100
    app._usage_base_output = 60
    app._usage_base_cache = 50

    ChromeController(app).reset_session_token_chrome()

    assert app._current_turn == 0
    assert app._last_model_calls == 0
    assert app._input_tokens == 0
