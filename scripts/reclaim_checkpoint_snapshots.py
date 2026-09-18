"""Reclaim disk space by dropping redundant ``DeltaChannel`` snapshots.

Background
----------
``DeltaChannel`` normally stores nothing in a checkpoint blob: the channel value
is rebuilt from the nearest ancestor snapshot (``seed``) plus every ``writes``
row after it (``langgraph/checkpoint/sqlite/_delta.py``).  Periodically, LangGraph
materialises the full accumulated value into ``channel_values[<channel>]`` as a
``_DeltaSnapshot`` blob -- that is what makes ``checkpoints.sqlite`` large.

Measured on a real store: 753 snapshot rows (0.70% of rows) held 70.5% of the
bytes, because each snapshot carries the whole message list at that moment and
``deepagents`` sets ``snapshot_frequency=50`` (LangGraph's default is 1000).

Reconstruction only ever uses the **nearest** ancestor snapshot, so the older
ones are dead weight unless a historical checkpoint is read back (time travel).

Hard constraint
---------------
Every thread must keep its **newest** snapshot per channel.  Verified
empirically: for compression-heavy threads (context summarisation writes an
``Overwrite``), ``writes`` alone is not a faithful change log and replaying it
from an empty or much older seed produces *different* message content -- silently,
with no error.  Keeping the newest snapshot is always correct and costs nothing
in load latency (the nearest snapshot is used anyway).

Safety
------
* Refuses to run while another process holds the database (``--force`` overrides).
* Refuses to run without a backup unless ``--no-backup`` is passed.
* Rewrites blobs only; never deletes rows (that would break the
  ``parent_checkpoint_id`` chain).
* Verifies a per-thread content fingerprint before and after; any mismatch is
  reported loudly and the run exits non-zero.
* ``--dry-run`` reports the plan without touching anything.

Normally this runs as step 2 of ``scripts/compact_synapse_db.ps1``, which owns the
occupancy check, the optional backup and the VACUUM step:

    pwsh -File scripts/compact_synapse_db.ps1 -DryRun
    pwsh -File scripts/compact_synapse_db.ps1 -SnapshotBackup

It also works standalone (``--force`` skips its own occupancy probe):

    uv run --no-sync python scripts/reclaim_checkpoint_snapshots.py --dry-run
    uv run --no-sync python scripts/reclaim_checkpoint_snapshots.py --force --no-backup
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path

SNAPSHOT_TYPE = "_DeltaSnapshot"
BUSY_TIMEOUT_MS = 120_000


def _format_size(num_bytes: float) -> str:
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:.2f} GB"
    return f"{num_bytes / 1024**2:.1f} MB"


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def assert_exclusive(path: Path) -> None:
    """Fail when another process is holding a write lock on the database."""
    conn = sqlite3.connect(str(path), timeout=0)
    try:
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            f"数据库被占用: {path}\n"
            f"  请先完全退出 Synapse TUI / synapse-runtime / synapse-web-console，"
            f"或加 --force 跳过检查。\n  原因: {exc}"
        ) from exc
    finally:
        conn.close()


def load_serde():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer()


def scan_snapshots(conn: sqlite3.Connection, channels: list[str], serde) -> dict:
    """Return ``{thread_id: {channel: [(checkpoint_id, size), ...]}}`` ascending."""
    found: dict[str, dict[str, list[tuple[str, int]]]] = {}
    cursor = conn.execute(
        "SELECT thread_id, checkpoint_id, checkpoint FROM checkpoints "
        "ORDER BY thread_id, checkpoint_id"
    )
    for thread_id, checkpoint_id, blob in cursor:
        try:
            obj = serde.loads_typed(("msgpack", blob))
        except Exception:  # noqa: BLE001 - an unreadable blob is left untouched
            continue
        values = obj.get("channel_values")
        if not isinstance(values, dict):
            continue
        for channel in channels:
            if type(values.get(channel)).__name__ == SNAPSHOT_TYPE:
                found.setdefault(thread_id, {}).setdefault(channel, []).append(
                    (checkpoint_id, len(blob))
                )
    return found


def fingerprint(db_path: Path, thread_id: str, serde) -> str:
    """Content hash of the messages reconstructed through the real load path."""
    from synapse.sessions.transcript import load_messages_from_sqlite_file

    messages = load_messages_from_sqlite_file(db_path, thread_id)
    digest = hashlib.sha256()
    for message in messages:
        try:
            digest.update(serde.dumps_typed(message)[1])
        except Exception:  # noqa: BLE001 - fall back to a stable repr
            digest.update(repr(message).encode("utf-8", "replace"))
    return f"{len(messages)}:{digest.hexdigest()[:16]}"


def strip_snapshots(conn: sqlite3.Connection, targets: list[tuple[str, str, str]], serde) -> int:
    """Remove ``channel_values[channel]`` from the given checkpoints, in place."""
    updates = []
    for thread_id, checkpoint_id, channel in targets:
        row = conn.execute(
            "SELECT checkpoint FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ?",
            (thread_id, checkpoint_id),
        ).fetchone()
        if row is None:
            continue
        obj = serde.loads_typed(("msgpack", row[0]))
        values = obj.get("channel_values")
        if not isinstance(values, dict) or channel not in values:
            continue
        values.pop(channel, None)
        updates.append((sqlite3.Binary(serde.dumps_typed(obj)[1]), thread_id, checkpoint_id))
    with conn:
        conn.executemany(
            "UPDATE checkpoints SET checkpoint = ? WHERE thread_id = ? AND checkpoint_id = ?",
            updates,
        )
    return len(updates)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="删除冗余的 DeltaChannel 快照，回收 checkpoints.sqlite 空间。"
    )
    parser.add_argument(
        "--synapse-dir",
        default=str(Path.cwd() / ".synapse"),
        help=".synapse 目录，默认 <当前目录>/.synapse",
    )
    parser.add_argument(
        "--channels",
        default="messages",
        help="要处理的通道，逗号分隔，默认 messages",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=1,
        help="每个线程每个通道保留最新的 N 个快照，默认 1（不要设为 0）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不修改")
    parser.add_argument("--force", action="store_true", help="跳过占用检查")
    parser.add_argument("--no-backup", action="store_true", help="确认没有备份也继续（危险）")
    parser.add_argument("--skip-verify", action="store_true", help="跳过指纹校验")
    args = parser.parse_args()

    if args.keep < 1:
        print("ERROR: --keep 必须 >= 1（每线程必须保留最新快照）", file=sys.stderr)
        return 2

    synapse_dir = Path(args.synapse_dir).expanduser().resolve()
    db_path = synapse_dir / "checkpoints.sqlite"
    if not db_path.is_file():
        print(f"ERROR: 找不到 {db_path}", file=sys.stderr)
        return 2

    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    serde = load_serde()

    print(f"数据库: {db_path}")
    print(f"通道:   {', '.join(channels)}   每线程保留最新 {args.keep} 个快照")

    if args.dry_run:
        print("模式:   DRY-RUN（不会修改任何数据）")
    else:
        if not args.force:
            assert_exclusive(db_path)
            print("占用检查: 通过")
        else:
            print("占用检查: 已用 --force 跳过")
        backup = db_path.with_suffix(db_path.suffix + ".bak")
        if not backup.is_file() and not args.no_backup:
            print(
                f"ERROR: 未找到备份 {backup}\n"
                f"  请先执行: Copy-Item '{db_path}' '{backup}'\n"
                f"  或确认不需要备份后加 --no-backup。\n"
                f"  注意：本操作不可逆。",
                file=sys.stderr,
            )
            return 2
        print(f"备份检查: {'已找到 ' + backup.name if backup.is_file() else '已跳过'}")

    before_size = db_path.stat().st_size
    conn = _open(db_path)
    try:
        found = scan_snapshots(conn, channels, serde)

        targets: list[tuple[str, str, str]] = []
        kept: list[tuple[str, str, str]] = []
        kept_bytes = 0
        for thread_id, per_channel in found.items():
            for channel, rows in per_channel.items():
                for checkpoint_id, _size in rows[: -args.keep]:
                    targets.append((thread_id, checkpoint_id, channel))
                for checkpoint_id, size in rows[-args.keep :]:
                    kept.append((thread_id, checkpoint_id, channel))
                    kept_bytes += size

        snap_bytes = sum(
            size
            for per_channel in found.values()
            for rows in per_channel.values()
            for _, size in rows
        )
        print()
        print(
            f"快照总数:   {sum(len(r) for pc in found.values() for r in pc.values())} 个 "
            f"/ {_format_size(snap_bytes)}"
        )
        print(f"涉及线程:   {len(found)} 个")
        print(f"保留:       {len(kept)} 个 / {_format_size(kept_bytes)}")
        reclaimable = _format_size(snap_bytes - kept_bytes)
        print(f"待删除:     {len(targets)} 个（预计回收约 {reclaimable}）")

        if not targets:
            print("\n没有可回收的快照。")
            return 0

        if args.dry_run:
            print("\nDRY-RUN 结束。")
            return 0

        verify_threads = sorted({t for t, _c, _ch in targets})
        print(f"\n校验基线（{len(verify_threads)} 个线程）...")
        started = time.perf_counter()
        before = {t: fingerprint(db_path, t, serde) for t in verify_threads}
        print(f"  完成，用时 {time.perf_counter() - started:.1f}s")

        print("改写快照...")
        changed = strip_snapshots(conn, targets, serde)
        print(f"  已改写 {changed} 行")
    finally:
        conn.close()

    if not args.skip_verify:
        print(f"校验结果（{len(verify_threads)} 个线程）...")
        mismatched = []
        for thread_id in verify_threads:
            if fingerprint(db_path, thread_id, serde) != before[thread_id]:
                mismatched.append(thread_id)
        if mismatched:
            print()
            print("ERROR: 以下线程重建内容不一致，请立即用备份回滚：", file=sys.stderr)
            for thread_id in mismatched:
                print(f"  - {thread_id}", file=sys.stderr)
            return 1
        print("  全部一致")

    conn = _open(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    after_size = db_path.stat().st_size
    print()
    print("结果")
    print(f"  主文件: {_format_size(before_size)} -> {_format_size(after_size)}")
    print("  提示: 空闲页需要 VACUUM 才会真正释放。请先退出 Synapse，再执行：")
    print("        pwsh -File scripts/compact_synapse_db.ps1 -NoPurge")
    print()
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
