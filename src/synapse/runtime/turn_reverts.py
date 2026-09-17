"""Undoing one turn's change to one file.

A turn's change card offers to put a single file back the way that turn found it.  That
needs the file's content *before* the turn, and nothing else on disk still holds it: the
workspace has moved on, and the standing delta against `HEAD` is not this turn's.  So the
content is kept -- bounded, in the workspace's own ``.synapse/`` state directory beside
the session database -- and a revert is a plain file write (or delete) from that copy.

Three rules keep this narrow, because this is the only part of the console that writes to
the reader's own files:

* it restores exactly one path, and only a path that turn is reported to have changed;
* it refuses unless the file still holds exactly what the turn left there, so a later
  turn's edit -- or the reader's own -- is never silently discarded;
* it never touches `HEAD`, the index, or any other file.  Git is only ever read.

A file that was clean when the turn started has no kept version; its pre-turn content is
the one `HEAD` named then, and that is what a revert puts back -- but only while `HEAD`
still names that commit, so a commit made since is never undone by surprise.

Nothing here is a policy: the caller decides when to record and when to revert.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from synapse.runtime.workspace_changes import GIT_TIMEOUT_S, MAX_FILE_BYTES, WorkspaceSnapshot

#: The state directory the workspace already keeps its sessions and attachments in.
STATE_DIRNAME = ".synapse"
#: Where one workspace's per-turn pre-change copies live, under that state directory.
RECORDS_DIRNAME = "turn-snapshots"
#: The on-disk shape of one record.  Version 2 records that the record's "the file was not
#: there" and "the file was clean" conclusions came from a snapshot known to be complete.
RECORD_VERSION = 2
#: Versions this module still reads.  A version 1 record was written before that
#: completeness was checked, so those two conclusions are read back as "unknown" (see
#: `_parse_file`): a revert then refuses instead of deleting or overwriting a file that the
#: snapshot it was built from never described.
_READABLE_VERSIONS = (1, RECORD_VERSION)

#: How many turns one thread keeps revertable, newest first.  A revert is an undo of
#: recent work, not an archive: beyond this the record is gone and a revert is refused.
MAX_RECORDS_PER_THREAD = 20
#: How much pre-change content one thread keeps in total.  The first records that fit are
#: kept, oldest pruned first, so the store is bounded by bytes and not only by count.
MAX_THREAD_BYTES = 32 * 1024 * 1024
#: How large a file may be to take part in a revert.  Reading it is how drift is detected,
#: so a file past this is refused rather than read.
MAX_CURRENT_BYTES = 8 * 1024 * 1024

#: Where a file's pre-turn content comes from.
BEFORE_CONTENT = "content"  #: kept from the turn's own snapshot
BEFORE_ABSENT = "absent"  #: the turn created the file
BEFORE_HEAD = "head"  #: it was clean at turn start, so `HEAD` held its content
BEFORE_UNKNOWN = "unknown"  #: not kept, and not provably `HEAD`'s: not revertable

#: What a revert did (or would do).
ACTION_RESTORE = "restore"
ACTION_DELETE = "delete"
ACTION_ALREADY = "already_reverted"

#: Stable refusal reasons.  A client maps these to its own wording and shows the message
#: as a fallback, so a refusal is never a bare "failed".
REASON_RECORD_EXPIRED = "record_expired"
REASON_PATH_NOT_IN_TURN = "path_not_in_turn"
REASON_PATH_INVALID = "path_invalid"
REASON_BEFORE_UNKNOWN = "before_unknown"
REASON_NO_BEFORE_CONTENT = "no_before_content"
REASON_CONTENT_NOT_KEPT = "content_not_kept"
REASON_CONTENT_DRIFT = "content_drift"
REASON_HEAD_MOVED = "head_moved"
REASON_SYMLINK_REFUSED = "symlink_refused"
REASON_NOT_A_FILE = "not_a_file"
REASON_TOO_LARGE = "too_large"
REASON_WRITE_FAILED = "write_failed"
REASON_WORKSPACE_UNAVAILABLE = "workspace_unavailable"

#: One path segment's worth of a record file name.  A thread or turn id reaches this
#: module from the wire and becomes a file name, so anything outside this set is refused
#: before it is ever joined to a path.
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_NAME_BYTES = 4096


class TurnRevertRefused(Exception):
    """A revert was refused.  ``reason`` is one of the ``REASON_*`` codes."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _digest(content: str | bytes) -> str:
    """SHA-256 of the file's own bytes, so two versions compare without a diff."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _safe_name(value: object) -> str:
    """One id safe to use as a file name, or ``TurnRevertRefused``."""
    if not isinstance(value, str) or not _SAFE_NAME.match(value) or ".." in value:
        raise TurnRevertRefused(REASON_PATH_INVALID, "session or turn id is not a safe name")
    return value


def _workspace_root(workspace: Path | str) -> Path:
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TurnRevertRefused(
            REASON_WORKSPACE_UNAVAILABLE, "the workspace directory is unavailable"
        ) from exc
    if not root.is_dir():
        raise TurnRevertRefused(
            REASON_WORKSPACE_UNAVAILABLE, "the workspace directory is unavailable"
        )
    return root


def _resolve_target(workspace: Path, path: str) -> Path:
    """The absolute target of one workspace-relative POSIX path, or a refusal.

    The path is validated as a relative POSIX path with no parent segments, and its
    resolved parent directory must still be inside the workspace, so a symlinked
    directory cannot aim a write somewhere else.
    """
    if not isinstance(path, str) or path == "" or len(path.encode("utf-8")) > _NAME_BYTES:
        raise TurnRevertRefused(REASON_PATH_INVALID, "the file path is required")
    if "\\" in path or path.startswith("/") or ":" in path:
        raise TurnRevertRefused(REASON_PATH_INVALID, "the file path must be workspace-relative")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise TurnRevertRefused(
            REASON_PATH_INVALID, "the file path must not contain parent segments"
        )
    target = workspace.joinpath(*pure.parts)
    try:
        parent = target.parent.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TurnRevertRefused(REASON_PATH_INVALID, "the file path cannot be resolved") from exc
    if parent != workspace and workspace not in parent.parents:
        raise TurnRevertRefused(REASON_PATH_INVALID, "the file path escapes the workspace")
    return target


# -- the record ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BeforeState:
    """What the file held when the turn started."""

    kind: str
    #: The content itself, when it was kept.
    content: str | None = None
    #: Digest of that content, so "already back where it started" is recognisable.
    digest: str | None = None

    @property
    def revertable(self) -> bool:
        return self.kind in {BEFORE_CONTENT, BEFORE_ABSENT, BEFORE_HEAD}


@dataclass(frozen=True, slots=True)
class AfterState:
    """What the turn left behind."""

    present: bool
    #: Digest of the content the turn left, or `None` when it was not kept -- and then
    #: drift cannot be detected, so a revert is refused rather than guessed at.
    digest: str | None = None
    lines: int | None = None


@dataclass(frozen=True, slots=True)
class RevertRecordFile:
    """One file of one turn, with everything a revert needs to stay safe."""

    path: str
    status: str
    before: BeforeState
    after: AfterState
    #: Set once this file was reverted, so the card can say so after a reload.
    reverted_action: str | None = None
    reverted_at: float | None = None

    @property
    def reverted(self) -> bool:
        return self.reverted_action is not None


@dataclass(frozen=True, slots=True)
class TurnRevertRecord:
    """One turn's pre-change copies, as persisted."""

    thread_id: str
    turn_id: str
    created_at: float
    files: tuple[RevertRecordFile, ...]
    #: The commit `HEAD` named when the turn started.
    head: str | None = None

    def file(self, path: str) -> RevertRecordFile | None:
        for entry in self.files:
            if entry.path == path:
                return entry
        return None

    def reverted_paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.files if entry.reverted)


@dataclass(frozen=True, slots=True)
class RevertPlan:
    """What a revert of one file will do, decided before anything is written."""

    path: str
    action: str
    #: The content to write for ``restore``; `None` for ``delete``.
    content: str | None
    #: The digest the file is expected to hold right now (``after``), when it is checked.
    expected_digest: str | None
    #: The digest the file will hold afterwards, when it is known.
    result_digest: str | None


def build_record(
    *,
    thread_id: str,
    turn_id: str,
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
    changes: tuple[Any, ...],
    created_at: float | None = None,
) -> TurnRevertRecord:
    """The pre-change state of every file a turn is reported to have changed.

    ``changes`` is the turn's own bounded report, so exactly the files a card shows become
    revertable -- a turn that rewrote a whole tree reports a prefix of it, and the rest is
    simply not offered for undo.

    A file the snapshot kept is stored as it stood.  A file the turn created is stored as
    "did not exist".  A file that was *clean* when the turn started has no kept version:
    its pre-turn content is the one `HEAD` named then, recorded as such and resolved when
    a revert actually runs.  Anything else -- binary, over budget, an unreadable
    snapshot -- is recorded as not revertable rather than guessed at.

    A snapshot that was truncated cannot say which files were there at all, so none of the
    above is inferred from a path merely being *missing* from it: such a file is recorded
    as not revertable.  Reading that absence as "the turn created it" would let a revert
    delete a file the turn never made, and as "clean at `HEAD`" it would overwrite one.

    What the turn left behind is read from the second snapshot's own `present`, not from
    whether the path appears in it: a file the turn *deleted* is still described there, and
    calling it present would make a revert refuse -- "the file is gone" -- instead of
    putting it back.
    """
    files: list[RevertRecordFile] = []
    for change in changes:
        path = str(getattr(change, "path", ""))
        status = str(getattr(change, "status", ""))
        if not path:
            continue
        was = before.files.get(path)
        now = after.files.get(path)
        if was is not None and was.content is not None:
            head_state = BeforeState(
                kind=BEFORE_CONTENT, content=was.content, digest=_digest(was.content)
            )
        elif was is None and before.paths_complete and status == "added":
            # Absent from a *complete* snapshot is the only proof that the file was not
            # there: the turn created it.  The completeness test comes first, so a truncated
            # snapshot -- which dropped paths it never looked at -- can never reach this
            # conclusion, and a revert can never delete a file the turn did not make.
            head_state = BeforeState(kind=BEFORE_ABSENT)
        elif was is None and before.paths_complete and not before.content_skipped:
            # Absent from a complete snapshot means clean at the turn's start, so this
            # commit's version of the file is what the turn found.
            head_state = BeforeState(kind=BEFORE_HEAD)
        else:
            head_state = BeforeState(kind=BEFORE_UNKNOWN)
        after_state = AfterState(
            present=now is not None and now.present,
            digest=_digest(now.content) if now is not None and now.content is not None else None,
            lines=None if now is None else now.lines,
        )
        files.append(
            RevertRecordFile(path=path, status=status, before=head_state, after=after_state)
        )
    return TurnRevertRecord(
        thread_id=thread_id,
        turn_id=turn_id,
        created_at=time.time() if created_at is None else created_at,
        files=tuple(files),
        head=before.head,
    )


def mark_reverted(
    record: TurnRevertRecord, path: str, action: str, *, at: float | None = None
) -> TurnRevertRecord:
    """The same record with one file marked as reverted."""
    stamp = time.time() if at is None else at
    files = tuple(
        replace(entry, reverted_action=action, reverted_at=stamp) if entry.path == path else entry
        for entry in record.files
    )
    return replace(record, files=files)


# -- persistence --------------------------------------------------------------


def record_dir(workspace: Path | str, thread_id: str) -> Path:
    """The directory one thread's records live in (``<workspace>/.synapse/...``)."""
    return Path(workspace) / STATE_DIRNAME / RECORDS_DIRNAME / _safe_name(thread_id)


def _record_name(created_at: float, turn_id: str) -> str:
    """``<sortable stamp>-<turn id>.json``, so pruning never has to parse a record."""
    return f"{int(created_at * 1000):013d}-{_safe_name(turn_id)}.json"


def _record_files(directory: Path) -> list[Path]:
    try:
        return sorted(
            (entry for entry in directory.iterdir() if entry.suffix == ".json" and entry.is_file()),
            key=lambda entry: entry.name,
        )
    except OSError:
        return []


def record_path(workspace: Path | str, thread_id: str, turn_id: str) -> Path | None:
    """The stored record of one turn, or `None` when there is none."""
    directory = record_dir(workspace, thread_id)
    wanted = f"{_safe_name(turn_id)}.json"
    for entry in _record_files(directory):
        # The name is ``<stamp>-<turn id>.json``, and the stamp holds no dash, so the
        # first dash separates them.  Matching on the tail alone would let one turn id
        # that ends another's pick up the wrong record.
        _, _, tail = entry.name.partition("-")
        if tail == wanted:
            return entry
    return None


def save_record(workspace: Path | str, record: TurnRevertRecord) -> bool:
    """Persist one record, pruning older ones; never raises.

    This runs on the turn's own thread at settlement, where a failure must not fail the
    turn: an unwritable state directory simply means the turn's changes cannot be undone
    later, and the turn still reports them.
    """
    try:
        directory = record_dir(workspace, record.thread_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(_record_json(record), ensure_ascii=False, separators=(",", ":"))
        target = directory / _record_name(record.created_at, record.turn_id)
        handle, temp_name = tempfile.mkstemp(dir=str(directory), prefix=".record-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        _prune(directory)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _prune(directory: Path) -> None:
    """Keep the newest ``MAX_RECORDS_PER_THREAD`` records within ``MAX_THREAD_BYTES``."""
    entries = _record_files(directory)
    sizes: dict[Path, int] = {}
    total = 0
    for entry in entries:
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        sizes[entry] = size
        total += size
    excess = len(entries) - MAX_RECORDS_PER_THREAD
    for entry in entries:
        if excess <= 0 and total <= MAX_THREAD_BYTES:
            break
        try:
            entry.unlink()
        except OSError:
            continue
        total -= sizes[entry]
        excess -= 1


def _record_json(record: TurnRevertRecord) -> dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "thread_id": record.thread_id,
        "turn_id": record.turn_id,
        "created_at": record.created_at,
        "head": record.head,
        "files": [
            {
                "path": entry.path,
                "status": entry.status,
                "before": {
                    "kind": entry.before.kind,
                    "content": entry.before.content,
                    "digest": entry.before.digest,
                },
                "after": {
                    "present": entry.after.present,
                    "digest": entry.after.digest,
                    "lines": entry.after.lines,
                },
                "reverted": (
                    None
                    if not entry.reverted
                    else {"action": entry.reverted_action, "at": entry.reverted_at}
                ),
            }
            for entry in record.files
        ],
    }


def _parse_file(raw: object, *, absence_proven: bool) -> RevertRecordFile | None:
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    status = raw.get("status")
    before = raw.get("before")
    after = raw.get("after")
    if not isinstance(path, str) or not path or not isinstance(before, dict):
        return None
    if not isinstance(after, dict):
        return None
    kind = before.get("kind")
    content = before.get("content")
    digest = before.get("digest")
    if not isinstance(kind, str) or kind not in {
        BEFORE_CONTENT,
        BEFORE_ABSENT,
        BEFORE_HEAD,
        BEFORE_UNKNOWN,
    }:
        return None
    if kind == BEFORE_CONTENT and (not isinstance(content, str) or not isinstance(digest, str)):
        return None
    if not isinstance(after.get("present"), bool):
        return None
    after_digest = after.get("digest")
    if after_digest is not None and not isinstance(after_digest, str):
        return None
    lines = after.get("lines")
    if lines is not None and not isinstance(lines, int):
        lines = None
    reverted = raw.get("reverted")
    action: str | None = None
    at: float | None = None
    if isinstance(reverted, dict):
        raw_action = reverted.get("action")
        raw_at = reverted.get("at")
        if isinstance(raw_action, str) and raw_action in {ACTION_RESTORE, ACTION_DELETE}:
            action = raw_action
        if isinstance(raw_at, (int, float)):
            at = float(raw_at)
    if not absence_proven and kind in {BEFORE_ABSENT, BEFORE_HEAD}:
        # The record predates the completeness check: "not in the snapshot" was read both as
        # "not on disk" and as "clean at `HEAD`", and one truncated snapshot makes both
        # wrong.  Kept content is still trusted; these conclusions are not, so the file
        # becomes not revertable rather than deletable or overwritable.
        kind = BEFORE_UNKNOWN
        content = None
        digest = None
    return RevertRecordFile(
        path=path,
        status=status if isinstance(status, str) else "",
        before=BeforeState(
            kind=kind,
            content=content if isinstance(content, str) else None,
            digest=digest if isinstance(digest, str) else None,
        ),
        after=AfterState(
            present=bool(after.get("present")),
            digest=after_digest if isinstance(after_digest, str) else None,
            lines=lines,
        ),
        reverted_action=action,
        reverted_at=at,
    )


def load_record(workspace: Path | str, thread_id: str, turn_id: str) -> TurnRevertRecord | None:
    """One turn's record, or `None` when it is gone or unreadable.

    A record that cannot be read is reported as absent, never as an error: history must
    render whether or not a revert is still possible.
    """
    try:
        target = record_path(workspace, thread_id, turn_id)
        if target is None:
            return None
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TurnRevertRefused):
        return None
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    if version not in _READABLE_VERSIONS:
        return None
    # Only a record written since the completeness check can be trusted to have *proved*
    # that a file it does not describe was not there.
    absence_proven = version == RECORD_VERSION
    if raw.get("thread_id") != thread_id or raw.get("turn_id") != turn_id:
        return None
    raw_files = raw.get("files")
    if not isinstance(raw_files, list):
        return None
    files: list[RevertRecordFile] = []
    for entry in raw_files:
        parsed = _parse_file(entry, absence_proven=absence_proven)
        if parsed is not None:
            files.append(parsed)
    created_at = raw.get("created_at")
    head = raw.get("head")
    return TurnRevertRecord(
        thread_id=thread_id,
        turn_id=turn_id,
        created_at=float(created_at) if isinstance(created_at, (int, float)) else 0.0,
        files=tuple(files),
        head=head if isinstance(head, str) else None,
    )


def load_reverted_paths(
    workspace: Path | str, thread_id: str, turn_ids: tuple[str, ...] | list[str]
) -> dict[str, tuple[str, ...]]:
    """``turn id -> reverted paths`` for the turns asked about.

    Only the turns a caller names are read, so a transcript page costs one small file per
    turn on screen.  A turn with no record, or one with nothing reverted, is simply
    absent from the result.
    """
    reverted: dict[str, tuple[str, ...]] = {}
    for turn_id in turn_ids:
        if not isinstance(turn_id, str) or not turn_id:
            continue
        record = load_record(workspace, thread_id, turn_id)
        if record is None:
            continue
        paths = record.reverted_paths()
        if paths:
            reverted[turn_id] = paths
    return reverted


# -- the revert itself --------------------------------------------------------


def _current_head(workspace: Path) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace").strip() or None


def _head_content(workspace: Path, commit: str, path: str) -> str | None:
    """The content one commit holds for one path, bounded, or `None` when it holds none.

    The size is asked for first so a large blob is refused rather than read into memory.
    """

    def _run(args: list[str]) -> bytes | None:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", *args],
                cwd=str(workspace),
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    spec = f"{commit}:{path}"
    raw_size = _run(["cat-file", "-s", spec])
    if raw_size is None:
        return None
    try:
        size = int(raw_size.decode("utf-8", errors="replace").strip())
    except ValueError:
        return None
    if size > MAX_FILE_BYTES or size > MAX_CURRENT_BYTES:
        raise TurnRevertRefused(
            REASON_TOO_LARGE, "the file is too large to restore from the commit"
        )
    raw = _run(["show", spec])
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True, slots=True)
class _CurrentFile:
    exists: bool
    digest: str | None
    symlink: bool
    regular: bool
    too_large: bool


def _inspect_current(target: Path) -> _CurrentFile:
    """What the file holds right now, without following a symlink to find out."""
    try:
        stat = target.lstat()
    except FileNotFoundError:
        return _CurrentFile(
            exists=False, digest=None, symlink=False, regular=False, too_large=False
        )
    except OSError as exc:
        raise TurnRevertRefused(
            REASON_CONTENT_DRIFT, "the file could not be read to check it is unchanged"
        ) from exc
    if stat.st_size > MAX_CURRENT_BYTES:
        return _CurrentFile(exists=True, digest=None, symlink=False, regular=False, too_large=True)
    is_symlink = target.is_symlink()
    regular = target.is_file() and not is_symlink
    if is_symlink or not regular:
        return _CurrentFile(
            exists=True, digest=None, symlink=is_symlink, regular=regular, too_large=False
        )
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise TurnRevertRefused(
            REASON_CONTENT_DRIFT, "the file could not be read to check it is unchanged"
        ) from exc
    return _CurrentFile(
        exists=True, digest=_digest(raw), symlink=False, regular=True, too_large=False
    )


def plan_revert(
    record: TurnRevertRecord,
    path: str,
    *,
    workspace: Path | str,
    head_now: str | None = None,
) -> RevertPlan:
    """Decide what reverting one file of one turn does, or refuse.

    Reads the workspace (the current file, and `HEAD` when the pre-turn content lives
    there) but writes nothing.  Every refusal is one of the ``REASON_*`` codes, and each
    says which condition failed rather than that "it did not work".
    """
    root = _workspace_root(workspace)
    entry = record.file(path)
    if entry is None:
        raise TurnRevertRefused(
            REASON_PATH_NOT_IN_TURN, "that file is not part of this turn's changes"
        )
    if not entry.before.revertable:
        raise TurnRevertRefused(
            REASON_BEFORE_UNKNOWN,
            "the file's content from before this turn was not kept, so it cannot be restored",
        )
    target = _resolve_target(root, path)
    current = _inspect_current(target)
    if current.symlink:
        raise TurnRevertRefused(
            REASON_SYMLINK_REFUSED, "the path is a symbolic link, so it is never written through"
        )
    if current.exists and not current.regular:
        raise TurnRevertRefused(REASON_NOT_A_FILE, "the path is not a regular file")
    if current.too_large:
        raise TurnRevertRefused(REASON_TOO_LARGE, "the file is too large to check it is unchanged")

    if entry.before.kind == BEFORE_ABSENT:
        if not current.exists:
            return RevertPlan(
                path=path,
                action=ACTION_ALREADY,
                content=None,
                expected_digest=None,
                result_digest=None,
            )
        if not entry.after.present or entry.after.digest is None:
            raise TurnRevertRefused(
                REASON_CONTENT_NOT_KEPT,
                "what the turn left in that file was not kept, so it cannot be checked",
            )
        if current.digest != entry.after.digest:
            raise TurnRevertRefused(
                REASON_CONTENT_DRIFT, "the file changed after this turn, so it was left alone"
            )
        return RevertPlan(
            path=path,
            action=ACTION_DELETE,
            content=None,
            expected_digest=entry.after.digest,
            result_digest=None,
        )

    if entry.before.kind == BEFORE_HEAD:
        if record.head is None or head_now is None:
            # Either the workspace had no commit when the turn started (nothing to restore
            # that file from) or it names none now: both mean the pre-turn content is gone.
            raise TurnRevertRefused(
                REASON_NO_BEFORE_CONTENT,
                "no commit holds this file's content from before the turn",
            )
        if head_now != record.head:
            raise TurnRevertRefused(
                REASON_HEAD_MOVED,
                "the workspace has a new commit since this turn, so restoring was refused",
            )
        content = _head_content(root, record.head, path)
        if content is None:
            raise TurnRevertRefused(
                REASON_NO_BEFORE_CONTENT,
                "that commit holds no readable text for this file, so it cannot be restored",
            )
        before_digest = _digest(content)
    else:
        content = entry.before.content or ""
        before_digest = entry.before.digest

    if entry.after.present:
        if not current.exists:
            raise TurnRevertRefused(REASON_CONTENT_DRIFT, "the file is gone, so it was left alone")
        if entry.after.digest is None:
            raise TurnRevertRefused(
                REASON_CONTENT_NOT_KEPT,
                "what the turn left in that file was not kept, so it cannot be checked",
            )
        if current.digest == before_digest:
            return RevertPlan(
                path=path,
                action=ACTION_ALREADY,
                content=None,
                expected_digest=entry.after.digest,
                result_digest=before_digest,
            )
        if current.digest != entry.after.digest:
            raise TurnRevertRefused(
                REASON_CONTENT_DRIFT, "the file changed after this turn, so it was left alone"
            )
        return RevertPlan(
            path=path,
            action=ACTION_RESTORE,
            content=content,
            expected_digest=entry.after.digest,
            result_digest=before_digest,
        )

    # The turn deleted the file: restoring it only makes sense while it is still gone.
    if current.exists:
        raise TurnRevertRefused(
            REASON_CONTENT_DRIFT, "the file was recreated after this turn, so it was left alone"
        )
    return RevertPlan(
        path=path,
        action=ACTION_RESTORE,
        content=content,
        expected_digest=None,
        result_digest=before_digest,
    )


def apply_plan(workspace: Path | str, plan: RevertPlan) -> int:
    """Carry out a plan on exactly one file; returns the bytes written.

    ``expected_digest`` is checked again here, immediately before anything is written, and
    not only where the plan was decided: the two are separate calls, and the file can move
    on in between -- the reader's own editor, a later turn, another tool.  A plan that no
    longer describes the file is refused instead of overwriting whatever arrived since.

    A restore is an atomic replace beside the target, so a reader never sees a half
    written file and a failure leaves the original in place.  Nothing else in the
    workspace, the index or `HEAD` is touched.

    What this cannot promise is a writer that races the check itself: the window between
    ``_verify_expected`` and the mutation is microseconds but not zero, so an edit landing
    exactly inside it is still lost.  Closing that would take a lock over the file that the
    workspace does not have; the workspace is treated as the reader's own.
    """
    if plan.action == ACTION_ALREADY:
        return 0
    root = _workspace_root(workspace)
    target = _resolve_target(root, plan.path)
    _verify_expected(target, plan.expected_digest)
    if plan.action == ACTION_DELETE:
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise TurnRevertRefused(
                REASON_WRITE_FAILED, f"the file could not be removed: {exc.strerror or exc}"
            ) from exc
        return 0
    content = plan.content or ""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise TurnRevertRefused(
            REASON_WRITE_FAILED, f"the file could not be written: {exc.strerror or exc}"
        ) from exc
    return len(content.encode("utf-8"))


def _verify_expected(target: Path, expected: str | None) -> None:
    """Refuse unless the file still holds exactly what the plan was decided against.

    ``expected`` is the digest the plan says the file holds right now, or `None` when the
    plan means "nothing is here" (a file this turn deleted, being put back).  Anything
    else -- gone, replaced, unreadable, no longer a plain file -- is drift, and drift is
    always answered with a refusal: this is the reader's own file.
    """
    current = _inspect_current(target)
    if current.symlink:
        raise TurnRevertRefused(
            REASON_SYMLINK_REFUSED, "the path is a symbolic link, so it is never written through"
        )
    if current.exists and not current.regular:
        raise TurnRevertRefused(REASON_NOT_A_FILE, "the path is not a regular file")
    if expected is None:
        if current.exists:
            raise TurnRevertRefused(
                REASON_CONTENT_DRIFT, "the file came back after this turn, so it was left alone"
            )
        return
    if not current.exists or current.too_large or current.digest != expected:
        raise TurnRevertRefused(
            REASON_CONTENT_DRIFT, "the file changed after this turn, so it was left alone"
        )


def revert_file(
    record: TurnRevertRecord,
    path: str,
    *,
    workspace: Path | str,
) -> tuple[str, int]:
    """Plan and carry out one file's revert: ``(action, bytes written)``.

    The caller persists the updated record; this function only touches the one file.
    """
    root = _workspace_root(workspace)
    plan = plan_revert(record, path, workspace=root, head_now=_current_head(root))
    written = apply_plan(root, plan)
    return plan.action, written
