"""Tests for slash command completion."""

from __future__ import annotations

from synapse.slash_complete import (
    SessionChoice,
    SlashCompleteContext,
    best_completion,
    complete_slash,
    cycle_completion,
    format_completion_hint,
)


def test_root_command_prefix():
    assert "/session" in complete_slash("/se")
    assert "/sessions" in complete_slash("/se")
    assert best_completion("/m") in {"/mcp", "/model", "/memory"}
    assert complete_slash("hello") == []


def test_session_and_mcp_subcommands():
    assert "/session list" in complete_slash("/session l")
    assert "/session switch" in complete_slash("/session sw")
    assert complete_slash("/session ")[0].startswith("/session ")
    assert "/mcp reload" in complete_slash("/mcp re")
    assert "/mcp tools" in complete_slash("/mcp t")
    assert "/export json" in complete_slash("/export j")


def test_dynamic_thread_and_model_completion():
    ctx = SlashCompleteContext(
        thread_ids=["abc123", "abc999", "zzz"],
        sessions=[
            SessionChoice("abc123", "Fix auth bug"),
            SessionChoice("abc999", "Refactor models"),
            SessionChoice("zzz", "session zzz"),
        ],
        model_names=["openai:demo", "openai:fast", "local"],
    )
    cands = complete_slash("/switch abc", ctx)
    assert cands == ["/switch abc123", "/switch abc999"]

    # Title fragment completion inserts thread_id.
    cands = complete_slash("/switch Fix", ctx)
    assert cands == ["/switch abc123"]
    cands = complete_slash("/switch auth", ctx)
    assert cands == ["/switch abc123"]

    cands = complete_slash("/session delete abc", ctx)
    assert "/session delete abc123" in cands

    cands = complete_slash("/model open", ctx)
    assert "/model openai:demo" in cands
    assert "/model openai:fast" in cands

    hint = format_completion_hint("/switch ", ctx)
    assert "Fix auth bug" in hint
    assert "abc123" in hint


def test_cycle_and_hint():
    ctx = SlashCompleteContext()
    first = best_completion("/m", ctx)
    assert first is not None
    second = cycle_completion("/m", first, ctx)
    assert second is not None
    assert second != first or len(complete_slash("/m", ctx)) == 1

    # After accepting a full subcommand, cycle siblings.
    nxt = cycle_completion("/mcp list", "/mcp list", ctx)
    assert nxt is not None
    assert nxt.startswith("/mcp ")

    hint = format_completion_hint("/mcp ", ctx)
    assert hint.startswith("tab:")


def test_make_textual_suggester_returns_suggestion():
    import asyncio

    from synapse.slash_complete import make_textual_suggester

    suggester = make_textual_suggester(lambda: SlashCompleteContext())
    suggestion = asyncio.run(suggester.get_suggestion("/he"))
    assert suggestion == "/help"
