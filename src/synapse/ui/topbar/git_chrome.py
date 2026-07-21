"""Git branch chrome for the TUI topbar.

Compact status:

- clean + in sync: green branch name (no dirty mark)
- dirty: red branch name + red `` *`` (space before star)
- ahead: ``↑N`` in ahead color
- behind: ``↓N`` in behind color
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text

# Cap large divergence counts so chrome stays short.
_COUNT_CAP = 99


@dataclass(frozen=True, slots=True)
class GitBranchChrome:
    """Local git branch snapshot for topbar rendering."""

    name: str
    dirty: bool = False
    ahead: int | None = None  # None = no upstream tracking
    behind: int | None = None

    @property
    def synced(self) -> bool:
        """Clean working tree and not ahead/behind (or no upstream)."""
        if self.dirty:
            return False
        if self.ahead is not None and int(self.ahead) > 0:
            return False
        if self.behind is not None and int(self.behind) > 0:
            return False
        return True


def _run_git(args: list[str], *, cwd: Path, timeout: float = 0.8) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def _format_count(n: int) -> str:
    n = max(0, int(n or 0))
    if n > _COUNT_CAP:
        return f"{_COUNT_CAP}+"
    return str(n)


def format_branch_chrome_plain(
    info: GitBranchChrome | None,
    *,
    mark: str = "⎇",
) -> str:
    """Plain-text form for tests / fallbacks: ``⎇ main * ↑2↓1``."""
    if info is None or not (info.name or "").strip():
        return ""
    name = info.name.strip()
    parts: list[str] = []
    head = f"{mark} {name}".strip() if (mark or "").strip() else name
    if info.dirty:
        head = f"{head} *"
    parts.append(head)
    bits = ""
    if info.ahead is not None and int(info.ahead) > 0:
        bits += f"↑{_format_count(int(info.ahead))}"
    if info.behind is not None and int(info.behind) > 0:
        bits += f"↓{_format_count(int(info.behind))}"
    if bits:
        parts.append(bits)
    return " ".join(parts)


def render_branch_chrome(
    info: GitBranchChrome | None,
    *,
    mark: str = "⎇",
    color_clean: str = "#81c995",
    color_dirty: str = "#f28b82",
    color_ahead: str = "#8ab4f8",
    color_behind: str = "#f4b183",
    color_diverged: str = "#e8eaed",
) -> Text:
    """Styled branch chrome for the topbar.

    Color rules:
    - fully clean/synced: green name (no star)
    - dirty: red name + red `` *``
    - clean but diverged: neutral name; ahead blue; behind orange
    """
    out = Text()
    if info is None or not (info.name or "").strip():
        return out

    name = info.name.strip()
    mark_s = (mark or "").strip()
    if info.dirty:
        name_style = color_dirty
    elif info.synced:
        name_style = color_clean
    else:
        name_style = color_diverged

    if mark_s:
        out.append(f"{mark_s} ", style=name_style)
    out.append(name, style=name_style)
    if info.dirty:
        # Space before dirty mark; mark shares dirty red with the name.
        out.append(" *", style=color_dirty)

    bits = Text()
    if info.ahead is not None and int(info.ahead) > 0:
        bits.append(f"↑{_format_count(int(info.ahead))}", style=color_ahead)
    if info.behind is not None and int(info.behind) > 0:
        bits.append(f"↓{_format_count(int(info.behind))}", style=color_behind)
    if bits.plain:
        out.append(" ")
        out.append_text(bits)
    return out


def probe_git_branch_chrome(cwd: Path | str, *, timeout: float = 0.8) -> GitBranchChrome | None:
    """Probe local branch name, dirty flag, and upstream ahead/behind."""
    root = Path(cwd)
    name = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=timeout)
    if not name or name == "HEAD":
        return None

    dirty = False
    porcelain = _run_git(["status", "--porcelain"], cwd=root, timeout=timeout)
    # status returns "" when clean; None on failure → treat as not dirty.
    if porcelain:
        dirty = True

    ahead: int | None = None
    behind: int | None = None
    # left = upstream-only (behind), right = HEAD-only (ahead)
    counts = _run_git(
        ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        cwd=root,
        timeout=timeout,
    )
    if counts:
        parts = counts.split()
        if len(parts) >= 2:
            try:
                behind = max(0, int(parts[0]))
                ahead = max(0, int(parts[1]))
            except ValueError:
                ahead = None
                behind = None

    return GitBranchChrome(name=name, dirty=dirty, ahead=ahead, behind=behind)
