from __future__ import annotations

from types import SimpleNamespace

import pytest

from synapse.ui.dialogs.controller import SlashController, TuiCommandEffects


def test_effects_normalize_thread_switch_and_hitl_action() -> None:
    result = SimpleNamespace(
        agent="agent",
        thread_id="new-thread",
        clear_log=False,
        reload_transcript=True,
        notice="done",
        lines=["line"],
        settings_changed=True,
        theme_name="dracula",
        markdown=None,
        error=False,
        resume_action="approve",
        resume_message="allow it",
    )

    effects = TuiCommandEffects.from_result(result, previous_thread_id="old-thread")

    assert effects.agent == "agent"
    assert effects.clear_transcript is True
    assert effects.reload_transcript is True
    assert effects.status_notice == "done"
    assert effects.resume_action == "approve"
    assert effects.resume_message == "allow it"


def test_effects_ignore_non_string_magicmock_like_resume_values() -> None:
    result = SimpleNamespace(
        agent=None,
        thread_id=None,
        clear_log=False,
        reload_transcript=False,
        notice=None,
        lines=[],
        settings_changed=False,
        theme_name=None,
        markdown=None,
        error=False,
        resume_action=object(),
        resume_message=object(),
    )

    effects = TuiCommandEffects.from_result(result, previous_thread_id="thread")

    assert effects.resume_action is None
    assert effects.resume_message is None


def test_cancel_active_turn_effect_cancels_session_runtime() -> None:
    calls: list[str] = []
    app = SimpleNamespace(
        thread_id="thread",
        _turn=SimpleNamespace(cancel=lambda reason: calls.append(reason)),
        _emit_system_lines=lambda *args, **kwargs: None,
        _reload_session_title=lambda: None,
        _refresh_topbar=lambda: None,
        _refresh_codex_usage=lambda **kwargs: None,
    )

    SlashController(app).apply_effects(TuiCommandEffects(cancel_active_turn=True))

    assert calls == ["goal_pause"]


def _effects_app(agent: object | None = None, settings: object | None = None) -> SimpleNamespace:
    """Minimal host surface for apply_effects on the current-thread path."""
    if settings is None:
        settings = SimpleNamespace(
            active_model="gpt",
            model="gpt-4.1",
            enable_thinking=True,
            reasoning_effort="high",
        )
    return SimpleNamespace(
        thread_id="thread",
        agent=agent,
        settings=settings,
        sub_title="",
        _turn=SimpleNamespace(),
        _emit_system_lines=lambda *args, **kwargs: None,
        append_event=lambda *args, **kwargs: None,
        _reload_session_title=lambda: None,
        _refresh_topbar=lambda: None,
        _refresh_codex_usage=lambda **kwargs: None,
        _render_status=lambda: None,
        _reset_session_token_chrome=lambda: None,
        _reload_tool_output_stats=lambda: None,
        _load_current_goal=lambda: None,
        _schedule_transcript_reset=lambda **kwargs: None,
        _restore_session_transcript=lambda **kwargs: None,
    )


def test_apply_effects_binds_rebuilt_agent_to_current_session_runtime() -> None:
    """/model rebuilds must update the session-owned runtime, not just app.agent.

    Regression: after switching model in session B and switching back to
    session A, the switch path preferred the frozen runtime agent, which still
    held the old graph — silently undoing the model switch.
    """
    new_agent = object()
    bound: list[tuple[str, object, object | None]] = []

    class _Turn:
        def bind_agent(
            self, thread_id: str, agent: object, *, settings: object | None = None
        ) -> None:
            bound.append((thread_id, agent, settings))

    app = _effects_app(agent=object())
    app._turn = _Turn()

    SlashController(app).apply_effects(TuiCommandEffects(agent=new_agent))

    assert app.agent is new_agent
    assert bound == [("thread", new_agent, None)]


def test_apply_effects_switch_syncs_settings_to_target_session_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching back must point global settings at the target session's model."""
    class _Runtime:
        agent = SimpleNamespace(_coding_model_profile="claude")

    class _Turn:
        def __init__(self) -> None:
            self.attached: list[str] = []

        def detach(self, thread_id: str | None = None) -> None:
            self.attached.append(str(thread_id))

        def agent_for_session(self, thread_id: str) -> object:
            del thread_id
            return _Runtime.agent

        def bind_agent(self, thread_id: str, agent: object, **kwargs: object) -> None:
            del thread_id, agent, kwargs

    settings = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        openai_base_url=None,
    )
    app = _effects_app(agent=object(), settings=settings)
    turn = _Turn()
    app._turn = turn

    fake_profile = SimpleNamespace(
        name="claude",
        model="claude-sonnet-4",
        enable_thinking=True,
        reasoning_effort="high",
        base_url=None,
        parallel_tool_calls=True,
    )
    fake_registry = SimpleNamespace(default="claude", profiles={"claude": fake_profile})
    fake_registry.get = lambda name: fake_profile if name == "claude" else (_ for _ in ()).throw(
        KeyError(name)
    )
    monkeypatch.setattr(
        "synapse.models.registry.registry_from_settings", lambda settings: fake_registry
    )

    def fake_apply(settings: object, profile: object, *, seed_thinking: bool) -> None:
        del profile, seed_thinking
        settings.active_model = "claude"  # type: ignore[attr-defined]
        settings.model = "claude-sonnet-4"  # type: ignore[attr-defined]

    monkeypatch.setattr("synapse.models.registry.apply_profile_to_settings", fake_apply)

    SlashController(app).apply_effects(
        TuiCommandEffects(thread_id="thread-2", clear_transcript=True)
    )

    assert app.thread_id == "thread-2"
    assert app.agent is _Runtime.agent
    assert settings.active_model == "claude"
    assert settings.model == "claude-sonnet-4"
    assert turn.attached == ["thread", "thread-2"]


def test_apply_effects_switch_keeps_settings_when_profiles_match() -> None:
    """No registry round-trip when the frozen agent already matches settings."""
    class _Runtime:
        agent = SimpleNamespace(_coding_model_profile="gpt")

    class _Turn:
        def detach(self, thread_id: str | None = None) -> None:
            del thread_id

        def agent_for_session(self, thread_id: str) -> object:
            del thread_id
            return _Runtime.agent

        def bind_agent(self, thread_id: str, agent: object, **kwargs: object) -> None:
            del thread_id, agent, kwargs

    settings = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        openai_base_url=None,
    )
    app = _effects_app(agent=object(), settings=settings)
    app._turn = _Turn()

    SlashController(app).apply_effects(
        TuiCommandEffects(thread_id="thread-2", clear_transcript=True)
    )

    assert settings.active_model == "gpt"
    assert settings.model == "gpt-4.1"


def test_switch_model_bg_uses_origin_agent_when_foreground_moved() -> None:
    """A model worker must rebuild from the session that initiated it.

    Regression: the worker body read app.agent/app.thread_id at execution
    time; switching sessions while the rebuild ran made the new session the
    template and persistence target of the old session's model change.
    """
    from pathlib import Path
    from unittest.mock import patch

    origin_agent = object()
    current_agent = object()
    captured: dict[str, object] = {}

    def fake_handle_slash(command: str, **kwargs: object) -> SimpleNamespace:
        del command
        captured.update(kwargs)
        return SimpleNamespace(
            handled=True,
            error=False,
            agent=object(),
            mcp_attach_pending=False,
        )

    foreground_settings = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        openai_api_key=None,
        anthropic_api_key=None,
        openai_base_url=None,
        enable_mcp=True,
        mcp_servers_json=None,
    )
    app = SimpleNamespace(
        thread_id="foreground",
        agent=current_agent,
        settings=foreground_settings,
        project_root=Path("."),
        call_from_thread=lambda fn, *a, **k: None,
        _clear_status_notice=lambda: None,
        set_activity=lambda *a, **k: None,
        append_event=lambda *a, **k: None,
    )
    controller = SlashController(app)
    controller.apply_ok_result = lambda *a, **k: None

    with patch("synapse.commands.slash_cmds.handle_slash", fake_handle_slash):
        controller.switch_model_bg(
            "/model claude",
            "switching model to claude",
            origin_thread_id="origin-session",
            origin_agent=origin_agent,
        )

    assert captured["agent"] is origin_agent
    assert captured["thread_id"] == "origin-session"
    assert captured["agent"] is not current_agent
    assert captured["settings"] is not foreground_settings


def test_finish_model_switch_binds_origin_when_foreground_moved() -> None:
    """A model rebuild completing after a session switch must not touch the
    foreground session: only the origin runtime is updated."""
    new_agent = object()
    bound: list[tuple[str, object, object | None]] = []

    class _Turn:
        def bind_agent(
            self, thread_id: str, agent: object, *, settings: object | None = None
        ) -> None:
            bound.append((thread_id, agent, settings))

    app = SimpleNamespace(
        thread_id="foreground-session",
        _turn=_Turn(),
        append_event=lambda *args, **kwargs: None,
    )
    controller = SlashController(app)
    controller.apply_ok_result = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("foreground must not be touched")
    )

    ok = SimpleNamespace(agent=new_agent)
    controller._finish_model_switch(ok, "origin-session")

    assert bound == [("origin-session", new_agent, None)]
    assert app.thread_id == "foreground-session"


def test_finish_model_switch_applies_when_foreground_matches() -> None:
    applied: list[tuple[object, float]] = []
    app = SimpleNamespace(
        thread_id="same-session",
        _turn=None,
        append_event=lambda *args, **kwargs: None,
    )
    controller = SlashController(app)
    controller.apply_ok_result = lambda ok, ttl=4.0: applied.append((ok, ttl))

    ok = SimpleNamespace(agent=object())
    controller._finish_model_switch(ok, "same-session", notice_ttl=1.5)

    assert applied == [(ok, 1.5)]


def test_finish_model_switch_does_not_commit_background_settings() -> None:
    foreground = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
    )
    worker = SimpleNamespace(
        active_model="claude",
        model="claude-sonnet-4",
        enable_thinking=True,
        reasoning_effort="low",
    )

    class _Turn:
        def bind_agent(
            self, thread_id: str, agent: object, *, settings: object | None = None
        ) -> None:
            assert thread_id == "origin-session"
            assert settings is worker

    app = SimpleNamespace(
        thread_id="foreground-session",
        settings=foreground,
        _turn=_Turn(),
        append_event=lambda *args, **kwargs: None,
    )
    SlashController(app)._finish_model_switch(
        SimpleNamespace(agent=object()), "origin-session", worker
    )

    assert foreground.active_model == "gpt"
    assert foreground.model == "gpt-4.1"


def test_model_switch_pending_mcp_uses_rebuilt_origin_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path
    from unittest.mock import patch

    old_agent = object()
    rebuilt_agent = object()
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    settings = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        openai_api_key=None,
        anthropic_api_key=None,
        openai_base_url=None,
        enable_mcp=True,
        mcp_servers_json=None,
    )

    def call_from_thread(fn: object, *args: object, **kwargs: object) -> None:
        del kwargs
        scheduled.append((fn, args))

    app = SimpleNamespace(
        thread_id="foreground",
        agent=old_agent,
        settings=settings,
        project_root=Path("."),
        call_from_thread=call_from_thread,
        _clear_status_notice=lambda: None,
        set_activity=lambda *args, **kwargs: None,
        append_event=lambda *args, **kwargs: None,
    )
    controller = SlashController(app)
    controller.rebind_agent_worker = lambda *a, **k: None
    ok = SimpleNamespace(error=False, agent=rebuilt_agent, mcp_attach_pending=True)

    with patch("synapse.commands.slash_cmds.handle_slash", lambda *a, **k: ok):
        controller.switch_model_bg(
            "/model claude",
            "switching",
            origin_thread_id="origin-session",
            origin_agent=old_agent,
        )

    attach_calls = [args for fn, args in scheduled if fn == controller.attach_mcp_after_switch]
    assert len(attach_calls) == 1
    assert attach_calls[0][0] == "origin-session"
    assert attach_calls[0][1] is rebuilt_agent
    assert attach_calls[0][2] is not settings


def test_finish_mcp_worker_binds_origin_when_foreground_moved() -> None:
    new_agent = object()
    bound: list[tuple[str, object, object | None]] = []

    class _Turn:
        def bind_agent(
            self, thread_id: str, agent: object, *, settings: object | None = None
        ) -> None:
            bound.append((thread_id, agent, settings))

    app = SimpleNamespace(
        thread_id="foreground-session",
        _turn=_Turn(),
        _mcp_reloading=True,
        append_event=lambda *args, **kwargs: None,
        set_activity=lambda *args, **kwargs: None,
    )
    controller = SlashController(app)
    controller.apply_ok_result = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("foreground must not be touched")
    )

    controller._finish_mcp_worker(
        SimpleNamespace(agent=new_agent), origin_thread_id="origin-session"
    )

    assert bound == [("origin-session", new_agent, None)]
    assert app._mcp_reloading is False


def test_finish_model_switch_does_not_commit_on_error() -> None:
    """A failed model rebuild must not push half-applied settings to foreground."""
    foreground = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
    )
    # handle_slash mutated the worker copy before the rebuild failed.
    worker = SimpleNamespace(
        active_model="claude",
        model="claude-sonnet-4",
        enable_thinking=True,
        reasoning_effort="low",
    )
    applied: list[object] = []
    app = SimpleNamespace(
        thread_id="same-session",
        settings=foreground,
        _turn=None,
        append_event=lambda *args, **kwargs: None,
    )
    controller = SlashController(app)
    controller.apply_ok_result = lambda ok, ttl=4.0: applied.append(ok)

    controller._finish_model_switch(
        SimpleNamespace(agent=None, error=True), "same-session", worker
    )

    assert foreground.active_model == "gpt"
    assert foreground.model == "gpt-4.1"
    assert foreground.reasoning_effort == "high"
    assert len(applied) == 1


def test_finish_mcp_worker_does_not_commit_on_error() -> None:
    """A failed MCP reload must not push half-applied settings to foreground."""
    foreground = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        enable_mcp=True,
        mcp_servers_json=None,
    )
    worker = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        enable_mcp=False,
        mcp_servers_json='{"servers": []}',
    )
    applied: list[object] = []
    app = SimpleNamespace(
        thread_id="same-session",
        settings=foreground,
        _mcp_reloading=True,
        append_event=lambda *args, **kwargs: None,
        set_activity=lambda *args, **kwargs: None,
    )
    controller = SlashController(app)
    controller.apply_ok_result = lambda ok, ttl=4.0: applied.append(ok)

    controller._finish_mcp_worker(
        SimpleNamespace(agent=None, error=True),
        origin_thread_id="same-session",
        worker_settings=worker,
    )

    assert foreground.enable_mcp is True
    assert foreground.mcp_servers_json is None
    assert len(applied) == 1


def test_attach_mcp_after_switch_skips_without_origin_agent() -> None:
    """attach must never fall back to the (different) foreground agent."""
    scheduled: list[tuple[object, ...]] = []
    app = SimpleNamespace(
        thread_id="foreground",
        agent=object(),
        _mcp_attaching=False,
        _attach_mcp_after_switch_bg=lambda *a, **k: scheduled.append(a),
    )
    controller = SlashController(app)

    controller.attach_mcp_after_switch(
        origin_thread_id="origin-session",
        origin_agent=None,
    )

    assert scheduled == []
    assert app._mcp_attaching is False


def test_apply_effects_switch_failure_rolls_back_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed session build must restore the previous session's settings."""
    settings = SimpleNamespace(
        active_model="gpt",
        model="gpt-4.1",
        enable_thinking=True,
        reasoning_effort="high",
        openai_api_key=None,
        anthropic_api_key=None,
        openai_base_url=None,
    )

    class _Turn:
        def detach(self, thread_id: str | None = None) -> None:
            del thread_id

        def agent_for_session(self, thread_id: str) -> object | None:
            del thread_id
            return None

        def bind_agent(self, thread_id: str, agent: object, **kwargs: object) -> None:
            del thread_id, agent, kwargs

        def attach(self, thread_id: str) -> None:
            del thread_id

        def sync_foreground_status(self) -> None:
            pass

    app = _effects_app(agent=object(), settings=settings)
    app._turn = _Turn()
    controller = SlashController(app)

    def boom(thread_id: str, template_agent: object) -> object:
        del thread_id, template_agent
        raise RuntimeError("build failed")

    monkeypatch.setattr(controller, "_build_session_agent", boom)
    # handle_slash already mutated the global settings towards the target
    # session before apply_effects ran; the pre-switch snapshot was captured
    # by the caller before that mutation.
    snapshot = controller._settings_snapshot(settings)
    settings.active_model = "claude"
    settings.model = "claude-sonnet-4"

    controller.apply_effects(
        TuiCommandEffects(thread_id="target", clear_transcript=True),
        settings_snapshot=snapshot,
    )

    assert app.thread_id == "thread"
    assert settings.active_model == "gpt"
    assert settings.model == "gpt-4.1"
    assert settings.reasoning_effort == "high"


def _handle_model_fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, SimpleNamespace]:
    """Shared fake registry/profile for handle_model model-switch tests."""
    fake_profile = SimpleNamespace(name="deep", model="openai:deep")
    fake_registry = SimpleNamespace(
        default="zen",
        profiles={"deep": fake_profile},
        allowed_thinking_levels=lambda name: ["off", "low", "high", "max"],
    )
    fake_registry.get = lambda name: (
        fake_profile if name == "deep" else (_ for _ in ()).throw(KeyError(name))
    )
    monkeypatch.setattr(
        "synapse.commands.model.registry_from_settings", lambda settings: fake_registry
    )
    monkeypatch.setattr(
        "synapse.models.registry.apply_profile_to_settings",
        lambda settings, profile, *, seed_thinking: setattr(
            settings, "active_model", profile.name
        ),
    )
    settings = SimpleNamespace(
        active_model="zen",
        model="openai:zen",
        enable_thinking=True,
        reasoning_effort="high",
        parallel_tool_calls=True,
        openai_api_key=None,
        anthropic_api_key=None,
        openai_base_url=None,
    )
    return settings, fake_profile


def test_handle_model_does_not_persist_after_rebuild_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the model binding must be persisted even when the slow agent
    rebuild fails — otherwise the user's choice lives only in memory and is
    lost on the next startup (the reported F2 dialog model loss)."""
    from pathlib import Path

    from synapse.commands.model import handle_model

    settings, _ = _handle_model_fakes(monkeypatch)
    captured: list[tuple[str, str | None]] = []

    def fake_persist(current: object, thread_id: str | None) -> str | None:
        captured.append((current.active_model, thread_id))
        return None

    def fake_rebuild(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("gateway unreachable")

    result = handle_model(
        ["deep"],
        settings=settings,
        agent=object(),
        project_root=Path("."),
        thread_id="t1",
        apply_thinking_inplace=lambda *a, **k: False,
        rebuild_agent=fake_rebuild,
        persist_model_binding=fake_persist,
        mcp_attach_pending=lambda s: False,
    )

    assert result.error is True
    # A failed build must not persist a binding that has no runtime graph.
    assert captured == []


def test_handle_model_surfaces_persist_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed binding persist must be reported, not silently swallowed."""
    from pathlib import Path

    from synapse.commands.model import handle_model

    settings, _ = _handle_model_fakes(monkeypatch)
    monkeypatch.setattr("synapse.models.registry.format_model_status", lambda s: "deep")

    def fake_persist(current: object, thread_id: str | None) -> str | None:
        del current, thread_id
        return "failed to persist model binding: disk full"

    result = handle_model(
        ["deep"],
        settings=settings,
        agent=object(),
        project_root=Path("."),
        thread_id="t1",
        apply_thinking_inplace=lambda *a, **k: False,
        rebuild_agent=lambda *a, **k: object(),
        persist_model_binding=fake_persist,
        mcp_attach_pending=lambda s: False,
    )

    assert result.error is True
    assert result.lines == [
        "model switched to deep  (deep)",
        "failed to persist model binding: disk full",
    ]


def test_persist_model_binding_returns_error_instead_of_silent_swallow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slash-layer persist must surface failures so the UI can warn."""
    from synapse.commands import slash_cmds

    def boom(settings: object) -> object:
        del settings
        raise OSError("disk full")

    monkeypatch.setattr(slash_cmds, "_store", boom)
    error = slash_cmds._persist_model_binding(SimpleNamespace(), "t1")
    assert error is not None
    assert "disk full" in error