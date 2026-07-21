"""Git branch chrome for the TUI topbar.

Compact status:

- clean + in sync: green branch name (no dirty mark)
- dirty: red branch name + red `` *`` (space before star)
- diff stats (when dirty): `` Nf +A -D`` (files changed, lines added, lines deleted)
- ahead: ``↑N`` in ahead color
- behind: ``↓N`` in behind color

Diff stats use ``git diff HEAD --shortstat`` (working tree + index vs HEAD).

Changed-file popover data uses ``git status --porcelain`` plus staged/unstaged
``numstat`` so untracked files are included and each path gets ``+A -D``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text

# Cap large divergence counts so chrome stays short.
_COUNT_CAP = 99
# Popover / shortstat path caps (display only).
_PATH_CAP = 48
_FILE_LIST_CAP = 40


@dataclass(frozen=True, slots=True)
class GitBranchChrome:
    """Local git branch snapshot for topbar rendering."""

    name: str
    dirty: bool = False
    ahead: int | None = None  # None = no upstream tracking
    behind: int | None = None
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0

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


@dataclass(frozen=True, slots=True)
class GitChangedFile:
    """One dirty / untracked path for the branch hover popover."""

    path: str
    # Single display status: M / A / D / R / C / ? / U / T / …
    status: str
    lines_added: int = 0
    lines_deleted: int = 0
    is_untracked: bool = False
    # Optional rename/copy source (display only).
    source_path: str | None = None

    @property
    def has_line_stats(self) -> bool:
        return (not self.is_untracked) and (
            self.lines_added > 0 or self.lines_deleted > 0 or self.status in {"D", "A", "M", "R", "C", "T"}
        )


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


def _probe_diff_shortstat(cwd: Path, *, timeout: float = 0.8) -> tuple[int, int, int]:
    """Return ``(files_changed, lines_added, lines_deleted)`` from
    ``git diff HEAD --shortstat`` (working tree + index vs HEAD).

    Returns ``(0, 0, 0)`` on failure or when there is nothing to diff
    (e.g. no commits yet).
    """
    raw = _run_git(["diff", "HEAD", "--shortstat"], cwd=cwd, timeout=timeout)
    if not raw:
        return 0, 0, 0

    files = 0
    inserted = 0
    deleted = 0
    m = re.search(r"(\d+)\s+files?\s+changed", raw)
    if m:
        files = int(m.group(1))
    m = re.search(r"(\d+)\s+insertions?\(\+\)", raw)
    if m:
        inserted = int(m.group(1))
    m = re.search(r"(\d+)\s+deletions?\(-\)", raw)
    if m:
        deleted = int(m.group(1))
    return files, inserted, deleted


def _status_letter(xy: str, *, untracked: bool = False) -> str:
    """Collapse porcelain XY codes into one display letter."""
    if untracked or xy == "??":
        return "?"
    if xy == "!!":
        return "!"
    x = (xy[:1] or " ").upper()
    y = (xy[1:2] or " ").upper() if len(xy) > 1 else " "
    # Prefer worktree letter when both present and differ; else staged.
    for ch in (y, x):
        if ch not in {" ", "?"}:
            return ch
    return "M"


def _parse_porcelain_line(line: str) -> tuple[str, str, str | None] | None:
    """Return ``(xy, path, source_path)`` or None for empty/unusable lines."""
    raw = (line or "").rstrip("\n")
    if not raw:
        return None
    if raw.startswith("??") or raw.startswith("!!"):
        path = raw[2:].lstrip()
        if not path:
            return None
        return raw[:2], path, None
    if len(raw) < 4:
        return None
    xy = raw[:2]
    rest = raw[3:]
    source: str | None = None
    # Rename / copy: ``R  old -> new`` (non -z porcelain).
    if " -> " in rest and (xy[:1] in {"R", "C"} or xy[1:2] in {"R", "C"}):
        left, right = rest.split(" -> ", 1)
        source = left.strip() or None
        path = right.strip()
    else:
        path = rest.strip()
    if not path:
        return None
    return xy, path, source


def _parse_numstat(raw: str | None) -> dict[str, tuple[int, int]]:
    """Parse ``git diff --numstat`` output into path → (added, deleted)."""
    out: dict[str, tuple[int, int]] = {}
    if not raw:
        return out
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a_s, d_s, path = parts[0], parts[1], parts[-1]
        if not path:
            continue
        # Binary files show ``-`` for both counts.
        try:
            added = 0 if a_s == "-" else max(0, int(a_s))
        except ValueError:
            added = 0
        try:
            deleted = 0 if d_s == "-" else max(0, int(d_s))
        except ValueError:
            deleted = 0
        prev = out.get(path, (0, 0))
        out[path] = (prev[0] + added, prev[1] + deleted)
    return out


def probe_git_changed_files(
    cwd: Path | str,
    *,
    timeout: float = 1.0,
    limit: int = _FILE_LIST_CAP,
) -> list[GitChangedFile]:
    """List dirty / untracked files with optional line-level add/delete stats.

    Sources:
    - ``git status --porcelain=v1`` for paths + status letters (includes ``??``)
    - ``git diff --numstat`` + ``git diff --cached --numstat`` for ``+A -D``
    """
    root = Path(cwd)
    porcelain = _run_git(["status", "--porcelain=v1"], cwd=root, timeout=timeout)
    if porcelain is None:
        return []
    if not porcelain:
        return []

    unstaged = _parse_numstat(
        _run_git(["diff", "--numstat"], cwd=root, timeout=timeout)
    )
    staged = _parse_numstat(
        _run_git(["diff", "--cached", "--numstat"], cwd=root, timeout=timeout)
    )

    items: list[GitChangedFile] = []
    seen: set[str] = set()
    for line in porcelain.splitlines():
        parsed = _parse_porcelain_line(line)
        if parsed is None:
            continue
        xy, path, source = parsed
        if path in seen:
            continue
        seen.add(path)
        untracked = xy == "??"
        status = _status_letter(xy, untracked=untracked)
        added = 0
        deleted = 0
        if not untracked:
            a1, d1 = unstaged.get(path, (0, 0))
            a2, d2 = staged.get(path, (0, 0))
            added = a1 + a2
            deleted = d1 + d2
            # Renames: numstat may key the new path only; also try source.
            if source and added == 0 and deleted == 0:
                a1, d1 = unstaged.get(source, (0, 0))
                a2, d2 = staged.get(source, (0, 0))
                added = a1 + a2
                deleted = d1 + d2
        items.append(
            GitChangedFile(
                path=path,
                status=status,
                lines_added=added,
                lines_deleted=deleted,
                is_untracked=untracked,
                source_path=source,
            )
        )
        if limit > 0 and len(items) >= int(limit):
            break
    return items


def format_changed_file_plain(item: GitChangedFile, *, path_width: int = _PATH_CAP) -> str:
    """Plain row: ``M  path/to/file.py  +10 -3``."""
    status = (item.status or "M")[:1]
    path = item.path or ""
    if path_width > 0 and len(path) > path_width:
        path = "…" + path[-(path_width - 1) :]
    if item.is_untracked:
        stats = "untracked"
    elif item.lines_added or item.lines_deleted:
        stats = f"+{_format_count(item.lines_added)} -{_format_count(item.lines_deleted)}"
    elif status == "D":
        stats = "deleted"
    elif status == "A":
        stats = "added"
    else:
        stats = ""
    body = f"{status}  {path}"
    return f"{body}  {stats}" if stats else body


def render_changed_file_row(
    item: GitChangedFile,
    *,
    path_width: int = _PATH_CAP,
    color_status_m: str = "#f4b183",
    color_status_a: str = "#81c995",
    color_status_d: str = "#f28b82",
    color_status_u: str = "#9aa0a6",
    color_path: str = "#e8eaed",
    color_added: str = "#81c995",
    color_deleted: str = "#f28b82",
    color_muted: str = "#9aa0a6",
) -> Text:
    """Styled popover row for one changed file."""
    status = (item.status or "M")[:1]
    if item.is_untracked or status == "?":
        st_style = color_status_u
    elif status == "A":
        st_style = color_status_a
    elif status == "D":
        st_style = color_status_d
    elif status in {"R", "C"}:
        st_style = color_status_m
    else:
        st_style = color_status_m

    path = item.path or ""
    if path_width > 0 and len(path) > path_width:
        path = "…" + path[-(path_width - 1) :]

    out = Text()
    out.append(f"{status}  ", style=st_style)
    out.append(path, style=color_path)
    if item.is_untracked:
        out.append("  untracked", style=color_muted)
    elif item.lines_added or item.lines_deleted:
        out.append("  ")
        if item.lines_added:
            out.append(f"+{_format_count(item.lines_added)}", style=color_added)
            if item.lines_deleted:
                out.append(" ")
        if item.lines_deleted:
            out.append(f"-{_format_count(item.lines_deleted)}", style=color_deleted)
    elif status == "D":
        out.append("  deleted", style=color_deleted)
    elif status == "A":
        out.append("  added", style=color_added)
    return out


def format_branch_chrome_plain(
    info: GitBranchChrome | None,
    *,
    mark: str = "⎇",
) -> str:
    """Plain-text form for tests / fallbacks: ``⎇ main * 3f +120 -45 ↑2↓1``."""
    if info is None or not (info.name or "").strip():
        return ""
    name = info.name.strip()
    parts: list[str] = []
    head = f"{mark} {name}".strip() if (mark or "").strip() else name
    if info.dirty:
        head = f"{head} *"
    parts.append(head)
    # Diff stats (only when there are changes).
    if info.files_changed > 0:
        parts.append(f"{_format_count(info.files_changed)}f")
    if info.lines_added > 0:
        parts.append(f"+{_format_count(info.lines_added)}")
    if info.lines_deleted > 0:
        parts.append(f"-{_format_count(info.lines_deleted)}")
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
    color_files: str = "#9aa0a6",
    color_added: str = "#81c995",
    color_deleted: str = "#f28b82",
) -> Text:
    """Styled branch chrome for the topbar.

    Color rules:
    - fully clean/synced: green name (no star)
    - dirty: red name + red `` *``
    - diff stats: ``Nf`` (files count, muted), ``+A`` (added, green), ``-D`` (deleted, red)
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

    # Diff stats: files changed + lines added / deleted (vs HEAD).
    if info.files_changed > 0:
        out.append(f" {_format_count(info.files_changed)}f", style=color_files)
    if info.lines_added > 0:
        out.append(f" +{_format_count(info.lines_added)}", style=color_added)
    if info.lines_deleted > 0:
        out.append(f" -{_format_count(info.lines_deleted)}", style=color_deleted)

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

    files, inserted, deleted = _probe_diff_shortstat(root, timeout=timeout)

    return GitBranchChrome(
        name=name,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        files_changed=files,
        lines_added=inserted,
        lines_deleted=deleted,
    )
