"""What one turn did to the workspace, file by file.

The transcript reports *this turn's* contribution to each file, so the numbers cannot
come from `git diff --numstat HEAD`: that is the workspace's standing delta against the
last commit, and every earlier turn's edits would be counted again.  Instead the state
at the turn's start is recorded -- the changed files themselves, bounded, read with
plain reads so the repository is never written to -- and the delta is the difference
between that state and the state at the end.

A file the turn created is all insertions, one it deleted is all deletions, and one it
edited is counted from the two versions line by line (the same shape `git diff
--numstat` reports, without asking git to reconstruct a state we already hold).

Nothing here is a policy: the caller decides when to snapshot, how many files to keep
and what to do with the result.
"""

from __future__ import annotations

import difflib
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from synapse.runtime.service.event_types import TurnChange

#: How many changed files one snapshot describes.  A turn that rewrites a whole tree is
#: reported by count, not file by file.
MAX_SNAPSHOT_FILES = 200
#: How much of one file is kept for the line count.  A bigger file still counts as
#: changed; it just cannot be counted.
MAX_FILE_BYTES = 256 * 1024
#: Whole-snapshot budget.  The first files that fit are kept, in path order, so the
#: snapshot is bounded by bytes and not only by count.
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
#: Git is read-only here, so a hung repository must not hold a turn open.
GIT_TIMEOUT_S = 5.0

#: Status letters `git status --porcelain` prints for a path.
_ADDED = "A"
_DELETED = "D"
_UNTRACKED = "??"

#: The runtime's own bookkeeping inside a workspace (`<workspace>/.synapse/logs/...`,
#: see `observability/error_log.py`).  It is not something the reader asked a turn to
#: do, so a turn's changes never report it -- a failed turn would otherwise list its
#: own error log beside the work.
_IGNORED_PREFIXES = (".synapse/",)


def _is_ignored(path: str) -> bool:
    return path.startswith(_IGNORED_PREFIXES)


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One changed file as it stood when the snapshot was taken."""

    path: str
    #: `git status`'s own worktree letter (`A`, `M`, `D`, `??`, ...).
    status: str
    #: Digest of the content, so an unchanged file is recognised without a diff.
    digest: str
    #: Line count of the content, or `None` when it was not kept.
    lines: int | None
    #: The content itself, or `None` when the file was too large, binary or unreadable.
    content: str | None
    binary: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """The changed files of a workspace at one moment."""

    files: dict[str, SnapshotFile] = field(default_factory=dict)
    #: Each changed path's standing delta against `HEAD`, from `git diff --numstat`.
    #: A file that was *clean* when the snapshot was taken has no kept version, and its
    #: standing delta is exactly the delta of whatever changes it next -- so the turn
    #: that touches it can still be counted without keeping every clean file's content.
    standing: dict[str, tuple[int, int, bool]] = field(default_factory=dict)
    #: True when the workspace held more changed files than `MAX_SNAPSHOT_FILES`.
    truncated: bool = False
    #: The commit `HEAD` named when the snapshot was taken, or `None` when git could not
    #: name one.  A file that was clean at this moment holds exactly this commit's
    #: version of itself, which is what a revert puts back -- and only while `HEAD`
    #: still points here.
    head: str | None = None
    #: True when a changed file existed but its content was not kept (binary, over
    #: budget, unreadable).  A revert then cannot assume that a path *missing* from
    #: this snapshot was clean at `HEAD`, so it refuses instead of guessing.
    content_skipped: bool = False


def _git(root: Path, args: list[str]) -> str | None:
    """Run one read-only git command, or `None` when git cannot answer."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def _status_entries(raw: str) -> list[tuple[str, str]]:
    """`(worktree letter, path)` for each `git status --porcelain` line."""
    entries: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        if line.startswith(_UNTRACKED):
            # `??` is both columns at once, not a letter in the worktree column.
            entries.append((_UNTRACKED, line[3:].strip()))
            continue
        index_letter, worktree_letter, path = line[0], line[1], line[3:].strip()
        # A rename prints `old -> new`; the new path is the one that exists now.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        letter = worktree_letter if worktree_letter != " " else index_letter
        entries.append((letter, path))
    return entries


def _numstat_entries(raw: str) -> dict[str, tuple[int, int, bool]]:
    """`path -> (insertions, deletions, binary)` from `git diff --numstat`."""
    totals: dict[str, tuple[int, int, bool]] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[2]
        if " => " in path:  # a rename prints `old => new`
            path = path.split(" => ", 1)[1]
        if added == "-" or removed == "-":
            totals[path] = (0, 0, True)
            continue
        try:
            totals[path] = (int(added), int(removed), False)
        except ValueError:
            continue
    return totals


def _read_file(
    root: Path, path: str, *, budget: int
) -> tuple[str | None, int | None, bool, bool]:
    """Read one file for the snapshot: `(content, lines, binary, skipped)`.

    A file that cannot be counted (too large for the remaining budget, binary, or
    unreadable) is still a changed file; it just reports no lines.  ``skipped`` says the
    file exists but its content was not kept, so its absence from another snapshot can
    never be read as "unchanged".
    """
    target = root / path
    try:
        if not target.is_file():
            return None, None, False, False
        size = target.stat().st_size
        if size > budget or size > MAX_FILE_BYTES:
            return None, None, False, True
        raw = target.read_bytes()
    except OSError:
        return None, None, False, True
    if b"\x00" in raw:
        return None, None, True, True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, True, True
    return text, len(text.splitlines()), False, False


def snapshot_workspace(root: Path | str) -> WorkspaceSnapshot | None:
    """Record every file the workspace has changed, as it stands right now.

    `None` when git cannot answer at all (not a repository, git missing, timeout): a
    turn must not fail because its bookkeeping could not be taken.
    """
    workspace = Path(root).expanduser()
    raw = _git(workspace, ["status", "--porcelain=v1", "--untracked-files=all"])
    if raw is None:
        return None
    numstat = _git(workspace, ["diff", "--numstat", "HEAD"])
    standing = _numstat_entries(numstat) if numstat is not None else {}
    head = (_git(workspace, ["rev-parse", "HEAD"]) or "").strip() or None
    entries = sorted(_status_entries(raw), key=lambda entry: entry[1])
    files: dict[str, SnapshotFile] = {}
    budget = MAX_SNAPSHOT_BYTES
    truncated = False
    content_skipped = False
    for letter, path in entries:
        if _is_ignored(path):
            continue
        if len(files) >= MAX_SNAPSHOT_FILES:
            truncated = True
            break
        content, lines, binary, skipped = _read_file(workspace, path, budget=budget)
        content_skipped = content_skipped or skipped
        if content is not None:
            budget -= len(content.encode("utf-8"))
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        else:
            # No content to compare: fall back to the file's own identity, so a file
            # that did not change between two snapshots is still recognised.
            try:
                stat = (workspace / path).stat()
                digest = f"stat:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                digest = "stat:missing"
        files[path] = SnapshotFile(
            path=path,
            status=letter,
            digest=digest,
            lines=lines,
            content=content,
            binary=binary,
        )
    return WorkspaceSnapshot(
        files=files,
        standing=standing,
        truncated=truncated,
        head=head,
        content_skipped=content_skipped,
    )


def _count_lines(before: str, after: str) -> tuple[int, int]:
    """Added and removed lines between two versions of one file."""
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return added, removed


def changes_between(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
    *,
    limit: int = 50,
) -> tuple[tuple[TurnChange, ...], int]:
    """The files the turn touched, and how many there were.

    Pure over two snapshots: the caller decides when each was taken.  A file whose
    versions were not both kept reports that it changed and no line counts, rather than
    a fabricated zero.
    """
    changes: list[TurnChange] = []
    paths = sorted(set(before.files) | set(after.files))
    for path in paths:
        if _is_ignored(path):
            continue
        was = before.files.get(path)
        now = after.files.get(path)
        if was is not None and now is not None and was.digest == now.digest:
            continue
        if was is None and now is None:
            continue
        if was is None:
            # The file was clean (or absent) when the turn started: an untracked or
            # newly added path is the turn's own creation, and anything else existed at
            # `HEAD` already -- so the turn's delta is that file's standing delta, which
            # is what the snapshot recorded from `git diff --numstat`.
            created = now.status in {_UNTRACKED, _ADDED}
            standing = after.standing.get(path)
            if standing is None:
                status = "added" if created else "modified"
                insertions, deletions, binary = (now.lines or 0), 0, now.binary
            else:
                status = "added" if created else "modified"
                insertions, deletions, binary = standing
        elif now is None:
            status, insertions, deletions, binary = "deleted", 0, was.lines or 0, was.binary
        elif was.content is not None and now.content is not None:
            insertions, deletions = _count_lines(was.content, now.content)
            status, binary = "modified", False
        else:
            # Changed, but at least one version was not kept: say so instead of zero.
            status, insertions, deletions, binary = "modified", 0, 0, True
        changes.append(
            TurnChange(
                path=path,
                status=status,
                insertions=insertions,
                deletions=deletions,
                binary=binary,
            )
        )
    total = len(changes)
    # Biggest changes first: a reader scanning a long list wants the substantial files,
    # and the count above still says how many there were.
    changes.sort(key=lambda change: (-(change.insertions + change.deletions), change.path))
    return tuple(changes[:limit]), total
