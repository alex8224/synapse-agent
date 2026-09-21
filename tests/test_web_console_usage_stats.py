"""Tests for the web-console usage analytics aggregator.

Every case builds a real ``.synapse`` SQLite fixture using the producer's own
``ModelRequestCompressionEvent`` shape, so the aggregation is verified against
the exact event format ``model_request_compression_middleware`` writes:

* empty workspaces report real zeros, never a fabricated baseline;
* the ``today``/``7d``/``30d``/``all``/``custom`` windows are UTC closed
  intervals and filter every panel;
* cumulative throughput is ``provider_input + output`` while net input is
  ``provider_input - cache_read - cache_write``, and reasoning is never added on
  top of provider output;
* the activity heatmap is a continuous (gap-filled) calendar strip, capped to
  the last 366 days of a very wide window without truncating the totals;
* more than 5000 events are all counted (the read streams, it never truncates);
* sessions are ranked by real token totals and tool records are labelled as a
  partial view, not "all calls";
* invalid parameters raise ``UsageStatsError`` (the host maps this to HTTP 400).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.tool_output.models import ModelRequestCompressionEvent
from synapse.web_console.usage_stats import UsageStatsError, _git_state, get_usage_statistics

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

_TOOL_SCHEMA = """
CREATE TABLE model_request_compression_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE tool_output_refs (
    ref TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_SESSION_SCHEMA = """
CREATE TABLE sessions (
    thread_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT
);
"""


def _event(
    *,
    request_id: str,
    provider_input: int,
    cache_read: int = 0,
    cache_write: int = 0,
    output: int = 0,
    saved: int = 0,
    model: str = "gpt-5",
    turn_id: str = "turn-1",
    reasoning: int = 0,
    duration_ms: float = 0.0,
) -> dict:
    """A producer-shaped event dict (``ModelRequestCompressionEvent.as_dict()``)."""
    event = ModelRequestCompressionEvent(
        request_id=request_id,
        provider="openai",
        api_style="responses",
        auth_mode="payg",
        model=model,
        input_tokens_before=provider_input + saved,
        input_tokens_after=provider_input,
        provider_input_tokens=provider_input,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        uncached_input_tokens=max(0, provider_input - cache_read - cache_write),
        output_tokens=output,
        total_saved_tokens=saved,
        turn_id=turn_id,
        content_breakdown={"reasoning": reasoning} if reasoning else {},
        duration_ms=duration_ms,
    )
    return event.as_dict()


def _build_workspace(
    tmp_path: Path,
    *,
    events: list[dict] | None = None,
    refs: list[tuple] | None = None,
    sessions: list[tuple] | None = None,
) -> Path:
    ws = tmp_path / "ws"
    synapse = ws / ".synapse"
    synapse.mkdir(parents=True)
    con = sqlite3.connect(synapse / "tool-outputs.sqlite")
    try:
        con.executescript(_TOOL_SCHEMA)
        for row in events or []:
            con.execute(
                "INSERT INTO model_request_compression_events"
                "(request_id, thread_id, event_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    row["event"]["request_id"],
                    row["thread_id"],
                    json.dumps(row["event"], ensure_ascii=False),
                    row["created_at"],
                ),
            )
        for ref in refs or []:
            con.execute(
                "INSERT INTO tool_output_refs"
                "(ref, thread_id, checkpoint_ns, tool_call_id, "
                "tool_name, status, sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ref,
            )
        con.commit()
    finally:
        con.close()
    if sessions is not None:
        scon = sqlite3.connect(synapse / "sessions.sqlite")
        try:
            scon.executescript(_SESSION_SCHEMA)
            scon.executemany(
                "INSERT INTO sessions"
                "(thread_id, title, model, created_at, updated_at, tags_json, summary)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                sessions,
            )
            scon.commit()
        finally:
            scon.close()
    return ws


# --- empty / missing stores --------------------------------------------------


def test_missing_databases_report_real_zeros(tmp_path: Path) -> None:
    ws = tmp_path / "empty"
    ws.mkdir()
    stats = get_usage_statistics(ws, now=NOW)
    kpi = stats["kpi"]
    assert kpi["total_tokens"] == 0
    assert kpi["call_count"] == 0
    assert kpi["turn_count"] == 0
    assert kpi["cache_hit_rate"] is None
    assert kpi["saved_pct"] is None
    assert kpi["estimated_cost"] is None
    assert kpi["loc_added"] is None
    # The default today window is a single UTC date cell.
    assert [d["date"] for d in stats["heatmap"]["days"]] == ["2026-09-21"]
    assert stats["range"] == {"key": "today", "start": "2026-09-21", "end": "2026-09-21"}

    week_stats = get_usage_statistics(ws, now=NOW, range_key="7d")
    assert [d["date"] for d in stats["heatmap"]["days"]] == [
        "2026-09-21"
    ]
    assert [d["date"] for d in week_stats["heatmap"]["days"]] == [
        f"2026-09-{day:02d}" for day in range(15, 22)
    ]
    assert all(day["tokens"] == 0 and day["sessions"] == 0 for day in week_stats["heatmap"]["days"])
    assert all(day["tokens"] == 0 and day["sessions"] == 0 for day in stats["heatmap"]["days"])
    assert stats["heatmap"]["truncated"] is False
    assert stats["breakdowns"]["agent"] is None
    assert stats["top_sessions"] == []
    assert stats["top_tools"]["items"] == []


def test_empty_tables_report_real_zeros(tmp_path: Path) -> None:
    ws = _build_workspace(tmp_path)
    stats = get_usage_statistics(ws, now=NOW)
    assert stats["kpi"]["total_tokens"] == 0
    assert stats["project_matrix"][0]["tokens"] == 0
    assert stats["project_matrix"][0]["cost"] is None


# --- cache / net-input口径 ----------------------------------------------------


def test_net_input_deducts_cache_and_reasoning_is_not_double_counted(
    tmp_path: Path,
) -> None:
    event = _event(
        request_id="r1",
        provider_input=1000,
        cache_read=600,
        cache_write=100,
        output=200,
        saved=300,
        reasoning=999,
    )
    ws = _build_workspace(
        tmp_path,
        events=[{"created_at": "2026-09-21T10:00:00Z", "thread_id": "t1", "event": event}],
    )
    stats = get_usage_statistics(ws, now=NOW, range_key="today")
    kpi = stats["kpi"]
    assert kpi["provider_input_tokens"] == 1000
    assert kpi["net_input_tokens"] == 300  # 1000 - 600 - 100
    assert kpi["cache_read_tokens"] == 600
    assert kpi["cache_write_tokens"] == 100
    assert kpi["output_tokens"] == 200
    # Reasoning (999) lives inside provider output; adding it again would double count.
    # 累计吞吐 = provider_input + output: every prompt token the provider
    # processed (cached and fresh alike), not the narrower net input.
    assert kpi["total_tokens"] == 1000 + 200
    assert kpi["cache_hit_rate"] == 60.0
    assert kpi["saved_pct"] == round(300 / (1000 + 300) * 100, 1)


# --- range windows -----------------------------------------------------------


def test_range_windows_are_utc_closed_intervals(tmp_path: Path) -> None:
    events = [
        {
            "created_at": "2026-09-21T00:00:00Z",
            "thread_id": "t",
            "event": _event(request_id="a", provider_input=100),
        },
        {
            "created_at": "2026-09-20T23:59:59Z",
            "thread_id": "t",
            "event": _event(request_id="b", provider_input=200),
        },
        {
            "created_at": "2026-09-15T00:00:00Z",
            "thread_id": "t",
            "event": _event(request_id="c", provider_input=400),
        },
        {
            "created_at": "2026-09-14T23:59:59Z",
            "thread_id": "t",
            "event": _event(request_id="d", provider_input=800),
        },
        {
            "created_at": "2026-08-01T00:00:00Z",
            "thread_id": "t",
            "event": _event(request_id="e", provider_input=1600),
        },
    ]
    ws = _build_workspace(tmp_path, events=events)

    today = get_usage_statistics(ws, now=NOW, range_key="today")
    assert today["kpi"]["total_tokens"] == 100
    assert today["range"] == {"key": "today", "start": "2026-09-21", "end": "2026-09-21"}

    week = get_usage_statistics(ws, now=NOW, range_key="7d")
    assert week["kpi"]["total_tokens"] == 100 + 200 + 400

    month = get_usage_statistics(ws, now=NOW, range_key="30d")
    assert month["kpi"]["total_tokens"] == 100 + 200 + 400 + 800

    everything = get_usage_statistics(ws, now=NOW, range_key="all")
    assert everything["kpi"]["total_tokens"] == 100 + 200 + 400 + 800 + 1600

    custom = get_usage_statistics(
        ws, now=NOW, range_key="custom", start_date="2026-09-14", end_date="2026-09-15"
    )
    assert custom["kpi"]["total_tokens"] == 800 + 400
    assert custom["range"] == {"key": "custom", "start": "2026-09-14", "end": "2026-09-15"}


def test_invalid_parameters_raise_usage_stats_error(tmp_path: Path) -> None:
    ws = _build_workspace(tmp_path)
    with pytest.raises(UsageStatsError):
        get_usage_statistics(ws, now=NOW, range_key="yesterday")
    with pytest.raises(UsageStatsError):
        get_usage_statistics(ws, now=NOW, range_key="custom")
    with pytest.raises(UsageStatsError):
        get_usage_statistics(
            ws, now=NOW, range_key="custom", start_date="2026-09-21", end_date="2026-09-01"
        )
    with pytest.raises(UsageStatsError):
        get_usage_statistics(
            ws, now=NOW, range_key="custom", start_date="nope", end_date="2026-09-21"
        )
    with pytest.raises(UsageStatsError):
        get_usage_statistics(ws, now=NOW, project="some-other-workspace")


def test_current_project_is_accepted_and_unknown_is_rejected(tmp_path: Path) -> None:
    ws = _build_workspace(
        tmp_path,
        events=[
            {
                "created_at": "2026-09-21T01:00:00Z",
                "thread_id": "t",
                "event": _event(request_id="a", provider_input=100),
            }
        ],
    )
    selected = get_usage_statistics(ws, now=NOW, project=ws.name)
    assert selected["project"]["selected"] == ws.name
    assert selected["project"]["connected_count"] == 1
    assert selected["project_matrix"][0]["is_current"] is True
    assert selected["project_matrix"][0]["tokens"] == 100
    with pytest.raises(UsageStatsError):
        get_usage_statistics(ws, now=NOW, project="not-this-workspace")


def test_explicit_project_name_overrides_directory_name(tmp_path: Path) -> None:
    ws = _build_workspace(tmp_path)
    stats = get_usage_statistics(ws, now=NOW, project="catalog-name", project_name="catalog-name")
    assert stats["project"]["current"]["name"] == "catalog-name"


def test_all_and_selected_catalog_projects_use_real_project_data(tmp_path: Path) -> None:
    first = _build_workspace(tmp_path)
    second = tmp_path / "second"
    second_synapse = second / ".synapse"
    second_synapse.mkdir(parents=True)
    con = sqlite3.connect(second_synapse / "tool-outputs.sqlite")
    try:
        con.executescript(_TOOL_SCHEMA)
        event = _event(request_id="second-1", provider_input=2000)
        con.execute(
            "INSERT INTO model_request_compression_events"
            "(request_id, thread_id, event_json, created_at) VALUES (?, ?, ?, ?)",
            ("second-1", "second-thread", json.dumps(event), "2026-09-21T01:00:00Z"),
        )
        con.commit()
    finally:
        con.close()

    entries = [
        {"workspace_name": first.name, "workspace_path": str(first)},
        {"workspace_name": "second", "workspace_path": str(second)},
    ]
    all_stats = get_usage_statistics(
        first,
        now=NOW,
        range_key="today",
        project_name=first.name,
        project_entries=entries,
    )
    second_stats = get_usage_statistics(
        first,
        now=NOW,
        range_key="today",
        project="second",
        project_name=first.name,
        project_entries=entries,
    )
    assert all_stats["project"]["connected_count"] == 2
    assert all_stats["kpi"]["total_tokens"] == 2000
    assert second_stats["kpi"]["total_tokens"] == 2000
    assert second_stats["project_matrix"][0]["name"] == "second"


# --- no silent truncation ----------------------------------------------------


def test_more_than_five_thousand_events_are_all_counted(tmp_path: Path) -> None:
    events = [
        {
            "created_at": "2026-09-21T00:00:00Z",
            "thread_id": f"t{index}",
            "event": _event(request_id=f"r{index}", provider_input=10),
        }
        for index in range(6000)
    ]
    ws = _build_workspace(tmp_path, events=events)
    stats = get_usage_statistics(ws, now=NOW, range_key="all")
    assert stats["kpi"]["call_count"] == 6000
    assert stats["kpi"]["total_tokens"] == 60000
    assert len(stats["heatmap"]["days"]) == 1


# --- sessions ----------------------------------------------------------------


def test_sessions_are_ranked_by_real_tokens(tmp_path: Path) -> None:
    events = [
        {
            "created_at": "2026-09-21T01:00:00Z",
            "thread_id": "small",
            "event": _event(request_id="s1", provider_input=100, turn_id="t1"),
        },
        {
            "created_at": "2026-09-21T02:00:00Z",
            "thread_id": "big",
            "event": _event(request_id="b1", provider_input=1000, turn_id="t1"),
        },
        {
            "created_at": "2026-09-21T03:00:00Z",
            "thread_id": "big",
            "event": _event(request_id="b2", provider_input=2000, turn_id="t2"),
        },
    ]
    sessions = [
        (
            "big",
            "Big Session",
            "gpt-5",
            "2026-09-21T00:00:00+00:00",
            "2026-09-21T03:00:00+00:00",
            "[]",
            None,
        ),
        (
            "small",
            "Small Session",
            "gpt-5-mini",
            "2026-09-21T00:00:00+00:00",
            "2026-09-21T01:00:00+00:00",
            "[]",
            None,
        ),
    ]
    ws = _build_workspace(tmp_path, events=events, sessions=sessions)
    stats = get_usage_statistics(ws, now=NOW, range_key="today")
    top = stats["top_sessions"]
    assert [s["thread_id"] for s in top] == ["big", "small"]
    assert top[0]["title"] == "Big Session"
    assert top[0]["tokens"] == 3000
    assert top[0]["turns"] == 2
    assert top[1]["model"] == "gpt-5-mini"
    assert stats["project_matrix"][0]["sessions_count"] == 2
    assert stats["project_matrix"][0]["turns_count"] == 2


# --- heatmap & trend ---------------------------------------------------------


def test_heatmap_and_daily_trend_use_real_dates(tmp_path: Path) -> None:
    events = [
        {
            "created_at": "2026-09-20T01:00:00Z",
            "thread_id": "a",
            "event": _event(request_id="h1", provider_input=100, cache_read=50, output=10),
        },
        {
            "created_at": "2026-09-21T05:00:00Z",
            "thread_id": "b",
            "event": _event(request_id="h2", provider_input=200, cache_read=100, output=20),
        },
        {
            "created_at": "2026-09-21T06:00:00Z",
            "thread_id": "b",
            "event": _event(request_id="h3", provider_input=300, cache_read=150, output=30),
        },
    ]
    ws = _build_workspace(tmp_path, events=events)
    stats = get_usage_statistics(ws, now=NOW, range_key="7d")

    days = {d["date"]: d for d in stats["heatmap"]["days"]}
    assert days["2026-09-20"]["tokens"] == 110  # provider_input 100 + output 10
    assert days["2026-09-20"]["sessions"] == 1
    assert days["2026-09-21"]["tokens"] == (200 + 20) + (300 + 30)
    assert days["2026-09-21"]["sessions"] == 1
    # The heatmap is a continuous calendar strip: every day of the 7d window is
    # present (a zero-activity day is a real cell, not a missing column).
    assert [d["date"] for d in stats["heatmap"]["days"]] == [
        f"2026-09-{day:02d}" for day in range(15, 22)
    ]
    assert stats["heatmap"]["truncated"] is False

    items = stats["trend"]["items"]
    assert stats["trend"]["granularity"] == "day"
    assert len(items) == 7
    assert items[0]["date"] == "09-15"
    assert items[-1]["date"] == "09-21"
    assert items[-1]["cache"] == 250
    assert items[-1]["input"] == 100 + 150
    assert items[-1]["output"] == 50


def test_today_trend_is_hourly_and_bounded(tmp_path: Path) -> None:
    events = [
        {
            "created_at": "2026-09-21T05:00:00Z",
            "thread_id": "a",
            "event": _event(request_id="x", provider_input=100, output=10),
        }
    ]
    ws = _build_workspace(tmp_path, events=events)
    stats = get_usage_statistics(ws, now=NOW, range_key="today")
    items = stats["trend"]["items"]
    assert stats["trend"]["granularity"] == "hour"
    assert len(items) == 24
    assert items[5]["output"] == 10
    assert items[0]["output"] == 0


def test_heatmap_is_continuous_across_gaps(tmp_path: Path) -> None:
    """A gap day is a real zero cell, so the weekday grid stays aligned."""
    events = [
        {
            "created_at": "2026-09-18T01:00:00Z",
            "thread_id": "a",
            "event": _event(request_id="g1", provider_input=100),
        },
        {
            "created_at": "2026-09-21T01:00:00Z",
            "thread_id": "a",
            "event": _event(request_id="g2", provider_input=200),
        },
    ]
    ws = _build_workspace(tmp_path, events=events)
    stats = get_usage_statistics(ws, now=NOW, range_key="7d")
    days = stats["heatmap"]["days"]
    # The gap (09-19 / 09-20) must not collapse the strip: a missing column would
    # shift every later cell's weekday.
    assert [d["date"] for d in days] == [f"2026-09-{day:02d}" for day in range(15, 22)]
    by_date = {d["date"]: d for d in days}
    assert by_date["2026-09-19"]["tokens"] == 0
    assert by_date["2026-09-19"]["sessions"] == 0
    assert by_date["2026-09-18"]["tokens"] == 100
    assert by_date["2026-09-21"]["tokens"] == 200


def test_all_heatmap_is_continuous_from_first_to_last_active_day(tmp_path: Path) -> None:
    events = [
        {
            "created_at": "2026-09-01T00:00:00Z",
            "thread_id": "t",
            "event": _event(request_id="a", provider_input=10),
        },
        {
            "created_at": "2026-09-05T00:00:00Z",
            "thread_id": "t",
            "event": _event(request_id="b", provider_input=20),
        },
    ]
    ws = _build_workspace(tmp_path, events=events)
    stats = get_usage_statistics(ws, now=NOW, range_key="all")
    assert [d["date"] for d in stats["heatmap"]["days"]] == [
        f"2026-09-{day:02d}" for day in range(1, 6)
    ]
    assert stats["heatmap"]["truncated"] is False
    # The totals still cover the whole window (not just the heatmap strip).
    assert stats["kpi"]["total_tokens"] == 10 + 20


def test_all_heatmap_is_capped_at_the_last_366_days(tmp_path: Path) -> None:
    """A >366-day span caps the heatmap but never the aggregate totals."""
    events = [
        {
            "created_at": "2024-01-01T00:00:00Z",
            "thread_id": "t",
            "event": _event(request_id="old", provider_input=1000),
        },
        {
            "created_at": "2026-09-21T00:00:00Z",
            "thread_id": "t",
            "event": _event(request_id="new", provider_input=7),
        },
    ]
    ws = _build_workspace(tmp_path, events=events)
    stats = get_usage_statistics(ws, now=NOW, range_key="all")
    days = stats["heatmap"]["days"]
    assert stats["heatmap"]["truncated"] is True
    assert len(days) == 366
    assert days[0]["date"] == "2025-09-21"
    assert days[-1]["date"] == "2026-09-21"
    # The 2024 event is outside the heatmap strip but still in the totals.
    assert stats["kpi"]["total_tokens"] == 1000 + 7


def test_custom_range_over_the_max_span_is_rejected(tmp_path: Path) -> None:
    """A caller-chosen window is bounded (400) instead of materialising days."""
    ws = _build_workspace(tmp_path)
    with pytest.raises(UsageStatsError):
        get_usage_statistics(
            ws,
            now=NOW,
            range_key="custom",
            start_date="2000-01-01",
            end_date="2026-09-21",
        )


# --- tools (partial view) ----------------------------------------------------


def test_tool_records_are_labelled_partial_not_all_calls(tmp_path: Path) -> None:
    refs = [
        ("ref1", "t1", "ns", "c1", "read_file", "success", "sha", "2026-09-21T01:00:00Z"),
        ("ref2", "t1", "ns", "c2", "read_file", "error", "sha", "2026-09-21T02:00:00Z"),
        ("ref3", "t2", "ns", "c3", "patch", "success", "sha", "2026-09-21T03:00:00Z"),
        ("ref4", "t2", "ns", "c4", "read_file", "success", "sha", "2026-08-01T00:00:00Z"),
    ]
    ws = _build_workspace(tmp_path, refs=refs)
    stats = get_usage_statistics(ws, now=NOW, range_key="today")
    tools = stats["top_tools"]
    assert tools["partial"] is True
    assert tools["recorded_total"] == 3  # the August ref is outside today
    assert "不代表全部工具调用" in tools["scope_note"]
    by_name = {t["name"]: t for t in tools["items"]}
    assert by_name["read_file"]["count"] == 2
    assert by_name["read_file"]["success_count"] == 1
    assert by_name["read_file"]["failure_count"] == 1
    assert by_name["read_file"]["success_rate"] == 50.0
    assert by_name["read_file"]["avg_ms"] is None


# --- provenance notes --------------------------------------------------------


@pytest.mark.parametrize(
    ("branch_output", "status_output", "expected"),
    [
        ("功能/统计\n".encode(), b" M filename-\xac\xff\n", ("功能/统计", True)),
        (b"feature/\xff\n", b"", ("feature/\ufffd", False)),
        (None, None, ("", False)),
    ],
)
def test_git_state_reads_bytes_without_locale_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch_output: bytes | None,
    status_output: bytes | None,
    expected: tuple[str, bool],
) -> None:
    outputs = iter((branch_output, status_output))
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        assert not kwargs.get("text")
        assert not kwargs.get("encoding")
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] == 2.0
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=next(outputs), stderr=b"\xff")

    monkeypatch.setattr("synapse.web_console.usage_stats.shutil.which", lambda _: "git")
    monkeypatch.setattr("synapse.web_console.usage_stats.subprocess.run", run)
    assert _git_state(tmp_path) == expected
    assert len(calls) == 2
    assert calls[0][-2:] == ["branch", "--show-current"]
    assert calls[1][-2:] == ["status", "--porcelain"]


def test_payload_carries_provenance_notes(tmp_path: Path) -> None:
    ws = _build_workspace(tmp_path)
    stats = get_usage_statistics(ws, now=NOW)
    joined = " ".join(stats["notes"])
    assert "cache_read" in joined
    assert "UTC" in joined
    assert stats["generated_at"].startswith("2026-09-21")


def test_model_filtering_and_available_models(tmp_path: Path) -> None:
    events = [
        {
            "thread_id": "thread-1",
            "created_at": "2026-09-21T09:00:00+00:00",
            "event": _event(
                request_id="req-1",
                provider_input=100,
                output=50,
                model="deepseek-v4-flash",
            ),
        },
        {
            "thread_id": "thread-2",
            "created_at": "2026-09-21T10:00:00+00:00",
            "event": _event(
                request_id="req-2",
                provider_input=400,
                output=100,
                model="gpt-5.6-luna",
            ),
        },
    ]
    ws = _build_workspace(
        tmp_path,
        events=events,
        sessions=[
            ("thread-1", "Session 1", "deepseek-v4-flash", "2026-09-21", "2026-09-21", "[]", ""),
            ("thread-2", "Session 2", "openai:gpt-5.6-luna", "2026-09-21", "2026-09-21", "[]", ""),
        ],
    )

    all_stats = get_usage_statistics(ws, model="all", now=NOW)
    assert "deepseek-v4-flash" in all_stats["available_models"]
    assert "gpt-5.6-luna" in all_stats["available_models"]
    assert all_stats["selected_model"] == "all"
    assert all_stats["kpi"]["total_tokens"] == 150 + 500

    filtered_a = get_usage_statistics(ws, model="deepseek-v4-flash", now=NOW)
    assert filtered_a["selected_model"] == "deepseek-v4-flash"
    assert filtered_a["kpi"]["total_tokens"] == 150
    assert len(filtered_a["top_sessions"]) == 1
    assert filtered_a["top_sessions"][0]["thread_id"] == "thread-1"

    # Provider prefix matching: "openai:gpt-5.6-luna" matches "gpt-5.6-luna"
    filtered_b = get_usage_statistics(ws, model="openai:gpt-5.6-luna", now=NOW)
    assert filtered_b["kpi"]["total_tokens"] == 500
    assert len(filtered_b["top_sessions"]) == 1
    assert filtered_b["top_sessions"][0]["thread_id"] == "thread-2"

    # Unknown model produces 0 totals gracefully
    filtered_none = get_usage_statistics(ws, model="non-existent-model", now=NOW)
    assert filtered_none["kpi"]["total_tokens"] == 0
    assert filtered_none["top_sessions"] == []
