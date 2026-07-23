"""Tests for slash command completion."""

from __future__ import annotations

from pathlib import Path

from synapse.slash_complete import (
    SessionChoice,
    SlashCompleteContext,
    _glob_at_candidates,
    best_completion,
    complete_at_line,
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


# ---------------------------------------------------------------------------
# @ path completion tests
# ---------------------------------------------------------------------------


def _make_workspace(base: Path) -> Path:
    """Create a small directory tree for testing @ completion."""
    root = base / "ws"
    root.mkdir()
    (root / "README.md").write_text("")
    (root / "pyproject.toml").write_text("")
    src = root / "src"
    src.mkdir()
    (src / "app.py").write_text("")
    synapse = src / "synapse"
    synapse.mkdir()
    (synapse / "__init__.py").write_text("")
    (synapse / "slash_complete.py").write_text("")
    (synapse / "prompts.py").write_text("")
    ui = synapse / "ui"
    ui.mkdir()
    (ui / "tui.py").write_text("")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_models.py").write_text("")
    (tests_dir / "test_utils.py").write_text("")
    return root


def test_at_direct_children_in_root(tmp_path: Path):
    """Typing @REA should match README.md in workspace root."""
    ws = _make_workspace(tmp_path)
    cands = _glob_at_candidates("REA", ws)
    assert "README.md" in cands


def test_at_directory_listing(tmp_path: Path):
    """Typing @src/ should list immediate children of src/."""
    ws = _make_workspace(tmp_path)
    cands = _glob_at_candidates("src/", ws)
    assert "src/app.py" in cands
    assert "src/synapse/" in cands


def test_at_partial_path_with_slash(tmp_path: Path):
    """Typing @src/sy should match src/synapse/ under src."""
    ws = _make_workspace(tmp_path)
    cands = _glob_at_candidates("src/sy", ws)
    assert "src/synapse/" in cands


def test_at_recursive_prefix_search(tmp_path: Path):
    """Typing @sla should find src/synapse/slash_complete.py via recursive fallback."""
    ws = _make_workspace(tmp_path)
    cands = _glob_at_candidates("sla", ws)
    matching = [c for c in cands if "slash_complete" in c]
    assert len(matching) > 0, f"Expected recursive match, got: {cands}"


def test_at_recursive_finds_deep_file(tmp_path: Path):
    """Typing @tui should find src/synapse/ui/tui.py recursively."""
    ws = _make_workspace(tmp_path)
    cands = _glob_at_candidates("tui", ws)
    paths = [c for c in cands if "tui" in c]
    assert len(paths) > 0, f"Expected recursive tui match, got: {cands}"


def test_at_recursive_directories_first(tmp_path: Path):
    """Directory matches should appear before file matches."""
    ws = _make_workspace(tmp_path)
    cands = _glob_at_candidates("test", ws)
    # tests/ (dir) should come before test_models.py (file)
    dir_indices = [i for i, c in enumerate(cands) if c.endswith("/")]
    file_indices = [i for i, c in enumerate(cands) if not c.endswith("/")]
    if dir_indices and file_indices:
        assert min(dir_indices) < min(file_indices)


def test_at_recursive_skips_ignored_dirs(tmp_path: Path):
    """Recursive search should skip .git, __pycache__, .venv etc."""
    ws = _make_workspace(tmp_path)
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "foo.pyc").write_text("")
    (ws / ".venv").mkdir()
    (ws / ".venv" / "python.exe").write_text("")
    cands = _glob_at_candidates("conf", ws)
    # Should not include .git/config
    assert not any(".git" in c for c in cands)
    assert not any("__pycache__" in c for c in cands)
    assert not any(".venv" in c for c in cands)


def test_at_short_input_skips_recursive(tmp_path: Path):
    """Fewer than 3 chars should only match direct children, never recurse."""
    ws = _make_workspace(tmp_path)
    # "pr" matches nothing as a direct child, and 2 chars won't recurse.
    cands = _glob_at_candidates("pr", ws)
    assert not any("prompts" in c for c in cands)


def test_at_three_char_triggers_recursive(tmp_path: Path):
    """Three characters should allow the recursive fallback."""
    ws = _make_workspace(tmp_path)
    # "pro" should find "prompts.py" via recursive search.
    cands = _glob_at_candidates("pro", ws)
    matching = [c for c in cands if "prompts" in c]
    assert len(matching) > 0, f"Expected recursive match for 3-char, got: {cands}"


def test_complete_at_line_recursive(tmp_path: Path):
    """complete_at_line should return full-line candidates with recursive matches."""
    ws = _make_workspace(tmp_path)
    cands = complete_at_line("cat @sla", ws)
    matching = [c for c in cands if "slash_complete" in c]
    assert len(matching) > 0, f"Expected recursive match, got: {cands}"
    # Full-line format preserves the prefix.
    for c in matching:
        assert c.startswith("cat @")
