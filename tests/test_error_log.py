from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from synapse.observability import error_log


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError(), ConnectionError()])
def test_empty_exception_summary_keeps_type(error: Exception) -> None:
    assert error_log.exception_summary(error) == type(error).__name__


def test_broken_exception_string_does_not_hide_original_type() -> None:
    class BrokenError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("formatting failed")

    assert error_log.exception_summary(BrokenError()) == "BrokenError"


@pytest.mark.parametrize(
    "message",
    [
        'Authorization: Bearer FIXTURE-CREDENTIAL',
        '{"api_key": "FIXTURE-CREDENTIAL", "message": "denied"}',
        "token=FIXTURE-CREDENTIAL",
        "Bearer FIXTURE-CREDENTIAL",
        "https://user:FIXTURE-CREDENTIAL@example.test/v1?token=FIXTURE-CREDENTIAL",
        "-----BEGIN PRIVATE KEY-----\nFIXTURE-CREDENTIAL\n-----END PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----\nFIXTURE-CREDENTIAL",
    ],
)
def test_display_redacts_credential_shapes(message: str) -> None:
    assert "FIXTURE-CREDENTIAL" not in error_log.safe_error_text(message)


def test_display_is_bounded_and_ignores_terminal_control_codes() -> None:
    summary = error_log.safe_error_text("\x1b[31mboom\n" + "x" * 20000)
    assert len(summary) <= 1000
    assert summary.startswith("boom ")
    assert "\x1b" not in summary
    assert "\n" not in summary


def test_log_keeps_stack_and_codes_not_messages_locals_or_source(tmp_path: Path) -> None:
    try:
        try:
            raise ValueError("PRIVATE-CAUSE")
        except ValueError as cause:
            raise PermissionError(13, "PRIVATE-BODY") from cause
    except PermissionError as error:
        report = error_log.record_error(
            tmp_path, operation="tui.submit", thread_id="t1", turn_id="turn1", error=error
        )
    assert report.path is not None
    text = report.path.read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["error_type"] == "PermissionError"
    assert record["errno"] == 13
    assert record["thread_id"] == "t1"
    assert record["turn_id"] == "turn1"
    assert "Traceback" in record["stack"]
    assert "test_log_keeps_stack_and_codes" in record["stack"]
    assert "ValueError" in record["stack"]
    assert "PRIVATE-BODY" not in text
    assert "PRIVATE-CAUSE" not in text
    assert "raise PermissionError" not in text


def test_result_has_summary_but_does_not_persist_arbitrary_body(tmp_path: Path) -> None:
    report = error_log.record_error(
        tmp_path, operation="tui.submit.result", detail="RuntimeError: PRIVATE-RESULT"
    )
    assert report.summary == "RuntimeError: PRIVATE-RESULT"
    assert report.path is not None
    text = report.path.read_text(encoding="utf-8")
    assert "PRIVATE-RESULT" not in text
    assert json.loads(text)["error_type"] == "RuntimeError"


def test_log_rotation_is_bounded_and_json_records_stay_valid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(error_log, "_MAX_LOG_BYTES", 700)
    for _ in range(40):
        report = error_log.record_error(tmp_path, operation="test", error=TimeoutError())
        assert report.path is not None
    files = list((tmp_path / ".synapse" / "logs").glob("errors-*.log*"))
    assert len(files) == 4
    for path in files:
        assert path.stat().st_size <= 700
        for line in path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["error_type"] == "TimeoutError"


def test_parallel_writes_do_not_mix_records(tmp_path: Path) -> None:
    def write(index: int) -> None:
        report = error_log.record_error(
            tmp_path, operation="parallel", thread_id=str(index), error=RuntimeError()
        )
        assert report.path is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(50)))
    files = list((tmp_path / ".synapse" / "logs").glob("errors-*.log"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(records) == 50
    assert {item["thread_id"] for item in records} == {str(i) for i in range(50)}


def test_unwritable_log_is_nonfatal_and_reported(tmp_path: Path) -> None:
    (tmp_path / ".synapse").write_text("block directory creation", encoding="utf-8")
    report = error_log.record_error(tmp_path, operation="tui.submit", error=TimeoutError())
    assert report.summary == "TimeoutError"
    assert report.path is None
    assert report.log_error


def test_no_workspace_does_not_write_to_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    report = error_log.record_error(None, operation="test", error=RuntimeError())
    assert report.log_error == "workspace unavailable"
    assert not (tmp_path / ".synapse").exists()
