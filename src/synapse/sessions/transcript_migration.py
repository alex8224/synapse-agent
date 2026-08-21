"""Isolated legacy transcript projection migration.

A legacy checkpoint may require deserializing the complete message history once.
Doing that inside the long-lived TUI raises CPython/Windows allocator high-water
memory even after the objects are released. The migration therefore runs in a
short-lived child process and writes only compact events to ``transcript.sqlite``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_TIMEOUT_SECONDS = 300.0
_MAX_ERROR_CHARS = 2_000


@dataclass(frozen=True)
class TranscriptMigrationResult:
    success: bool
    error: str | None = None


def projection_needs_rebuild(
    projection: Any,
    thread_id: str,
    checkpoint_path: Path | str,
) -> bool:
    """True when the projection is missing or older than the checkpoint.

    The LangGraph checkpoint is the source of truth. ``contains_thread`` alone
    is not enough: a crash mid-turn advances the checkpoint while the derived
    projection stays at the older turn count. Compare the stored source
    checkpoint id against the newest id without deserializing messages.
    """
    if not projection.contains_thread(thread_id):
        return True
    from synapse.sessions.transcript import latest_checkpoint_id_from_sqlite_file

    latest = latest_checkpoint_id_from_sqlite_file(checkpoint_path, thread_id)
    if latest is None:
        return False
    return projection.source_checkpoint_id(thread_id) != latest


def migrate_transcript_projection(
    *,
    checkpoint_path: Path | str,
    projection_path: Path | str,
    thread_id: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> TranscriptMigrationResult:
    """Build one legacy thread projection in a disposable child process."""
    checkpoint = Path(checkpoint_path).expanduser().resolve()
    projection = Path(projection_path).expanduser().resolve()
    if not checkpoint.is_file():
        return TranscriptMigrationResult(False, f"checkpoint database not found: {checkpoint}")
    if getattr(sys, "frozen", False):
        # A PyInstaller binary is the Typer CLI, not a Python interpreter:
        # ``-m`` becomes ``--model`` and ``--worker`` is rejected. Route
        # through a hidden CLI subcommand that runs the same worker body.
        command = [
            sys.executable,
            "transcript-migration-worker",
            "--checkpoint-path",
            str(checkpoint),
            "--projection-path",
            str(projection),
            "--thread-id",
            str(thread_id),
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "synapse.sessions.transcript_migration",
            "--worker",
            "--checkpoint-path",
            str(checkpoint),
            "--projection-path",
            str(projection),
            "--thread-id",
            str(thread_id),
        ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout)),
            check=False,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return TranscriptMigrationResult(
            False,
            f"transcript migration timed out after {float(timeout):.0f}s",
        )
    except OSError as exc:
        return TranscriptMigrationResult(False, f"transcript migration failed: {exc}")
    if completed.returncode == 0:
        return TranscriptMigrationResult(True)
    error = (completed.stderr or completed.stdout or "worker failed").strip()
    if len(error) > _MAX_ERROR_CHARS:
        error = error[-_MAX_ERROR_CHARS:]
    return TranscriptMigrationResult(False, error)


def _run_worker(
    *,
    checkpoint_path: Path,
    projection_path: Path,
    thread_id: str,
) -> int:
    """Worker entry: all full-history allocations die with this process."""
    from synapse.sessions.transcript import (
        latest_checkpoint_id_from_sqlite_file,
        load_messages_from_sqlite_file,
    )
    from synapse.sessions.transcript_projection import TranscriptProjection

    projection = TranscriptProjection(projection_path)
    try:
        # Skip only when the projection already matches the newest checkpoint.
        # A crash mid-turn leaves the checkpoint ahead of the projection, so a
        # missing ``source_checkpoint_id`` (or a stale one) must trigger a
        # rebuild from the source of truth instead of reusing the old data.
        if not projection_needs_rebuild(projection, thread_id, checkpoint_path):
            return 0
        observed_id = projection.source_checkpoint_id(thread_id)
        snapshot_id = None
        messages: list[Any] = []
        for _ in range(3):
            snapshot_id = latest_checkpoint_id_from_sqlite_file(checkpoint_path, thread_id)
            messages = load_messages_from_sqlite_file(checkpoint_path, thread_id)
            latest_id = latest_checkpoint_id_from_sqlite_file(checkpoint_path, thread_id)
            if latest_id == snapshot_id:
                break
        else:
            # A continuously advancing checkpoint cannot produce a coherent
            # snapshot in this worker. Report a retryable migration failure
            # instead of claiming success with a stale projection.
            return 2
        # Compare-and-swap: the slow message load above can race a concurrent
        # turn that appends to the projection. Only overwrite when the
        # projection watermark is still the value we observed; otherwise a
        # concurrent append already advanced it and we must not clobber it.
        projection.replace_from_messages(
            thread_id,
            messages,
            source_checkpoint_id=snapshot_id,
            expected_source_checkpoint_id=observed_id,
            require_match=True,
        )
        return 0
    finally:
        projection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--checkpoint-path", type=Path)
    parser.add_argument("--projection-path", type=Path)
    parser.add_argument("--thread-id")
    return parser


def run_transcript_migration_worker(
    *,
    checkpoint_path: Path,
    projection_path: Path,
    thread_id: str,
) -> int:
    """Run the worker and return a process exit code.

    Shared by the ``-m`` module CLI and the packaged Typer CLI so source and
    frozen installs execute the identical worker body.
    """
    try:
        return _run_worker(
            checkpoint_path=checkpoint_path,
            projection_path=projection_path,
            thread_id=thread_id,
        )
    except Exception as exc:  # noqa: BLE001 - worker boundary reports a compact error
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.worker:
        return 2
    if args.checkpoint_path is None or args.projection_path is None or not args.thread_id:
        return 2
    return run_transcript_migration_worker(
        checkpoint_path=args.checkpoint_path,
        projection_path=args.projection_path,
        thread_id=str(args.thread_id),
    )


if __name__ == "__main__":
    raise SystemExit(main())
