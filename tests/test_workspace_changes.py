"""Unit tests for per-turn workspace changes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from synapse.runtime.workspace_changes import (
    MAX_SNAPSHOT_FILES,
    SnapshotFile,
    WorkspaceSnapshot,
    changes_between,
    snapshot_workspace,
)


def _file(
    path: str,
    content: str | None,
    *,
    status: str = "M",
    lines: int | None = None,
    binary: bool = False,
) -> SnapshotFile:
    return SnapshotFile(
        path=path,
        status=status,
        # No content means no content to compare: the identity is the file's own.
        digest=content if content is not None else f"stat:{path}",
        lines=lines if lines is not None else (None if content is None
                                              else len(content.splitlines())),
        content=content,
        binary=binary,
    )


def _repository(tmp_path: Path) -> Path:
    """A workspace holding one committed, two-line file."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "tracked.py").write_text("one\ntwo\n", encoding="utf-8")
    git("add", "tracked.py")
    git("commit", "-q", "-m", "init")
    return tmp_path


def _commit(workspace: Path, message: str) -> None:
    subprocess.run(
        ["git", "commit", "-q", "-am", message],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def test_a_new_file_is_all_insertions() -> None:
    before = WorkspaceSnapshot(files={})
    after = WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n", status="??")})
    changes, total = changes_between(before, after)
    assert total == 1
    assert [(c.path, c.status, c.insertions, c.deletions) for c in changes] == [
        ("a.py", "added", 2, 0)
    ]


def test_a_deleted_file_is_all_deletions() -> None:
    before = WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\nthree\n", status="M")})
    changes, total = changes_between(before, WorkspaceSnapshot(files={}))
    assert total == 1
    assert [(c.status, c.insertions, c.deletions) for c in changes] == [("deleted", 0, 3)]


def test_an_edit_counts_this_turns_lines_only() -> None:
    # The turn replaced one line and added two: the counts are the delta between the two
    # versions, not the file's standing difference against HEAD.
    before = WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")})
    after = WorkspaceSnapshot(files={"a.py": _file("a.py", "one\nTWO\nthree\nfour\n")})
    changes, _ = changes_between(before, after)
    assert [(c.status, c.insertions, c.deletions) for c in changes] == [("modified", 3, 1)]


def test_an_unchanged_file_is_not_a_change() -> None:
    same = _file("a.py", "one\ntwo\n")
    changes, total = changes_between(
        WorkspaceSnapshot(files={"a.py": same}), WorkspaceSnapshot(files={"a.py": same})
    )
    assert (changes, total) == ((), 0)


def test_a_file_that_changed_twice_still_reports_one_net_delta() -> None:
    # A turn may write a file three times; the card is one entry with the net effect.
    before = WorkspaceSnapshot(files={"a.py": _file("a.py", "start\n")})
    after = WorkspaceSnapshot(files={"a.py": _file("a.py", "start\nend\n")})
    changes, total = changes_between(before, after)
    assert total == 1
    assert changes[0].insertions == 1


def test_a_file_whose_versions_were_not_kept_says_so() -> None:
    # Too large, binary or unreadable: the change is reported, the counts are not
    # invented.  (`binary` is the payload's "no line counts to report".)
    # A different identity (the file's size and mtime moved) with no content to diff.
    before = WorkspaceSnapshot(files={"big.bin": _file("big.bin", None, binary=True)})
    after_file = _file("big.bin", None, binary=True)
    after = WorkspaceSnapshot(files={"big.bin": SnapshotFile(
        path=after_file.path, status=after_file.status, digest="stat:big.bin:2",
        lines=None, content=None, binary=True,
    )})
    changes, _ = changes_between(before, after)
    assert [(c.status, c.binary, c.insertions, c.deletions) for c in changes] == [
        ("modified", True, 0, 0)
    ]


def test_the_list_is_bounded_and_reports_the_total() -> None:
    before = WorkspaceSnapshot(files={})
    after = WorkspaceSnapshot(
        files={f"f{i}.py": _file(f"f{i}.py", "x\n" * (i + 1), status="??") for i in range(10)}
    )
    changes, total = changes_between(before, after, limit=3)
    assert total == 10, 'the count is how many files changed'
    assert len(changes) == 3
    # The substantial files lead, so a truncated list is still the useful part of it.
    assert [c.path for c in changes] == ["f9.py", "f8.py", "f7.py"]


def test_a_snapshot_reads_a_real_workspace(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "tracked.py").write_text("one\ntwo\n", encoding="utf-8")
    git("add", "tracked.py")
    git("commit", "-q", "-m", "init")

    clean = snapshot_workspace(tmp_path)
    assert clean is not None
    assert clean.files == {}, 'a clean workspace has nothing to report'

    (tmp_path / "tracked.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "fresh.py").write_text("hello\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    after = snapshot_workspace(tmp_path)
    assert after is not None
    assert set(after.files) == {"tracked.py", "fresh.py", "blob.bin"}

    changes, total = changes_between(clean, after)
    assert total == 3
    by_path = {change.path: change for change in changes}
    # `tracked.py` was clean when the turn started, so the turn's delta is that file's
    # standing delta against HEAD (nothing else could have changed it since).
    assert (by_path["tracked.py"].status, by_path["tracked.py"].insertions) == ("modified", 1)
    assert by_path["tracked.py"].deletions == 0
    assert (by_path["fresh.py"].status, by_path["fresh.py"].insertions) == ("added", 1)
    assert by_path["blob.bin"].binary is True, 'a binary file has no line counts'
    assert (tmp_path / "tracked.py").read_text(encoding="utf-8") == "one\ntwo\nthree\n", (
        'the snapshot must not write to the repository'
    )


def test_a_snapshot_of_something_that_is_not_a_repository_is_none(tmp_path: Path) -> None:
    assert snapshot_workspace(tmp_path) is None


def test_the_snapshot_bounds_how_many_files_it_describes(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    for index in range(MAX_SNAPSHOT_FILES + 5):
        (tmp_path / f"f{index}.py").write_text("x\n", encoding="utf-8")
    snapshot = snapshot_workspace(tmp_path)
    assert snapshot is not None
    assert len(snapshot.files) == MAX_SNAPSHOT_FILES
    assert snapshot.truncated is True


def test_the_snapshot_leaves_the_git_index_untouched(tmp_path: Path) -> None:
    """Describing the workspace must not write to the repository it only describes.

    `git status` normally refreshes the index's cached stat information and writes the index
    back.  The snapshot's git calls run with ``GIT_OPTIONAL_LOCKS=0`` precisely so that the
    bookkeeping of a turn cannot modify the repository.
    """
    workspace = _repository(tmp_path)
    # A tracked file whose content no longer matches the index's cached stat information:
    # this is what makes `git status` want to write the index, which is the write ruled out.
    (workspace / "tracked.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    index = workspace / ".git" / "index"
    before = index.stat()

    snapshot = snapshot_workspace(workspace)

    assert snapshot is not None
    after = index.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size), (
        'the snapshot refreshed and rewrote .git/index'
    )


def test_a_truncated_snapshot_does_not_call_a_file_the_turns_own(tmp_path: Path) -> None:
    """A path a truncated before-snapshot dropped is not evidence of a new file."""
    before = WorkspaceSnapshot(files={}, truncated=True)
    after = WorkspaceSnapshot(files={"late.py": _file("late.py", "x\n", status="??")})

    changes, total = changes_between(before, after)

    assert (total, len(changes)) == (1, 1)
    assert changes[0].path == "late.py"
    assert changes[0].status == "modified", (
        'a truncated snapshot cannot say the turn created the file'
    )
    assert changes[0].insertions == 1, 'the counts are unchanged by the label'


def test_a_commit_inside_a_turn_is_not_a_deletion(tmp_path: Path) -> None:
    """A turn that commits what it found leaves every file exactly as it was."""
    workspace = _repository(tmp_path)
    (workspace / "tracked.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    before = snapshot_workspace(workspace)
    assert before is not None
    _commit(workspace, "the turn commits its own work")

    # The tree is clean now, so no file is dirty any more -- which is not the same as every
    # file having been deleted.  The carried path is what tells the two apart.
    after = snapshot_workspace(workspace, carry=before.files)
    assert after is not None
    assert set(after.files) == {"tracked.py"}, "the carried path is still described"
    changes, total = changes_between(before, after)
    assert (changes, total) == ((), 0)
    assert (workspace / "tracked.py").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_a_carried_path_that_is_gone_from_disk_is_a_deletion(tmp_path: Path) -> None:
    """A deletion the turn also commits is still a deletion, and still all of it."""
    workspace = _repository(tmp_path)
    (workspace / "tracked.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    before = snapshot_workspace(workspace)
    assert before is not None
    (workspace / "tracked.py").unlink()
    _commit(workspace, "delete it")

    after = snapshot_workspace(workspace, carry=before.files)
    assert after is not None
    changes, total = changes_between(before, after)
    assert total == 1
    assert [(c.path, c.status, c.insertions, c.deletions) for c in changes] == [
        ("tracked.py", "deleted", 0, 3)
    ]


def test_a_file_the_turn_deletes_is_all_deletions(tmp_path: Path) -> None:
    workspace = _repository(tmp_path)
    (workspace / "tracked.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    before = snapshot_workspace(workspace)
    assert before is not None
    (workspace / "tracked.py").unlink()

    after = snapshot_workspace(workspace, carry=before.files)
    assert after is not None
    changes, total = changes_between(before, after)
    assert total == 1
    assert [(c.status, c.insertions, c.deletions) for c in changes] == [("deleted", 0, 3)]


def test_a_file_the_turn_deletes_that_was_clean_is_all_deletions(tmp_path: Path) -> None:
    # Nothing was kept for it: the turn's delta is the file's standing delta against `HEAD`,
    # which for a file that was clean is exactly this turn's deletion.
    workspace = _repository(tmp_path)
    before = snapshot_workspace(workspace)
    assert before is not None
    (workspace / "tracked.py").unlink()

    after = snapshot_workspace(workspace, carry=before.files)
    assert after is not None
    changes, total = changes_between(before, after)
    assert total == 1
    assert [(c.status, c.insertions, c.deletions) for c in changes] == [("deleted", 0, 2)]
