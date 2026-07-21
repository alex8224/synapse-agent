"""Tests for iterations A/B/C helpers: compact, HITL, skills, subagents, safety."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.context_compact import (
    extract_summarization_event,
    format_summarization_event,
)
from synapse.hitl import (
    PendingAction,
    PendingInterrupt,
    build_decisions,
    format_interrupt_lines,
)
from synapse.safety import (
    build_interrupt_on,
    get_safety_profile,
)
from synapse.skills_catalog import discover_skills, format_skills_lines
from synapse.subagents import build_default_subagents, format_subagents_lines


def test_format_summarization_event():
    assert format_summarization_event(None) is None
    note = format_summarization_event({"cutoff": 12, "file_path": "/conversation_history/t.md"})
    assert note is not None
    assert "compacted" in note
    assert "cutoff=12" in note


def test_extract_summarization_event_from_state_obj():
    state = SimpleNamespace(values={"_summarization_event": {"cutoff": 3}})
    assert extract_summarization_event(state)["cutoff"] == 3


def test_hitl_build_decisions_approve_reject():
    pending = PendingInterrupt(
        actions=[
            PendingAction(name="execute", args={"command": "ls"}),
            PendingAction(name="write_file", args={"path": "a.py"}),
        ]
    )
    assert build_decisions(pending, action="approve") == [
        {"type": "approve"},
        {"type": "approve"},
    ]
    rej = build_decisions(pending, action="reject", message="nope")
    assert rej[0]["type"] == "reject"
    assert rej[0]["message"] == "nope"
    lines = format_interrupt_lines(pending)
    assert any("execute" in ln for ln in lines)
    assert any("/approve" in ln for ln in lines)


def test_hitl_extract_pending_from_state_interrupts():
    from synapse.hitl import extract_pending_interrupt, has_pending_interrupt

    value = {
        "action_requests": [
            {"name": "execute", "args": {"command": "echo hi"}, "description": "run"},
        ],
        "review_configs": [{"allowed_decisions": ["approve", "reject"]}],
    }
    agent = SimpleNamespace(
        get_state=lambda _cfg: SimpleNamespace(
            interrupts=[SimpleNamespace(value=value)],
            tasks=(),
            next=(),
        )
    )
    pending = extract_pending_interrupt(agent, {"configurable": {"thread_id": "t"}})
    assert pending is not None
    assert len(pending.actions) == 1
    assert pending.actions[0].name == "execute"
    assert has_pending_interrupt(agent, {"configurable": {"thread_id": "t"}})


def test_slash_approve_reject_resume_fields():
    from synapse.slash_cmds import handle_slash

    settings = SimpleNamespace()
    ok = handle_slash("/approve", settings=settings, agent=None, thread_id="t1")
    assert ok.handled is True
    assert ok.resume_action == "approve"
    rej = handle_slash(
        "/reject too risky",
        settings=settings,
        agent=None,
        thread_id="t1",
    )
    assert rej.resume_action == "reject"
    assert rej.resume_message == "too risky"


def test_safety_profiles_and_interrupt_on():
    assert build_interrupt_on(require_approval=False) is None
    mapping = build_interrupt_on(require_approval=True)
    assert mapping is not None
    assert mapping["execute"] is True
    assert mapping["write_file"] is True
    assert get_safety_profile("dev-approve").require_approval is True
    assert get_safety_profile("readonly").readonly is True
    assert get_safety_profile("hitl").name == "dev-approve"


def test_subagents_isolation():
    specs = build_default_subagents(isolate_tools=True)
    assert specs is not None
    by_name = {s["name"]: s for s in specs}
    # LocalShell-safe isolation: middleware tool exclusion, not permissions.
    assert "middleware" in by_name["researcher"]
    assert "middleware" in by_name["reviewer"]
    assert "permissions" not in by_name["researcher"]
    assert "permissions" not in by_name["reviewer"]
    assert by_name["tester"].get("tools")
    lines = format_subagents_lines(specs)
    assert any("researcher" in ln for ln in lines)
    assert any("tool-exclude" in ln for ln in lines)


def test_discover_skills(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for tests\n---\n\nBody\n",
        encoding="utf-8",
    )
    found = discover_skills([str(tmp_path / "skills")])
    assert len(found) == 1
    assert found[0].name == "demo-skill"
    lines = format_skills_lines(found)
    assert any("demo-skill" in ln for ln in lines)
