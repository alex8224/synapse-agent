from __future__ import annotations

from types import SimpleNamespace

from synapse.ui.dialogs.controller import TuiCommandEffects


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
