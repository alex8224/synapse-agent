"""Git branch chrome formatting and probe helpers."""

from __future__ import annotations

from synapse.ui.topbar.git_chrome import (
    GitBranchChrome,
    format_branch_chrome_plain,
    render_branch_chrome,
)


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
