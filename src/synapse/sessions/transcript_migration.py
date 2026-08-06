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

_DEFAULT_TIMEOUT_SECONDS = 300.0
_MAX_ERROR_CHARS = 2_000


@dataclass(frozen=True)
class TranscriptMigrationResult:
    success: bool
    error: str | None = None


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
    from synapse.sessions.transcript import load_messages_from_sqlite_file
    from synapse.sessions.transcript_projection import TranscriptProjection

    projection = TranscriptProjection(projection_path)
    try:
        # Another worker/TUI turn may have populated this thread while the child
        # was starting. Never overwrite newer incremental projection data.
        if projection.contains_thread(thread_id):
            return 0
        messages = load_messages_from_sqlite_file(checkpoint_path, thread_id)
        projection.replace_from_messages(thread_id, messages)
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.worker:
        return 2
    if args.checkpoint_path is None or args.projection_path is None or not args.thread_id:
        return 2
    try:
        return _run_worker(
            checkpoint_path=args.checkpoint_path,
            projection_path=args.projection_path,
            thread_id=str(args.thread_id),
        )
    except Exception as exc:  # noqa: BLE001 - worker boundary reports a compact error
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
