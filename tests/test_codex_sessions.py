"""Tests for read-only Codex session discovery."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from synapse.codex_sessions import CodexSessionScanner

ID_ONE = "11111111-1111-1111-1111-111111111111"
ID_TWO = "22222222-2222-2222-2222-222222222222"


def _rollout_path(home: Path, native_id: str, *, compressed: bool = False) -> Path:
    suffix = ".jsonl.zst" if compressed else ".jsonl"
    filename = f"rollout-2026-03-20T12-00-00-{native_id}{suffix}"
    path = home / "sessions" / "2026" / "03" / "20" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_rollout(
    path: Path,
    workspace: Path,
    *,
    title: str = "Inspect scanner",
    source: str = "cli",
) -> None:
    records = [
        {
            "type": "session_meta",
            "payload": {"id": path.name, "cwd": str(workspace), "source": source},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": title},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")


def _create_state_db(home: Path, rows: list[tuple[str, Path, Path, str, int]]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "state_12.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE threads (
                id TEXT,
                rollout_path TEXT,
                updated_at_ms INTEGER,
                source TEXT,
                cwd TEXT,
                title TEXT,
                first_user_message TEXT,
                archived INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            [
                (native_id, str(rollout), updated_at, "cli", str(workspace), title, "fallback")
                for native_id, rollout, workspace, title, updated_at in rows
            ],
        )


def test_scanner_reads_matching_state_db_session_readonly(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE)
    other_rollout = _rollout_path(home, ID_TWO)
    _write_rollout(rollout, workspace)
    _write_rollout(other_rollout, other_workspace)
    _create_state_db(
        home,
        [
            (ID_ONE, rollout, workspace, "State DB title", 1_774_000_000_000),
            (ID_TWO, other_rollout, other_workspace, "Other workspace", 1_775_000_000_000),
        ],
    )

    result = CodexSessionScanner(home).scan(workspace)

    assert result.discovery == "state_db"
    assert [session.native_id for session in result.sessions] == [ID_ONE]
    assert result.sessions[0].title == "State DB title"
    assert result.sessions[0].source == "cli"
    assert result.sessions[0].rollout_path == rollout.resolve()
    assert result.sessions[0].fingerprint

    with sqlite3.connect(home / "state_12.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 2


def test_scanner_lists_all_supported_workspaces_when_unfiltered(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE)
    other_rollout = _rollout_path(home, ID_TWO)
    _write_rollout(rollout, workspace)
    _write_rollout(other_rollout, other_workspace)
    _create_state_db(
        home,
        [
            (ID_ONE, rollout, workspace, "Current workspace", 1_774_000_000_000),
            (ID_TWO, other_rollout, other_workspace, "Other workspace", 1_775_000_000_000),
        ],
    )

    result = CodexSessionScanner(home).scan()

    assert [session.native_id for session in result.sessions] == [ID_TWO, ID_ONE]
    assert [session.cwd for session in result.sessions] == [
        other_workspace.resolve(),
        workspace.resolve(),
    ]


def test_scanner_supplements_state_db_with_rollout_headers_when_requested(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_rollout = _rollout_path(home, ID_ONE)
    fallback_rollout = _rollout_path(home, ID_TWO)
    _write_rollout(state_rollout, workspace, title="State DB session")
    _write_rollout(fallback_rollout, workspace, title="Supplemented rollout session")
    _create_state_db(
        home,
        [(ID_ONE, state_rollout, workspace, "State DB session", 1_774_000_000_000)],
    )

    default = CodexSessionScanner(home).scan(workspace)
    supplemented = CodexSessionScanner(home).scan(workspace, include_rollout_fallback=True)

    assert [session.native_id for session in default.sessions] == [ID_ONE]
    assert {session.native_id for session in supplemented.sessions} == {ID_ONE, ID_TWO}
    supplemented_session = next(
        session for session in supplemented.sessions if session.native_id == ID_TWO
    )
    assert supplemented_session.title == "Supplemented rollout session"
    assert CodexSessionScanner(home).inspect(
        ID_TWO,
        workspace=workspace,
        include_rollout_fallback=True,
    ) is not None


def test_scanner_falls_back_to_rollout_headers_when_state_db_is_missing(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE)
    _write_rollout(rollout, workspace, title="Header title")

    result = CodexSessionScanner(home).scan(workspace)

    assert result.discovery == "rollout_headers"
    assert [session.native_id for session in result.sessions] == [ID_ONE]
    assert result.sessions[0].title == "Header title"
    assert result.sessions[0].source == "cli"


def test_scanner_reads_compressed_rollout_headers(tmp_path: Path) -> None:
    import zstandard

    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE, compressed=True)
    records = [
        {"type": "session_meta", "payload": {"cwd": str(workspace), "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Compressed title"}},
    ]
    payload = "\n".join(map(json.dumps, records)).encode()
    rollout.write_bytes(zstandard.ZstdCompressor().compress(payload))

    result = CodexSessionScanner(home).scan(workspace)

    assert [session.native_id for session in result.sessions] == [ID_ONE]
    assert result.sessions[0].title == "Compressed title"


def test_scanner_reads_large_bounded_session_metadata_header(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE)
    metadata = {
        "type": "session_meta",
        "payload": {
            "cwd": str(workspace),
            "source": "cli",
            "base_instructions": "x" * (37 * 1024),
        },
    }
    event = {
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "Large header title"},
    }
    rollout.write_text("\n".join(map(json.dumps, (metadata, event))), encoding="utf-8")

    result = CodexSessionScanner(home).scan(workspace)

    assert [session.native_id for session in result.sessions] == [ID_ONE]
    assert result.sessions[0].title == "Large header title"


def test_scanner_skips_oversized_internal_record_before_user_title(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE)
    records = (
        {"type": "session_meta", "payload": {"cwd": str(workspace), "source": "cli"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "developer", "content": "x" * (80 * 1024)},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Title after large record"},
        },
    )
    rollout.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")

    result = CodexSessionScanner(home).scan(workspace)

    assert [session.native_id for session in result.sessions] == [ID_ONE]
    assert result.sessions[0].title == "Title after large record"


def test_scanner_warns_for_invalid_compressed_rollout(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    compressed = _rollout_path(home, ID_ONE, compressed=True)
    compressed.write_bytes(b"not valid zstd")

    result = CodexSessionScanner(home).scan(workspace)

    assert result.sessions == ()
    assert any("invalid zstd" in warning for warning in result.warnings)


def test_scanner_rejects_state_db_path_outside_codex_sessions(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / f"rollout-2026-03-20T12-00-00-{ID_ONE}.jsonl"
    outside.parent.mkdir(parents=True, exist_ok=True)
    _write_rollout(outside, workspace)
    _create_state_db(home, [(ID_ONE, outside, workspace, "Outside", 1_774_000_000_000)])

    result = CodexSessionScanner(home).scan(workspace)

    assert result.sessions == ()
    assert any("invalid rollout path" in warning for warning in result.warnings)


def test_scanner_filters_internal_title_text(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE)
    _write_rollout(rollout, workspace)
    _create_state_db(
        home,
        [(ID_ONE, rollout, workspace, "<environment_context> secret metadata", 1_774_000_000_000)],
    )

    result = CodexSessionScanner(home).scan(workspace)

    assert result.sessions[0].title == "fallback"


def test_scanner_state_db_query_filters_before_row_limit(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE)
    _write_rollout(rollout, workspace)
    other_rollout = _rollout_path(home, ID_TWO)
    _write_rollout(other_rollout, other_workspace)
    rows = [(ID_ONE, rollout, workspace, "Matching", 1_000)]
    rows.extend(
        (ID_TWO, other_rollout, other_workspace, "Other", updated_at)
        for updated_at in range(2_000, 7_100)
    )
    _create_state_db(home, rows)

    result = CodexSessionScanner(home).scan(workspace)

    assert [session.native_id for session in result.sessions] == [ID_ONE]
    assert not any("row limit" in warning for warning in result.warnings)


def test_scanner_fallback_rejects_unknown_source(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rollout = _rollout_path(home, ID_ONE)
    _write_rollout(rollout, workspace, source="untrusted")

    result = CodexSessionScanner(home).scan(workspace)

    assert result.sessions == ()


def test_scanner_rejects_symlinked_sessions_root(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    outside = tmp_path / "outside"
    workspace = tmp_path / "workspace"
    home.mkdir()
    outside.mkdir()
    workspace.mkdir()
    (home / "sessions").symlink_to(outside, target_is_directory=True)

    result = CodexSessionScanner(home).scan(workspace)

    assert result.discovery == "none"
    assert any("symlink" in warning for warning in result.warnings)
