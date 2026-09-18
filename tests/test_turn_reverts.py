"""Unit tests for reverting one turn's change to one file."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from synapse.runtime.turn_reverts import (
    ACTION_ALREADY,
    ACTION_DELETE,
    ACTION_RESTORE,
    BEFORE_ABSENT,
    BEFORE_CONTENT,
    BEFORE_HEAD,
    BEFORE_UNKNOWN,
    MAX_RECORDS_PER_THREAD,
    REASON_BEFORE_UNKNOWN,
    REASON_CONTENT_DRIFT,
    REASON_HEAD_MOVED,
    REASON_PATH_INVALID,
    REASON_PATH_NOT_IN_TURN,
    REASON_SYMLINK_REFUSED,
    TurnRevertRefused,
    apply_plan,
    build_record,
    load_record,
    load_reverted_paths,
    mark_reverted,
    plan_revert,
    revert_file,
    save_record,
)
from synapse.runtime.workspace_changes import (
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
    binary: bool = False,
    digest: str | None = None,
) -> SnapshotFile:
    # A kept file's digest is the digest of its bytes; a file whose content was not kept
    # falls back to the file's own identity, exactly as the snapshot records it.
    if digest is None:
        digest = (
            hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content is not None
            else f"stat:{path}"
        )
    return SnapshotFile(
        path=path,
        status=status,
        digest=digest,
        lines=None if content is None else len(content.splitlines()),
        content=content,
        binary=binary,
    )


def _record(
    tmp_path: Path,
    *,
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
    head: str | None = None,
    before_files: dict[str, SnapshotFile] | None = None,
):
    """A record for the turn that took `before` to `after`, over a real workspace."""
    before = WorkspaceSnapshot(
        files=before.files if before_files is None else before_files,
        standing=before.standing,
        truncated=before.truncated,
        head=before.head if head is None else head,
        content_skipped=before.content_skipped,
    )
    changes, _ = changes_between(before, after)
    return build_record(
        thread_id="t1",
        turn_id="turn-1",
        before=before,
        after=after,
        changes=changes,
        created_at=1000.0,
    )


def test_a_modified_file_keeps_its_pre_turn_content(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    entry = record.file("a.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_CONTENT
    assert entry.before.content == "one\n"
    assert entry.after.present is True
    assert entry.reverted is False


def test_a_created_file_is_recorded_as_absent_before(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}),
        after=WorkspaceSnapshot(files={"new.py": _file("new.py", "hi\n", status="??")}),
    )
    entry = record.file("new.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_ABSENT
    assert entry.before.content is None


def test_a_file_that_was_clean_before_the_turn_restores_from_the_commit(tmp_path: Path) -> None:
    # The snapshot only holds *changed* files, so a file the turn found clean has no kept
    # version: its pre-turn content is the one `HEAD` named.
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}, head="cafe"),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    entry = record.file("a.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_HEAD
    assert record.head == "cafe"


def test_a_binary_file_is_not_revertable(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(
            files={"b.bin": _file("b.bin", None, binary=True, digest="stat:b.bin:1")}
        ),
        after=WorkspaceSnapshot(
            files={"b.bin": _file("b.bin", None, binary=True, digest="stat:b.bin:2")}
        ),
    )
    entry = record.file("b.bin")
    assert entry is not None
    assert entry.before.kind == BEFORE_UNKNOWN
    assert entry.before.revertable is False


def test_a_truncated_snapshot_makes_a_missing_file_unknown(tmp_path: Path) -> None:
    # "Absent from the snapshot" only means "clean at turn start" when the snapshot was
    # complete: a truncated one never saw the file at all.
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}, truncated=True, head="cafe"),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
    )
    entry = record.file("a.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_UNKNOWN


def test_a_truncated_snapshot_never_authorizes_a_delete(tmp_path: Path) -> None:
    """A path the truncated before-snapshot never listed may have been there all along.

    The before-snapshot stops at ``MAX_SNAPSHOT_FILES``, so a file it dropped looks exactly
    like a file the turn created.  Reading that as "absent before" would let a revert delete
    a file the turn never made, so the record must refuse instead.
    """
    (tmp_path / "late.py").write_bytes(b"mine\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}, truncated=True),
        after=WorkspaceSnapshot(files={"late.py": _file("late.py", "mine\n", status="??")}),
    )
    entry = record.file("late.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_UNKNOWN, (
        'absence from a truncated snapshot is not proof the turn created the file'
    )
    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(record, "late.py", workspace=tmp_path)
    assert caught.value.reason == REASON_BEFORE_UNKNOWN
    assert (tmp_path / "late.py").read_bytes() == b"mine\n", (
        'a file the turn never made must survive the revert'
    )


def test_a_created_file_is_still_revertable_when_another_file_was_not_kept(
    tmp_path: Path,
) -> None:
    """`content_skipped` is about some other file's content, not this path's existence."""
    (tmp_path / "new.py").write_bytes(b"hi\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}, content_skipped=True),
        after=WorkspaceSnapshot(files={"new.py": _file("new.py", "hi\n", status="??")}),
    )
    entry = record.file("new.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_ABSENT, (
        "a file elsewhere whose content was not kept must not make this turn's own file "
        "undeletable"
    )
    assert revert_file(record, "new.py", workspace=tmp_path) == (ACTION_DELETE, 0)
    assert not (tmp_path / "new.py").exists()


def test_reverting_a_modified_file_writes_the_pre_turn_content(tmp_path: Path) -> None:
    # Bytes, not text: the snapshot's digest is over the file's own bytes, and a text-mode
    # write would translate the newlines on this platform.
    (tmp_path / "a.py").write_bytes(b"one\ntwo\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    action, written = revert_file(record, "a.py", workspace=tmp_path)
    assert action == ACTION_RESTORE
    assert written == 4
    assert (tmp_path / "a.py").read_bytes() == b"one\n"


def test_reverting_a_created_file_deletes_it(tmp_path: Path) -> None:
    (tmp_path / "new.py").write_bytes(b"hi\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}),
        after=WorkspaceSnapshot(files={"new.py": _file("new.py", "hi\n", status="??")}),
    )
    action, written = revert_file(record, "new.py", workspace=tmp_path)
    assert (action, written) == (ACTION_DELETE, 0)
    assert not (tmp_path / "new.py").exists()


def test_reverting_a_deleted_file_puts_it_back(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"gone.py": _file("gone.py", "kept\n")}),
        after=WorkspaceSnapshot(files={}),
    )
    action, written = revert_file(record, "gone.py", workspace=tmp_path)
    assert action == ACTION_RESTORE
    assert written == 5
    assert (tmp_path / "gone.py").read_bytes() == b"kept\n"


def test_a_file_changed_since_the_turn_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"one\nedited later\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(record, "a.py", workspace=tmp_path)
    assert caught.value.reason == REASON_CONTENT_DRIFT
    assert (tmp_path / "a.py").read_bytes() == b"one\nedited later\n", (
        "a refused revert must not touch the file"
    )


def test_a_file_the_turn_left_deleted_is_left_alone_when_it_came_back(tmp_path: Path) -> None:
    (tmp_path / "gone.py").write_bytes(b"recreated\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"gone.py": _file("gone.py", "kept\n")}),
        after=WorkspaceSnapshot(files={}),
    )
    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(record, "gone.py", workspace=tmp_path)
    assert caught.value.reason == REASON_CONTENT_DRIFT
    assert (tmp_path / "gone.py").read_bytes() == b"recreated\n"


def test_a_plan_is_refused_when_the_file_moved_on_after_it_was_decided(tmp_path: Path) -> None:
    """Planning and writing are two calls, so the plan's expectation is checked at the write."""
    (tmp_path / "a.py").write_bytes(b"one\ntwo\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    plan = plan_revert(record, "a.py", workspace=tmp_path)
    assert plan.action == ACTION_RESTORE

    (tmp_path / "a.py").write_bytes(b"one\ntwo\nthree\n")

    with pytest.raises(TurnRevertRefused) as caught:
        apply_plan(tmp_path, plan)
    assert caught.value.reason == REASON_CONTENT_DRIFT
    assert (tmp_path / "a.py").read_bytes() == b"one\ntwo\nthree\n", (
        'a plan that no longer describes the file must not be written'
    )


def test_a_plan_to_delete_is_refused_when_the_content_changed(tmp_path: Path) -> None:
    (tmp_path / "new.py").write_bytes(b"hi\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}),
        after=WorkspaceSnapshot(files={"new.py": _file("new.py", "hi\n", status="??")}),
    )
    plan = plan_revert(record, "new.py", workspace=tmp_path)
    assert plan.action == ACTION_DELETE

    (tmp_path / "new.py").write_bytes(b"hi\nthere\n")

    with pytest.raises(TurnRevertRefused) as caught:
        apply_plan(tmp_path, plan)
    assert caught.value.reason == REASON_CONTENT_DRIFT
    assert (tmp_path / "new.py").read_bytes() == b"hi\nthere\n"


def test_a_plan_to_put_a_deleted_file_back_refuses_when_it_reappeared(tmp_path: Path) -> None:
    """The plan's expectation can also be "nothing is here", and that is checked too."""
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"gone.py": _file("gone.py", "kept\n")}),
        after=WorkspaceSnapshot(files={}),
    )
    plan = plan_revert(record, "gone.py", workspace=tmp_path)
    assert (plan.action, plan.expected_digest) == (ACTION_RESTORE, None)

    (tmp_path / "gone.py").write_bytes(b"someone else\n")

    with pytest.raises(TurnRevertRefused) as caught:
        apply_plan(tmp_path, plan)
    assert caught.value.reason == REASON_CONTENT_DRIFT
    assert (tmp_path / "gone.py").read_bytes() == b"someone else\n"


def test_reverting_twice_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"one\ntwo\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    assert revert_file(record, "a.py", workspace=tmp_path) == (ACTION_RESTORE, 4)
    action, written = revert_file(record, "a.py", workspace=tmp_path)
    assert (action, written) == (ACTION_ALREADY, 0)
    assert (tmp_path / "a.py").read_bytes() == b"one\n"


def test_a_path_that_is_not_part_of_the_turn_is_refused(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "two\n")}),
    )
    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(record, "elsewhere.py", workspace=tmp_path)
    assert caught.value.reason == REASON_PATH_NOT_IN_TURN


@pytest.mark.parametrize("path", ["../outside.py", "/abs.py", "a\\b.py", "a/../../b.py"])
def test_a_path_that_leaves_the_workspace_is_refused(tmp_path: Path, path: str) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n", status="??")}),
    )
    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(record, path, workspace=tmp_path)
    assert caught.value.reason in {REASON_PATH_INVALID, REASON_PATH_NOT_IN_TURN}


def test_a_symlink_is_never_written_through(tmp_path: Path) -> None:
    target = tmp_path / "real.py"
    target.write_bytes(b"one\ntwo\n")
    link = tmp_path / "a.py"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks here")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(record, "a.py", workspace=tmp_path)
    assert caught.value.reason == REASON_SYMLINK_REFUSED
    assert target.read_bytes() == b"one\ntwo\n", "the link target is untouched"


def test_a_file_that_was_not_kept_is_refused(tmp_path: Path) -> None:
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(
            files={"b.bin": _file("b.bin", None, binary=True, digest="stat:b.bin:1")}
        ),
        after=WorkspaceSnapshot(
            files={"b.bin": _file("b.bin", None, binary=True, digest="stat:b.bin:2")}
        ),
    )
    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(record, "b.bin", workspace=tmp_path)
    assert caught.value.reason == REASON_BEFORE_UNKNOWN


def test_the_commit_version_is_restored_when_the_commit_still_names_it(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "a.py").write_text("committed\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-q", "-m", "init")

    before = snapshot_workspace(tmp_path)
    assert before is not None
    (tmp_path / "a.py").write_text("committed\nedited\n", encoding="utf-8")
    after = snapshot_workspace(tmp_path)
    assert after is not None
    record = build_record(
        thread_id="t1",
        turn_id="turn-1",
        before=before,
        after=after,
        changes=changes_between(before, after)[0],
    )
    entry = record.file("a.py")
    assert entry is not None and entry.before.kind == BEFORE_HEAD

    action, written = revert_file(record, "a.py", workspace=tmp_path)
    assert action == ACTION_RESTORE
    assert written == len("committed\n")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "committed\n"


def test_the_commit_version_is_refused_once_the_commit_moved(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "a.py").write_text("committed\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-q", "-m", "init")
    before = snapshot_workspace(tmp_path)
    assert before is not None
    (tmp_path / "a.py").write_text("committed\nedited\n", encoding="utf-8")
    after = snapshot_workspace(tmp_path)
    assert after is not None
    record = build_record(
        thread_id="t1",
        turn_id="turn-1",
        before=before,
        after=after,
        changes=changes_between(before, after)[0],
    )
    # A commit made after the turn moves `HEAD`; the recorded version is no longer what the
    # turn found, so the revert refuses rather than undoing someone else's commit.
    git("add", "a.py")
    git("commit", "-q", "-m", "later")

    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(record, "a.py", workspace=tmp_path)
    assert caught.value.reason == REASON_HEAD_MOVED


def test_records_round_trip_through_the_state_directory(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}, head="cafe"),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    assert save_record(tmp_path, record) is True
    loaded = load_record(tmp_path, "t1", "turn-1")
    assert loaded is not None
    assert loaded.head == "cafe"
    assert loaded.files == record.files
    assert (tmp_path / ".synapse" / "turn-snapshots" / "t1").is_dir()


def _stored_record_as_version_1(tmp_path: Path) -> None:
    """Rewrite the one stored record into the shape the pre-fix code wrote."""
    directory = tmp_path / ".synapse" / "turn-snapshots" / "t1"
    stored = next(entry for entry in directory.iterdir())
    payload = json.loads(stored.read_text(encoding="utf-8"))
    payload["version"] = 1
    stored.write_text(json.dumps(payload), encoding="utf-8")


def test_a_record_from_before_the_completeness_check_refuses_a_delete(tmp_path: Path) -> None:
    """A version 1 record's "the file was not there" rests on an absence nothing proved."""
    (tmp_path / "new.py").write_bytes(b"hi\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={}),
        after=WorkspaceSnapshot(files={"new.py": _file("new.py", "hi\n", status="??")}),
    )
    assert save_record(tmp_path, record) is True
    _stored_record_as_version_1(tmp_path)

    loaded = load_record(tmp_path, "t1", "turn-1")

    assert loaded is not None, 'a version 1 record is still readable'
    entry = loaded.file("new.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_UNKNOWN
    with pytest.raises(TurnRevertRefused) as caught:
        revert_file(loaded, "new.py", workspace=tmp_path)
    assert caught.value.reason == REASON_BEFORE_UNKNOWN
    assert (tmp_path / "new.py").read_bytes() == b"hi\n"


def test_a_record_from_before_the_check_still_restores_kept_content(tmp_path: Path) -> None:
    """The narrow rule: only the absence-based conclusions are dropped, never kept content."""
    (tmp_path / "a.py").write_bytes(b"one\ntwo\n")
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\ntwo\n")}),
    )
    assert save_record(tmp_path, record) is True
    _stored_record_as_version_1(tmp_path)

    loaded = load_record(tmp_path, "t1", "turn-1")

    assert loaded is not None
    assert revert_file(loaded, "a.py", workspace=tmp_path) == (ACTION_RESTORE, 4)
    assert (tmp_path / "a.py").read_bytes() == b"one\n"


def _deleted_file_record(tmp_path: Path, *, dirty_before: bool):
    """A real repository, and the record of a turn that deleted one tracked file.

    The deleted file is still *described* by the second snapshot -- as a `D` entry with
    `present=False` -- which is what tells a deletion apart from a file that merely stopped
    being dirty.
    """

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    # Bytes, not text: a text-mode write translates the newlines on this platform, and the
    # snapshot keeps the file's own bytes.
    (tmp_path / "a.py").write_bytes(b"committed\n")
    git("add", "a.py")
    git("commit", "-q", "-m", "init")
    if dirty_before:
        # The reader had an uncommitted edit when the turn started, so it was kept.
        (tmp_path / "a.py").write_bytes(b"committed\nedited\n")
    before = snapshot_workspace(tmp_path)
    assert before is not None
    (tmp_path / "a.py").unlink()
    after = snapshot_workspace(tmp_path, carry=before.files)
    assert after is not None
    changes, _ = changes_between(before, after)
    assert [(change.path, change.status) for change in changes] == [("a.py", "deleted")]
    return build_record(
        thread_id="t1", turn_id="turn-1", before=before, after=after, changes=changes
    )


def test_a_file_the_turn_deleted_is_put_back_from_the_kept_content(tmp_path: Path) -> None:
    """Undoing a deletion restores what the file held: that is what the card promises."""
    record = _deleted_file_record(tmp_path, dirty_before=True)
    entry = record.file("a.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_CONTENT, 'the uncommitted edit was kept'
    assert entry.after.present is False, 'the turn left the file gone'

    action, written = revert_file(record, "a.py", workspace=tmp_path)

    assert action == ACTION_RESTORE
    assert written == len(b"committed\nedited\n")
    assert (tmp_path / "a.py").read_bytes() == b"committed\nedited\n"


def test_a_clean_file_the_turn_deleted_comes_back_from_the_commit(tmp_path: Path) -> None:
    record = _deleted_file_record(tmp_path, dirty_before=False)
    entry = record.file("a.py")
    assert entry is not None
    assert entry.before.kind == BEFORE_HEAD, 'a clean file is restored from `HEAD`'
    assert entry.after.present is False

    action, written = revert_file(record, "a.py", workspace=tmp_path)

    assert action == ACTION_RESTORE
    assert written == len(b"committed\n")
    assert (tmp_path / "a.py").read_bytes() == b"committed\n"


def test_a_missing_record_is_simply_absent(tmp_path: Path) -> None:
    assert load_record(tmp_path, "t1", "turn-1") is None
    assert load_reverted_paths(tmp_path, "t1", ["turn-1"]) == {}


def test_a_corrupt_record_is_treated_as_absent(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "two\n")}),
    )
    assert save_record(tmp_path, record) is True
    for entry in (tmp_path / ".synapse" / "turn-snapshots" / "t1").iterdir():
        entry.write_text("{not json", encoding="utf-8")
    assert load_record(tmp_path, "t1", "turn-1") is None


def test_only_the_newest_records_are_kept(tmp_path: Path) -> None:
    for index in range(MAX_RECORDS_PER_THREAD + 3):
        record = _record(
            tmp_path,
            before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
            after=WorkspaceSnapshot(files={"a.py": _file("a.py", "two\n")}),
        )
        record = build_record(
            thread_id="t1",
            turn_id=f"turn-{index}",
            before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
            after=WorkspaceSnapshot(files={"a.py": _file("a.py", "two\n")}),
            changes=changes_between(
                WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
                WorkspaceSnapshot(files={"a.py": _file("a.py", "two\n")}),
            )[0],
            created_at=1000.0 + index,
        )
        assert save_record(tmp_path, record) is True
    kept = sorted(
        entry.name for entry in (tmp_path / ".synapse" / "turn-snapshots" / "t1").iterdir()
    )
    assert len(kept) == MAX_RECORDS_PER_THREAD
    assert load_record(tmp_path, "t1", "turn-0") is None, "the oldest record was pruned"
    assert load_record(tmp_path, "t1", f"turn-{MAX_RECORDS_PER_THREAD + 2}") is not None


def test_reverted_paths_are_reported_per_turn(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        before=WorkspaceSnapshot(files={"a.py": _file("a.py", "one\n")}),
        after=WorkspaceSnapshot(files={"a.py": _file("a.py", "two\n")}),
    )
    assert save_record(tmp_path, record) is True
    assert load_reverted_paths(tmp_path, "t1", ["turn-1"]) == {}
    assert save_record(tmp_path, mark_reverted(record, "a.py", ACTION_RESTORE, at=2.0)) is True
    assert load_reverted_paths(tmp_path, "t1", ["turn-1", "turn-2"]) == {"turn-1": ("a.py",)}
