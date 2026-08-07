from __future__ import annotations

from types import SimpleNamespace

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