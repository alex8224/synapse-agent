"""Read-only git status/diff for one session's workspace.

The TUI shells out to ``git`` in-process (``ui/git_explore/provider.py``); the
console has no such channel, so the same two questions are answered here and
exposed over the wire: what the working tree looks like, and what one file's
diff is.  Nothing here writes: no staging, no commits, no checkout.  An untracked
file has no diff to ask git for, so its content is read (bounded, binary-safe) and
reported as the diff a new file would produce -- never by staging it first.

Every result is bounded — the file list, the diff size and the subprocess
timeout — and every failure the caller can act on is a typed error rather than
an empty result that would look like "no changes".
"""

from __future__ import annotations

import difflib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from synapse.runtime.service.errors import (
    GitUnavailableError,
    InvalidRequestError,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "GitDiffQuery",
    "GitDiffResult",
    "GitFileChange",
    "GitStatusQuery",
    "GitStatusResult",
    "MAX_DIFF_BYTES",
    "MAX_STATUS_FILES",
    "git_diff_workspace",
    "git_status_workspace",
]

#: Changed files reported in one status call; past this the result says so.
MAX_STATUS_FILES = 200
#: One file's diff is capped at this many bytes (the text is UTF-8, so this is
#: also the character budget for ASCII-heavy diffs).
MAX_DIFF_BYTES = 256 * 1024
#: A git probe must never hold a worker thread indefinitely.
GIT_TIMEOUT_S = 5.0
#: The empty tree object: what an unborn ``HEAD`` is diffed against so a
#: repository with no commit yet still reports its staged additions.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass(frozen=True, slots=True)
class GitStatusQuery:
    """Status of the workspace the session runs in."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class GitFileChange:
    """One changed path, using git's own two-letter status columns.

    ``index_status`` is the staged column, ``worktree_status`` the unstaged one;
    ``??`` means untracked.  Both are passed through verbatim so a client can
    render them the way git does instead of guessing from a boolean.
    """

    path: str
    index_status: str
    worktree_status: str


@dataclass(frozen=True, slots=True)
class GitStatusResult:
    branch: str | None
    upstream: str | None
    ahead: int
    behind: int
    dirty: bool
    files: tuple[GitFileChange, ...]
    truncated: bool
    #: Tracked added/removed lines against ``HEAD`` (staged and unstaged
    #: combined, so a file is never counted twice); ``None`` when git cannot
    #: answer, never a fabricated ``0``.  Binary changes and untracked files
    #: carry no line counts and contribute nothing.
    insertions: int | None
    deletions: int | None


@dataclass(frozen=True, slots=True)
class GitDiffQuery:
    session: SessionRef
    path: str
    #: Compare the staged (index) version instead of the worktree.
    staged: bool = False


@dataclass(frozen=True, slots=True)
class GitDiffResult:
    path: str
    text: str
    binary: bool
    truncated: bool
    #: True when the path has no diff at all (unchanged, or untracked).
    empty: bool


def _workspace_root(session: object) -> Path:
    workspace = getattr(session, "workspace", None)
    if not workspace:
        raise GitUnavailableError("git workspace is unavailable")
    try:
        root = Path(str(workspace)).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise GitUnavailableError("git workspace is unavailable") from exc
    if not root.is_dir():
        raise GitUnavailableError("git workspace is unavailable")
    return root


def _run_git(root: Path, args: list[str]) -> bytes | None:
    """Run one bounded read-only git command; ``None`` when git cannot answer.

    A missing ``git`` binary, a non-repository workspace or a timeout all mean
    the same thing to a caller — "git cannot be read here" — and none of them
    may raise through the service boundary.
    """
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
    return completed.stdout


def _validate_repo_path(path: str) -> str:
    """One safe workspace-relative POSIX path, or ``InvalidRequestError``."""
    if not isinstance(path, str) or path == "":
        raise InvalidRequestError("git path is required")
    if len(path.encode("utf-8")) > 4096:
        raise InvalidRequestError("git path is too long")
    if "\\" in path or path.startswith("/") or ":" in path:
        raise InvalidRequestError("git path must be a relative POSIX path")
    pure = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in pure.parts):
        raise InvalidRequestError("git path must not contain empty or parent segments")
    if pure.is_absolute():
        raise InvalidRequestError("git path must be relative")
    return pure.as_posix()


def _parse_branch_line(line: str) -> tuple[str | None, str | None, int, int]:
    """Parse ``## branch...upstream [ahead N, behind M]`` from porcelain v1."""
    body = line[3:].strip()
    if body.startswith("HEAD (no branch)"):
        return None, None, 0, 0
    if body.startswith("No commits yet on "):
        return body[len("No commits yet on ") :].strip() or None, None, 0, 0
    ahead = behind = 0
    bracket = body.find(" [")
    if bracket >= 0:
        tracking = body[bracket + 2 :].rstrip("]")
        body = body[:bracket]
        for part in tracking.split(","):
            chunk = part.strip()
            if chunk.startswith("ahead "):
                ahead = int(chunk[6:].strip() or 0)
            elif chunk.startswith("behind "):
                behind = int(chunk[7:].strip() or 0)
    branch, _, upstream = body.partition("...")
    return (branch.strip() or None), (upstream.strip() or None), ahead, behind


def _numstat_totals(root: Path) -> tuple[int, int] | None:
    """Total added/removed tracked lines, or ``None`` when git cannot answer.

    ``git diff --numstat HEAD`` is the combined diff against the last commit, so
    a file that is both staged and further modified is counted once — the index
    and the worktree diffs are never summed on top of each other.  An unborn
    ``HEAD`` (a repository with no commit yet) is compared against the empty tree
    instead, which is the same projection.  Binary changes report ``-`` for both
    columns: they have no line counts and are skipped rather than counted as
    zero, and untracked files are not part of any diff so they are not counted
    here either.  A malformed line returns ``None`` so the caller reports
    "unknown" instead of a fabricated zero.
    """
    raw = _run_git(root, ["diff", "--numstat", "HEAD"])
    if raw is None:
        raw = _run_git(root, ["diff", "--numstat", _EMPTY_TREE])
    if raw is None:
        return None
    insertions = deletions = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed = parts[0], parts[1]
        if added == "-" or removed == "-":
            continue
        try:
            insertions += int(added)
            deletions += int(removed)
        except ValueError:
            return None
    return insertions, deletions


def git_status_workspace(query: GitStatusQuery, session: object) -> GitStatusResult:
    """``git status --porcelain=v1 --branch`` for the session's workspace."""
    if not isinstance(query, GitStatusQuery):
        raise InvalidRequestError("git status query must be a GitStatusQuery")
    root = _workspace_root(session)
    raw = _run_git(root, ["status", "--porcelain=v1", "--branch", "--untracked-files=all"])
    if raw is None:
        raise GitUnavailableError("git status is unavailable in this workspace")
    text = raw.decode("utf-8", errors="replace")

    branch: str | None = None
    upstream: str | None = None
    ahead = behind = 0
    files: list[GitFileChange] = []
    truncated = False
    for line in text.splitlines():
        if line.startswith("## "):
            branch, upstream, ahead, behind = _parse_branch_line(line)
            continue
        if len(line) < 4:
            continue
        if len(files) >= MAX_STATUS_FILES:
            truncated = True
            break
        files.append(
            GitFileChange(
                path=line[3:].strip(),
                index_status=line[0],
                worktree_status=line[1],
            )
        )
    totals = _numstat_totals(root)
    return GitStatusResult(
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        dirty=bool(files),
        files=tuple(files),
        truncated=truncated,
        insertions=None if totals is None else totals[0],
        deletions=None if totals is None else totals[1],
    )


def git_diff_workspace(query: GitDiffQuery, session: object) -> GitDiffResult:
    """One file's unified diff (staged or worktree), bounded and binary-safe."""
    if not isinstance(query, GitDiffQuery):
        raise InvalidRequestError("git diff query must be a GitDiffQuery")
    path = _validate_repo_path(query.path)
    root = _workspace_root(session)
    args = ["diff", "--no-color", "--unified=3"]
    if query.staged:
        args.append("--cached")
    args.extend(["--", path])
    raw = _run_git(root, args)
    if raw is None:
        raise GitUnavailableError("git diff is unavailable in this workspace")
    if b"\x00" in raw:
        return GitDiffResult(path=path, text="", binary=True, truncated=False, empty=False)
    if b"Binary files " in raw and b" differ" in raw:
        return GitDiffResult(path=path, text="", binary=True, truncated=False, empty=False)
    truncated = len(raw) > MAX_DIFF_BYTES
    body = raw[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")
    if body.strip() == "" and not query.staged:
        # `git diff` says nothing about a path it does not track, so an untracked file used
        # to be listed with nothing to show for it.  Its content is read here instead.
        new_file = _new_file_diff(root, path)
        if new_file is not None:
            return new_file
    return GitDiffResult(
        path=path,
        text=body,
        binary=False,
        truncated=truncated,
        # An empty diff is a real answer: unchanged, untracked, or a path git
        # cannot diff.  The caller renders it instead of an error.
        empty=body.strip() == "",
    )


def _untracked_paths(root: Path, path: str) -> set[str] | None:
    """The untracked, non-ignored paths a pathspec names, or `None` if git cannot answer."""
    raw = _run_git(root, ["ls-files", "--others", "--exclude-standard", "-z", "--", path])
    if raw is None:
        return None
    return {item for item in raw.decode("utf-8", errors="replace").split("\0") if item}


def _new_file_diff(root: Path, path: str) -> GitDiffResult | None:
    """The diff of an untracked file against nothing, or `None` when it is not one.

    Built here rather than asked of `git diff --no-index` because that needs a second path
    to compare against, and because nothing in this module may touch the repository or its
    index -- in particular an untracked file is *not* added with `--intent-to-add` just to
    make it visible.  A symlink reports its target, which is what git shows for one.
    """
    untracked = _untracked_paths(root, path)
    if untracked is None or path not in untracked:
        return None
    target = root.joinpath(*PurePosixPath(path).parts)
    try:
        if target.is_symlink():
            # A symlink's "content" is its target, as one line, the way git shows it.
            content = f"{os.readlink(target)}\n"
            size = len(content)
        elif target.is_file():
            size = target.stat().st_size
            with target.open("rb") as stream:
                raw = stream.read(MAX_DIFF_BYTES)
            if b"\x00" in raw:
                return GitDiffResult(path=path, text="", binary=True, truncated=False, empty=False)
            content = raw.decode("utf-8", errors="replace")
        else:
            # A directory (or something that is neither): git shows no file diff for it.
            return None
    except OSError:
        return None
    body = "".join(
        difflib.unified_diff(
            [],
            content.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{path}",
            lineterm="\n",
        )
    )
    encoded = body.encode("utf-8")
    truncated = size > MAX_DIFF_BYTES
    if len(encoded) > MAX_DIFF_BYTES:
        body = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")
        truncated = True
    return GitDiffResult(
        path=path,
        text=body,
        binary=False,
        truncated=truncated,
        # A new empty file has no hunks: the headers alone are the whole diff, exactly as
        # git reports one.
        empty=body.strip() == "",
    )
