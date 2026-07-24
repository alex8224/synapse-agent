"""Git branch chrome formatting and probe helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import synapse.ui.topbar.git_chrome as git_chrome
from synapse.ui.topbar.git_chrome import (
    GitBranchChrome,
    GitChangedFile,
    _parse_numstat,
    _parse_porcelain_line,
    _status_letter,
    format_branch_chrome_plain,
    format_changed_file_plain,
    render_branch_chrome,
    render_changed_file_row,
)


def test_run_git_uses_utf8_replace_for_windows_output(monkeypatch) -> None:
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="feature/中文\n")

    monkeypatch.setattr(git_chrome.subprocess, "run", fake_run)

    result = git_chrome._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=Path("."))

    assert result == "feature/中文"
    assert calls[0][1]["encoding"] == "utf-8"
    assert calls[0][1]["errors"] == "replace"


def test_plain_clean_synced_no_star() -> None:
    info = GitBranchChrome(name="main", dirty=False, ahead=0, behind=0)
    assert format_branch_chrome_plain(info) == "⎇ main"
    assert "*" not in format_branch_chrome_plain(info)


def test_plain_dirty_space_before_star() -> None:
    info = GitBranchChrome(name="main", dirty=True, ahead=0, behind=0)
    assert format_branch_chrome_plain(info) == "⎇ main *"


def test_plain_ahead_behind_combo() -> None:
    info = GitBranchChrome(name="feat", dirty=True, ahead=2, behind=1)
    assert format_branch_chrome_plain(info) == "⎇ feat * ↑2↓1"


def test_plain_count_cap() -> None:
    info = GitBranchChrome(name="main", dirty=False, ahead=120, behind=0)
    assert format_branch_chrome_plain(info) == "⎇ main ↑99+"


def test_rich_clean_uses_green_no_star() -> None:
    info = GitBranchChrome(name="main", dirty=False, ahead=0, behind=0)
    text = render_branch_chrome(
        info,
        color_clean="green",
        color_dirty="red",
        color_ahead="blue",
        color_behind="orange",
    )
    assert text.plain == "⎇ main"
    assert "*" not in text.plain
    # first span style is clean green
    spans = list(text.spans)
    assert spans
    assert "green" in str(spans[0].style)


def test_rich_dirty_name_and_star_red() -> None:
    info = GitBranchChrome(name="main", dirty=True)
    text = render_branch_chrome(
        info,
        color_clean="green",
        color_dirty="red",
        color_ahead="blue",
        color_behind="orange",
    )
    assert text.plain == "⎇ main *"
    styles = [str(s.style) for s in text.spans]
    assert any("red" in s for s in styles)
    assert all("green" not in s for s in styles)


def test_rich_ahead_behind_colors() -> None:
    info = GitBranchChrome(name="main", dirty=False, ahead=2, behind=3)
    text = render_branch_chrome(
        info,
        color_clean="green",
        color_dirty="red",
        color_ahead="blue",
        color_behind="orange",
        color_diverged="white",
    )
    assert text.plain == "⎇ main ↑2↓3"
    styles = " ".join(str(s.style) for s in text.spans)
    assert "blue" in styles
    assert "orange" in styles
    # not fully synced → not green
    assert "green" not in styles


def test_synced_property() -> None:
    assert GitBranchChrome("m", dirty=False, ahead=0, behind=0).synced
    assert GitBranchChrome("m", dirty=False, ahead=None, behind=None).synced
    assert not GitBranchChrome("m", dirty=True, ahead=0, behind=0).synced
    assert not GitBranchChrome("m", dirty=False, ahead=1, behind=0).synced



def test_status_letter_and_porcelain_parse() -> None:
    assert _status_letter("M ") == "M"
    assert _status_letter(" M") == "M"
    assert _status_letter("A ") == "A"
    assert _status_letter("??", untracked=True) == "?"
    assert _parse_porcelain_line(" M src/app.py") == (" M", "src/app.py", None)
    assert _parse_porcelain_line("?? scratch.txt") == ("??", "scratch.txt", None)
    assert _parse_porcelain_line("R  old.py -> new.py") == ("R ", "new.py", "old.py")


def test_parse_numstat_sums() -> None:
    raw = "10\t3\tsrc/a.py\n-\t-\tbin.dat\n2\t1\tsrc/a.py\n"
    stats = _parse_numstat(raw)
    assert stats["src/a.py"] == (12, 4)
    assert stats["bin.dat"] == (0, 0)


def test_format_changed_file_plain() -> None:
    m = GitChangedFile(path="src/app.py", status="M", lines_added=10, lines_deleted=3)
    assert format_changed_file_plain(m) == "M  src/app.py  +10 -3"
    u = GitChangedFile(path="new.txt", status="?", is_untracked=True)
    assert "untracked" in format_changed_file_plain(u)
    d = GitChangedFile(path="gone.py", status="D")
    assert "deleted" in format_changed_file_plain(d)


def test_line_counts_not_capped() -> None:
    """Add/delete line counts show full values; ahead still caps at 99+."""
    info = GitBranchChrome(
        name="main",
        dirty=True,
        ahead=120,
        files_changed=3,
        lines_added=150,
        lines_deleted=200,
    )
    plain = format_branch_chrome_plain(info)
    assert plain == "⎇ main * 3f +150 -200 ↑99+"
    rich = render_branch_chrome(info)
    assert rich.plain == "⎇ main * 3f +150 -200 ↑99+"

    big = GitChangedFile(path="big.py", status="M", lines_added=150, lines_deleted=200)
    assert format_changed_file_plain(big) == "M  big.py  +150 -200"
    row = render_changed_file_row(big)
    assert "+150" in row.plain
    assert "-200" in row.plain


def test_render_changed_file_row_styles() -> None:
    item = GitChangedFile(path="src/app.py", status="M", lines_added=2, lines_deleted=1)
    text = render_changed_file_row(item, color_added="green", color_deleted="red")
    assert "src/app.py" in text.plain
    assert "+2" in text.plain
    assert "-1" in text.plain
