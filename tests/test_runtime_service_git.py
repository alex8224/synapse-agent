"""Read-only git service: status/diff parsing, bounding and failure modes."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.service.errors import GitUnavailableError, InvalidRequestError
from synapse.runtime.service.git import (
    MAX_DIFF_BYTES,
    MAX_STATUS_FILES,
    GitDiffQuery,
    GitStatusQuery,
    git_diff_workspace,
    git_status_workspace,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import ProtocolError, dispatch

REF = SessionRef(project_id="p", thread_id="t")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet", "-b", "main")
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "--quiet", "-m", "init")
    return tmp_path


def _session(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace=str(workspace))


def test_status_reports_branch_and_changed_files(repo: Path) -> None:
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")

    status = git_status_workspace(GitStatusQuery(REF), _session(repo))

    assert status.branch == "main"
    assert status.dirty is True
    assert status.truncated is False
    paths = {change.path for change in status.files}
    assert paths == {"tracked.txt", "untracked.txt"}
    untracked = next(c for c in status.files if c.path == "untracked.txt")
    assert (untracked.index_status, untracked.worktree_status) == ("?", "?")


def test_a_clean_repository_is_not_dirty(repo: Path) -> None:
    status = git_status_workspace(GitStatusQuery(REF), _session(repo))
    assert status.files == ()
    assert status.dirty is False
    assert status.branch == "main"
    # A clean tree has a real zero, not an unknown: the numstat probe answered.
    assert (status.insertions, status.deletions) == (0, 0)


def test_status_reports_tracked_line_counts(repo: Path) -> None:
    (repo / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    status = git_status_workspace(GitStatusQuery(REF), _session(repo))
    assert (status.insertions, status.deletions) == (2, 0)


def test_removed_lines_are_reported(repo: Path) -> None:
    (repo / "tracked.txt").write_text("", encoding="utf-8")
    status = git_status_workspace(GitStatusQuery(REF), _session(repo))
    assert (status.insertions, status.deletions) == (0, 1)


def test_staged_and_unstaged_changes_are_never_counted_twice(repo: Path) -> None:
    # Stage a file, then modify it again: the combined diff against HEAD counts
    # the file once, instead of summing the index and the worktree diffs.
    (repo / "staged.txt").write_text("a\nb\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    (repo / "staged.txt").write_text("a\nb\nc\n", encoding="utf-8")
    status = git_status_workspace(GitStatusQuery(REF), _session(repo))
    assert (status.insertions, status.deletions) == (3, 0)


def test_untracked_and_binary_changes_contribute_no_lines(repo: Path) -> None:
    # One tracked line added, one untracked file (not part of any diff), and one
    # staged binary file (numstat reports `-`, so it has no line counts).
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\nnewer\n", encoding="utf-8")
    (repo / "blob.bin").write_bytes(bytes(range(256)))
    _git(repo, "add", "blob.bin")
    status = git_status_workspace(GitStatusQuery(REF), _session(repo))
    assert (status.insertions, status.deletions) == (1, 0)
    assert {change.path for change in status.files} == {
        "tracked.txt",
        "untracked.txt",
        "blob.bin",
    }


def test_an_unborn_head_still_reports_staged_line_counts(tmp_path: Path) -> None:
    # A repository with no commit yet: `git diff HEAD` has no HEAD to name, so the
    # worktree is compared against the empty tree instead.
    _git(tmp_path, "init", "--quiet", "-b", "main")
    (tmp_path / "new.txt").write_text("a\nb\nc\n", encoding="utf-8")
    _git(tmp_path, "add", "new.txt")
    status = git_status_workspace(GitStatusQuery(REF), _session(tmp_path))
    assert (status.insertions, status.deletions) == (3, 0)


def test_the_file_list_is_bounded(repo: Path) -> None:
    for index in range(MAX_STATUS_FILES + 5):
        (repo / f"file-{index:04d}.txt").write_text("x\n", encoding="utf-8")
    status = git_status_workspace(GitStatusQuery(REF), _session(repo))
    assert len(status.files) == MAX_STATUS_FILES
    assert status.truncated is True


def test_diff_returns_the_unified_diff_of_one_path(repo: Path) -> None:
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    diff = git_diff_workspace(GitDiffQuery(REF, "tracked.txt"), _session(repo))
    assert diff.empty is False
    assert diff.binary is False
    assert diff.truncated is False
    assert "+two" in diff.text
    assert diff.path == "tracked.txt"


def test_an_unchanged_path_is_an_empty_diff_not_an_error(repo: Path) -> None:
    diff = git_diff_workspace(GitDiffQuery(REF, "tracked.txt"), _session(repo))
    assert diff.empty is True
    assert diff.text == ""


def test_an_untracked_file_shows_its_content_as_a_new_file(repo: Path) -> None:
    # `git diff` says nothing about a path it does not track, so the explorer used to list
    # an untracked file and then show nothing at all.  Nothing is staged to fix that.
    (repo / "fresh.txt").write_text("one\ntwo\n", encoding="utf-8")

    diff = git_diff_workspace(GitDiffQuery(REF, "fresh.txt"), _session(repo))

    assert diff.empty is False
    assert diff.binary is False
    assert diff.truncated is False
    assert diff.text.splitlines()[0] == "--- /dev/null"
    assert diff.text.splitlines()[1] == "+++ b/fresh.txt"
    assert [line for line in diff.text.splitlines() if line.startswith("+")] == [
        "+++ b/fresh.txt",
        "+one",
        "+two",
    ]
    status = git_status_workspace(GitStatusQuery(REF), _session(repo))
    fresh = next(change for change in status.files if change.path == "fresh.txt")
    assert (fresh.index_status, fresh.worktree_status) == ("?", "?"), (
        "reading an untracked file must not stage it"
    )


def test_an_untracked_file_in_a_subdirectory_is_read_the_same_way(repo: Path) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "deep.txt").write_text("x\n", encoding="utf-8")
    diff = git_diff_workspace(GitDiffQuery(REF, "pkg/deep.txt"), _session(repo))
    assert diff.text.splitlines()[1] == "+++ b/pkg/deep.txt"
    assert "+x" in diff.text.splitlines()


def test_an_empty_untracked_file_has_no_hunks(repo: Path) -> None:
    # Exactly what git reports for a new empty file: the headers, and nothing else.
    (repo / "blank.txt").write_text("", encoding="utf-8")
    diff = git_diff_workspace(GitDiffQuery(REF, "blank.txt"), _session(repo))
    assert diff.empty is True
    assert diff.text == ""


def test_an_untracked_binary_file_is_not_decoded(repo: Path) -> None:
    (repo / "blob.bin").write_bytes(bytes(range(256)))
    diff = git_diff_workspace(GitDiffQuery(REF, "blob.bin"), _session(repo))
    assert diff.binary is True
    assert diff.text == ""


def test_an_untracked_file_past_the_diff_cap_is_truncated(repo: Path) -> None:
    line = "x" * 99 + "\n"
    (repo / "big.txt").write_text(line * (MAX_DIFF_BYTES // 100 + 10), encoding="utf-8")
    diff = git_diff_workspace(GitDiffQuery(REF, "big.txt"), _session(repo))
    assert diff.truncated is True
    assert len(diff.text.encode("utf-8")) <= MAX_DIFF_BYTES


def test_an_untracked_directory_is_not_read_as_a_file(repo: Path) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "deep.txt").write_text("x\n", encoding="utf-8")
    diff = git_diff_workspace(GitDiffQuery(REF, "pkg"), _session(repo))
    assert diff.empty is True


def test_a_staged_new_file_comes_from_git_itself(repo: Path) -> None:
    # A file added to the index is diffable by git, so nothing is synthesized for it.
    (repo / "added.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "added.txt")
    staged = git_diff_workspace(GitDiffQuery(REF, "added.txt", staged=True), _session(repo))
    assert staged.empty is False
    assert "+one" in staged.text
    # The worktree side of a staged, unmodified file is empty, and stays empty: the
    # synthesized new-file view is only for a path git does not track at all.
    worktree = git_diff_workspace(GitDiffQuery(REF, "added.txt"), _session(repo))
    assert worktree.empty is True


def test_unsafe_paths_are_refused(repo: Path) -> None:
    for bad in ("../escape.txt", "/etc/passwd", "a\\b", "sub/../..", ""):
        with pytest.raises(InvalidRequestError):
            git_diff_workspace(GitDiffQuery(REF, bad), _session(repo))


def test_a_workspace_without_git_is_unavailable(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitUnavailableError):
        git_status_workspace(GitStatusQuery(REF), _session(plain))
    with pytest.raises(GitUnavailableError):
        git_diff_workspace(GitDiffQuery(REF, "a.txt"), _session(plain))


def test_a_missing_workspace_is_unavailable() -> None:
    with pytest.raises(GitUnavailableError):
        git_status_workspace(GitStatusQuery(REF), SimpleNamespace(workspace=None))


def test_wire_dispatch_decodes_git_requests_into_queries() -> None:
    """The transport decodes the params and calls the matching service method."""
    calls: list[Any] = []

    class Service:
        async def git_status(self, query: Any) -> str:
            calls.append(query)
            return "status"

        async def git_diff(self, query: Any) -> str:
            calls.append(query)
            return "diff"

    async def run() -> None:
        params = {"session": {"project_id": "p", "thread_id": "t"}}
        assert await dispatch(Service(), "runtime.git.status", params) == "status"
        assert (
            await dispatch(
                Service(), "runtime.git.diff", {**params, "path": "a.ts", "staged": True}
            )
            == "diff"
        )

    asyncio.run(run())
    assert [type(call) for call in calls] == [GitStatusQuery, GitDiffQuery]
    assert calls[1].path == "a.ts"
    assert calls[1].staged is True
    # The default is the worktree, not the index.
    assert GitDiffQuery(REF, "a.ts").staged is False


def test_wire_dispatch_refuses_bad_git_params_before_the_service() -> None:
    touched: list[Any] = []

    class Service:
        async def git_diff(self, query: Any) -> str:
            touched.append(query)
            return "diff"

    async def run() -> None:
        params = {"session": {"project_id": "p", "thread_id": "t"}}
        with pytest.raises(ProtocolError):
            await dispatch(Service(), "runtime.git.diff", params)  # path missing
        with pytest.raises(ProtocolError):
            await dispatch(
                Service(), "runtime.git.diff", {**params, "path": "a.ts", "staged": "yes"}
            )
        with pytest.raises(ProtocolError):
            await dispatch(
                Service(), "runtime.git.diff", {**params, "path": "a.ts", "extra": 1}
            )

    asyncio.run(run())
    assert touched == []
