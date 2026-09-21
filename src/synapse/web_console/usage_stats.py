"""Usage analytics aggregator for the Synapse Web Console.

Reads the **current** workspace's project-local SQLite stores and aggregates
them into the console's usage dashboard payload:

* ``<workspace>/.synapse/tool-outputs.sqlite`` -- ``model_request_compression_events``
  (per-model-call token/cache accounting written by
  ``synapse.runtime.model_request_compression_middleware``) and
  ``tool_output_refs`` (tool outputs that were compressed and stored).
* ``<workspace>/.synapse/sessions.sqlite`` -- session titles/models, matched to
  the events by ``thread_id``.

Data provenance and limits (kept honest on purpose -- see
``docs/web-console/`` for the user-facing summary):

* Token/cache accounting comes from the real compression events.  **Cumulative
  throughput** (``kpi.total_tokens``) is ``provider_input_tokens + output_tokens``
  -- every prompt token the provider processed, cached or fresh, plus its output.
  **Net input** is ``provider_input_tokens - cache_read_tokens - cache_write_tokens``.
  Provider output is counted once and reasoning is **not** added on top: providers
  already fold reasoning into their output token count, so adding the middleware's
  ``content_breakdown.reasoning`` estimate would double count.
* Every date is UTC and every date window is a **closed** interval.  A ``custom``
  window is capped at :data:`MAX_CUSTOM_SPAN_DAYS` (400 beyond) and the activity
  heatmap is capped at :data:`MAX_HEATMAP_DAYS` continuous days (flagged in the
  payload); neither cap truncates the aggregate totals.
* Cost, changed code lines, tool execution duration and per-agent attribution
  have **no** source in these stores and are reported as ``None``/omitted rather
  than fabricated.
* Tool counts come from ``tool_output_refs`` and cover only tool outputs that
  were compressed and stored as references -- never all tool calls.

The host supplies the catalog's visible project workspaces.  The selected
project or ``all`` scope determines which of those project-local stores are
read; arbitrary paths are never accepted from the browser.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from synapse.tool_output import jsonio

logger = logging.getLogger(__name__)

#: Accepted ``range`` query values.  ``custom`` requires ``start`` and ``end``.
VALID_RANGES = ("today", "7d", "30d", "all", "custom")
#: The pseudo-project that means "the one connected workspace" (there is only
#: one -- the console never aggregates other workspaces).
PROJECT_ALL = "all"
#: Ranking cut-offs for the two "top N" panels.  These bound the *display*
#: list only; the underlying aggregation always streams every matching event.
MAX_TOP_SESSIONS = 10
MAX_TOP_TOOLS = 8
#: Hard bound on a ``custom`` window's span.  The caller picks this window, so
#: it is the only input that could otherwise ask for an arbitrarily long day
#: series; anything wider is rejected with a 400 instead of materialising it.
#: ~10 years is far beyond any real usage history.
MAX_CUSTOM_SPAN_DAYS = 3660
#: The activity heatmap renders one cell per calendar day, so a huge window
#: would blow up the DOM.  It is explicitly limited to the last 366 days of the
#: window (and the payload flags that); this never truncates the aggregate
#: totals, which still cover the whole window.
MAX_HEATMAP_DAYS = 366
#: The hourly matrix is one row per day and 24 columns per row, so it is capped
#: at the window's last two weeks (same rule as above: the cap moves the matrix,
#: never the totals).  A single-day window therefore renders exactly 24 cells,
#: which is what "show me today" means.
MAX_HEATMAP_HOUR_DAYS = 14
#: Fixed hex palette for the donut/breakdown charts (SVG presentation
#: attributes cannot resolve CSS custom properties reliably, so hex it is).
_CHART_PALETTE = (
    "#0078d4",
    "#10b981",
    "#a855f7",
    "#f59e0b",
    "#ef4444",
    "#06b6d4",
    "#8b5cf6",
    "#84cc16",
)
_GIT_TIMEOUT_SECONDS = 2.0


def _clean_model_name(name: str | None) -> str:
    if not name:
        return ""
    s = str(name).strip()
    if not s or s.lower() == "unknown":
        return ""
    if ":" in s:
        return s.split(":", 1)[1].strip()
    return s


def _model_matches(candidate: str, target: str | None) -> bool:
    if not target or target.strip() in ("", "all"):
        return True
    target_clean = _clean_model_name(target)
    cand_clean = _clean_model_name(candidate)
    if target_clean and cand_clean and target_clean == cand_clean:
        return True
    return candidate.strip() == target.strip()


class UsageStatsError(ValueError):
    """Invalid usage-stats request parameter (the host maps this to HTTP 400)."""


def _open_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open a project database read-only, or ``None`` when it does not exist."""
    if not db_path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error as exc:  # pragma: no cover - environment specific
        logger.warning("usage stats: cannot open %s: %s", db_path, exc)
        return None


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (AttributeError, ValueError):
        raise UsageStatsError(f"invalid {field} date (expected YYYY-MM-DD): {value!r}") from None


def _resolve_window(
    range_key: str, start_date: str, end_date: str, today: date
) -> tuple[date | None, date | None]:
    """Resolve a range key to an inclusive UTC ``[start, end]`` date window.

    ``None`` bounds mean unbounded (only ``all``).  ``today``/``7d``/``30d`` are
    anchored on ``today`` and are closed intervals (``7d`` spans today and the
    six preceding days).
    """
    if range_key == "today":
        return today, today
    if range_key == "7d":
        return today - timedelta(days=6), today
    if range_key == "30d":
        return today - timedelta(days=29), today
    if range_key == "all":
        return None, None
    if range_key == "custom":
        if not start_date or not end_date:
            raise UsageStatsError("custom range requires both start and end dates")
        start = _parse_date(start_date, "start")
        end = _parse_date(end_date, "end")
        if start > end:
            raise UsageStatsError("start date must not be after end date")
        if (end - start).days + 1 > MAX_CUSTOM_SPAN_DAYS:
            raise UsageStatsError(
                f"custom range must not exceed {MAX_CUSTOM_SPAN_DAYS} days"
            )
        return start, end
    raise UsageStatsError(
        f"unknown range: {range_key!r}; expected one of {', '.join(VALID_RANGES)}"
    )


def _window_clause(start: date | None, end: date | None) -> tuple[str, list[str]]:
    """A ``WHERE`` fragment on ``created_at`` for the inclusive UTC window."""
    clauses: list[str] = []
    params: list[str] = []
    if start is not None:
        clauses.append("created_at >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("created_at <= ?")
        params.append(f"{end.isoformat()}T23:59:59Z")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _empty_aggregate() -> dict[str, Any]:
    return {
        "total": {
            "tokens": 0,
            "net_input": 0,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "saved": 0,
            "calls": 0,
            "duration_ms": 0.0,
            "baseline": 0,
        },
        "days": {},
        "hours": {},
        # day -> 24 slots (None until an hour has activity); each slot counts the
        # hour's tokens and the distinct sessions active in it.  Only the window's
        # last ``MAX_HEATMAP_HOUR_DAYS`` days are kept (see ``_hour_slot``).
        "hour_days": {},
        "models": {},
        "threads": {},
        "turn_ids": set(),
        "tools": {},
        "tool_records": 0,
    }


#: The scalars the aggregate needs from one compression event.  They exist both
#: as columns in ``model_request_compression_rollup`` (the fast path) and as keys
#: inside ``event_json`` (the fallback path for events that predate the rollup).
_EVENT_FIELDS = (
    "provider_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "total_saved_tokens",
    "input_tokens_before",
    "duration_ms",
)

#: How many unprojected events one fallback query returns.  The fallback pages
#: through the whole remainder by ``id``: a batch bounds memory, never the count,
#: so a store whose projection is still catching up reports the same numbers as a
#: fully projected one -- it just takes longer to produce them.
_UNPROJECTED_BATCH = 5000

#: How many recent events the model-discovery fallback may parse when a store has
#: no rollup rows yet.  Discovery only needs the models in current use.
_MODEL_DISCOVERY_ROWS = 1000


def _projection_complete(con: sqlite3.Connection) -> bool:
    """Whether every compression event already has a rollup row.

    Two whole-table ``COUNT(*)`` calls, both answered from an index (the event
    table's ``request_id`` index, the rollup's primary key) -- a few milliseconds
    on a 43k-event store, and no index on the multi-gigabyte event payloads is
    needed to decide this.  The counts are compared globally rather than per
    window on purpose: a windowed count would need an index over ``created_at``
    on the events table, whose one-time build scans every ~30 KB payload while
    holding the write lock.
    """
    events = int(
        con.execute("SELECT COUNT(*) FROM model_request_compression_events").fetchone()[0]
    )
    projected = int(
        con.execute("SELECT COUNT(*) FROM model_request_compression_rollup").fetchone()[0]
    )
    return events == projected


def _consume_events(
    con: sqlite3.Connection,
    start: date | None,
    end: date | None,
    agg: dict[str, Any],
    model_filter: str | None = None,
) -> None:
    """Aggregate the window's compression events, fast path first.

    The window is read from ``model_request_compression_rollup`` -- ten narrow
    columns, ~120 B per event -- and only events that have no rollup row yet are
    read out of ``event_json``.  A complete rollup therefore never touches the
    ~30-90 KB-per-row payloads that made this endpoint take seconds.

    A store that predates the rollup table simply has nothing projected, and the
    whole window goes through the JSON fallback.
    """
    has_rollup = _table_exists(con, "model_request_compression_rollup")
    if has_rollup:
        where, params = _window_clause(start, end)
        columns = ", ".join(("thread_id", "created_at", "turn_id", "model", *_EVENT_FIELDS))
        cur = con.cursor()
        cur.execute(f"SELECT {columns} FROM model_request_compression_rollup{where}", params)
        for row in cur:  # streaming read -- deliberately not ``fetchall()``
            event = {field: row[field] for field in _EVENT_FIELDS}
            event["turn_id"] = row["turn_id"]
            event["model"] = row["model"]
            _accumulate_event(
                agg,
                str(row["thread_id"] or ""),
                str(row["created_at"] or ""),
                event,
                model_filter,
            )
        if _projection_complete(con):
            return

    # Fallback: the events that the rollup does not cover yet.  ``NOT EXISTS``
    # keeps this to the unprojected remainder, so a partially backfilled store
    # never has the same event counted twice.  The projection happens inside
    # SQLite: ``json_extract`` is ~2.6x faster here than building the whole
    # ~90 KB document in Python just to read ten scalars (measured on a 19k-event
    # store), and ``json_valid`` keeps a malformed row out of the aggregate
    # instead of aborting the query.
    where, params = _window_clause(start, end)
    if has_rollup:
        join = " AND" if where else " WHERE"
        unprojected_only = (
            f"{join} NOT EXISTS ("
            "  SELECT 1 FROM model_request_compression_rollup r WHERE r.request_id = e.request_id"
            ")"
        )
        validity = " AND json_valid(e.event_json)"
    else:
        unprojected_only = ""
        validity = (" AND" if where else " WHERE") + " json_valid(e.event_json)"
    extracted = ", ".join(
        f"json_extract(e.event_json, '$.{field}') AS {field}" for field in _EVENT_FIELDS
    )
    cur = con.cursor()
    last_id = 0
    while True:
        cur.execute(
            "SELECT e.id, e.thread_id, e.created_at, "
            "json_extract(e.event_json, '$.turn_id') AS turn_id, "
            "json_extract(e.event_json, '$.model') AS model, "
            f"{extracted} "
            f"FROM model_request_compression_events e{where}{unprojected_only}{validity} "
            "AND e.id > ? ORDER BY e.id LIMIT ?",
            (*params, last_id, _UNPROJECTED_BATCH),
        )
        rows = cur.fetchall()
        if not rows:
            return
        for row in rows:
            event = {field: row[field] for field in _EVENT_FIELDS}
            event["turn_id"] = row["turn_id"]
            event["model"] = row["model"]
            _accumulate_event(
                agg,
                str(row["thread_id"] or ""),
                str(row["created_at"] or ""),
                event,
                model_filter,
            )
        if len(rows) < _UNPROJECTED_BATCH:
            return
        last_id = int(rows[-1]["id"])


def _accumulate_event(
    agg: dict[str, Any],
    thread_id: str,
    created: str,
    event: dict[str, Any],
    model_filter: str | None,
) -> None:
    """Fold one compression event (from either store) into the aggregate."""
    total = agg["total"]
    day = created[:10]
    if len(day) != 10:
        return
    try:
        hour = int(created[11:13])
    except ValueError:
        hour = 0

    provider_input = _to_int(event.get("provider_input_tokens"))
    cache_read = _to_int(event.get("cache_read_tokens"))
    cache_write = _to_int(event.get("cache_write_tokens"))
    output = _to_int(event.get("output_tokens"))
    saved = _to_int(event.get("total_saved_tokens"))
    baseline = _to_int(event.get("input_tokens_before"))
    duration_ms = _to_float(event.get("duration_ms"))
    model = str(event.get("model") or "unknown") or "unknown"
    if model_filter and not _model_matches(model, model_filter):
        return
    turn_id = str(event.get("turn_id") or "")

    # Net input口径: provider prompt tokens minus *both* cache buckets.
    # ``uncached_input_tokens`` is only used as a fallback for events that
    # predate the provider counters; when it fires the provider input is
    # reconstructed from its parts so the throughput total stays coherent.
    net_input = max(0, provider_input - cache_read - cache_write)
    if provider_input == 0 and net_input == 0:
        net_input = max(0, _to_int(event.get("uncached_input_tokens")))
        provider_input = net_input + cache_read + cache_write
    # 累计吞吐口径: every prompt token the provider processed (cached and
    # fresh alike) plus its output.  ``net_input + output`` would silently
    # drop both cache buckets from the headline number.
    tokens = provider_input + output

    total["tokens"] += tokens
    total["net_input"] += net_input
    total["cache_read"] += cache_read
    total["cache_write"] += cache_write
    total["output"] += output
    total["saved"] += saved
    total["calls"] += 1
    total["duration_ms"] += duration_ms
    total["baseline"] += baseline

    day_bucket = agg["days"].setdefault(
        day,
        {
            "tokens": 0,
            "net_input": 0,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "saved": 0,
            "baseline": 0,
            "threads": set(),
        },
    )
    day_bucket["tokens"] += tokens
    day_bucket["net_input"] += net_input
    day_bucket["cache_read"] += cache_read
    day_bucket["cache_write"] += cache_write
    day_bucket["output"] += output
    day_bucket["saved"] += saved
    day_bucket["baseline"] += baseline
    if thread_id:
        day_bucket["threads"].add(thread_id)

    hour_bucket = agg["hours"].setdefault(
        hour,
        {
            "net_input": 0,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "baseline": 0,
        },
    )
    hour_bucket["net_input"] += net_input
    hour_bucket["cache_read"] += cache_read
    hour_bucket["cache_write"] += cache_write
    hour_bucket["output"] += output
    hour_bucket["baseline"] += baseline

    _hour_slot(agg, day, hour)["tokens"] += tokens
    if thread_id:
        _hour_slot(agg, day, hour)["threads"].add(thread_id)

    agg["models"][model] = agg["models"].get(model, 0) + tokens

    if thread_id:
        thread_bucket = agg["threads"].setdefault(
            thread_id, {"tokens": 0, "cache_read": 0, "provider_input": 0, "turns": set()}
        )
        thread_bucket["tokens"] += tokens
        thread_bucket["cache_read"] += cache_read
        thread_bucket["provider_input"] += provider_input
        if turn_id:
            thread_bucket["turns"].add(turn_id)

    if turn_id:
        agg["turn_ids"].add(turn_id)


def _hour_slot(agg: dict[str, Any], day: str, hour: int) -> dict[str, Any]:
    """The ``(UTC day, hour)`` cell of the hourly matrix, created on first use.

    Buckets stay in UTC here and are relabelled into the host's time zone when the
    payload is built (:func:`_local_hour_matrix`): a time-zone conversion per
    event costs ~18 us, i.e. ~0.4 s on a 20k-event window, while relabelling the
    handful of surviving buckets costs microseconds and is exactly equivalent --
    an offset moves a whole hour bucket, it never splits one.

    The matrix only ever shows the window's last :data:`MAX_HEATMAP_HOUR_DAYS`
    local days, so older days are dropped as they are pushed out -- the dict
    holds at most that many UTC days (plus a margin for the zone offset) no
    matter how wide the window is.
    """
    days = agg["hour_days"]
    slots = days.get(day)
    if slots is None:
        slots = days[day] = [None] * 24
        if len(days) > MAX_HEATMAP_HOUR_DAYS + 2:
            days.pop(min(days))
    slot = slots[hour]
    if slot is None:
        slot = slots[hour] = {"tokens": 0, "threads": set()}
    return slot


def _local_offset_for_day(day: str) -> timedelta:
    """The host's UTC offset at noon UTC of ``day``.

    Evaluated per day rather than once per request so a daylight-saving change
    inside the window is applied to the days on either side of it.  ``astimezone``
    with no argument resolves the platform's zone *for that instant*, which is
    what makes this correct on the two days a year that differ.
    """
    try:
        moment = datetime.fromisoformat(f"{day}T12:00:00+00:00")
        return moment.astimezone().utcoffset() or timedelta(0)
    except ValueError:  # pragma: no cover - malformed day key
        return timedelta(0)


def _local_hour_matrix(
    utc_slots: dict[str, list[dict[str, Any] | None]],
) -> tuple[list[dict[str, Any]], bool]:
    """Relabel the UTC hour buckets into the host's zone.

    Returns ``(rows, truncated)``: one row per local day that has activity, most
    recent last and at most :data:`MAX_HEATMAP_HOUR_DAYS` of them, each with 24
    values indexed by local hour.  A day only appears when something happened in
    it, which is why the rows carry their date instead of being padded into a
    continuous strip -- there is no weekday row to keep aligned.
    """
    merged: dict[str, list[dict[str, Any] | None]] = {}
    for day, slots in utc_slots.items():
        shift = int(_local_offset_for_day(day).total_seconds() // 3600)
        if shift == 0:
            local_day, local_slots = day, slots
            for hour, slot in enumerate(local_slots):
                if slot is None:
                    continue
                cell = _matrix_cell(merged, local_day, hour)
                cell["tokens"] += slot["tokens"]
                cell["threads"].update(slot["threads"])
            continue
        base = date.fromisoformat(day)
        for hour, slot in enumerate(slots):
            if slot is None:
                continue
            local_hour = hour + shift
            local_day = base
            if local_hour >= 24:
                local_hour -= 24
                local_day = base + timedelta(days=1)
            elif local_hour < 0:
                local_hour += 24
                local_day = base - timedelta(days=1)
            cell = _matrix_cell(merged, local_day.isoformat(), local_hour)
            cell["tokens"] += slot["tokens"]
            cell["threads"].update(slot["threads"])

    days = sorted(merged)
    truncated = len(days) > MAX_HEATMAP_HOUR_DAYS
    rows: list[dict[str, Any]] = []
    for day in days[-MAX_HEATMAP_HOUR_DAYS:]:
        slots = merged[day]
        rows.append(
            {
                "date": day,
                "tokens": [
                    int(slot["tokens"]) if slot is not None else 0 for slot in slots
                ],
                "sessions": [
                    len(slot["threads"]) if slot is not None else 0 for slot in slots
                ],
            }
        )
    return rows, truncated


def _matrix_cell(
    merged: dict[str, list[dict[str, Any] | None]], day: str, hour: int
) -> dict[str, Any]:
    slots = merged.setdefault(day, [None] * 24)
    cell = slots[hour]
    if cell is None:
        cell = slots[hour] = {"tokens": 0, "threads": set()}
    return cell


def _local_offset_label() -> str:
    """``UTC+08:00`` / ``UTC-05:00`` / ``UTC`` for the host's current zone."""
    offset = datetime.now(UTC).astimezone().utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    if total_minutes == 0:
        return "UTC"
    sign = "+" if total_minutes > 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _consume_tool_refs(
    con: sqlite3.Connection,
    start: date | None,
    end: date | None,
    agg: dict[str, Any],
    allowed_threads: set[str] | None = None,
) -> None:
    """Aggregate recorded tool-output refs (a partial, not exhaustive, view)."""
    where, params = _window_clause(start, end)
    cur = con.cursor()
    # ``thread_id`` is read unconditionally: the model filter narrows this table
    # to the threads that produced events in the window, and the previous column
    # list left that filter reading a column the query never selected.
    cur.execute(
        f"SELECT thread_id, tool_name, status, created_at FROM tool_output_refs{where}",
        params,
    )
    for row in cur:  # streaming read
        if allowed_threads is not None:
            thread_id = str(row["thread_id"] or "")
            if thread_id not in allowed_threads:
                continue
        name = str(row["tool_name"] or "unknown") or "unknown"
        status = str(row["status"] or "").lower()
        entry = agg["tools"].setdefault(name, {"count": 0, "success": 0, "failure": 0})
        entry["count"] += 1
        if status == "success":
            entry["success"] += 1
        else:
            entry["failure"] += 1
        agg["tool_records"] += 1


def _merge_aggregates(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge one workspace aggregate into a multi-project aggregate."""
    for key in (
        "tokens",
        "net_input",
        "cache_read",
        "cache_write",
        "output",
        "saved",
        "calls",
        "duration_ms",
        "baseline",
    ):
        target["total"][key] += source["total"][key]
    for day, source_bucket in source["days"].items():
        bucket = target["days"].setdefault(
            day,
            {
                "tokens": 0,
                "net_input": 0,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
                "saved": 0,
                "baseline": 0,
                "threads": set(),
            },
        )
        for key in (
            "tokens",
            "net_input",
            "cache_read",
            "cache_write",
            "output",
            "saved",
            "baseline",
        ):
            bucket[key] += source_bucket[key]
        bucket["threads"].update(source_bucket["threads"])
    for hour, source_bucket in source["hours"].items():
        bucket = target["hours"].setdefault(
            hour, {"net_input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "baseline": 0}
        )
        for key in ("net_input", "cache_read", "cache_write", "output", "baseline"):
            bucket[key] += source_bucket[key]
    for day, source_slots in source["hour_days"].items():
        slots = target["hour_days"].setdefault(day, [None] * 24)
        for hour, source_slot in enumerate(source_slots):
            if source_slot is None:
                continue
            slot = slots[hour]
            if slot is None:
                slot = slots[hour] = {"tokens": 0, "threads": set()}
            slot["tokens"] += source_slot["tokens"]
            slot["threads"].update(source_slot["threads"])
    for model, tokens in source["models"].items():
        target["models"][model] = target["models"].get(model, 0) + tokens
    for thread_id, source_bucket in source["threads"].items():
        bucket = target["threads"].setdefault(
            thread_id, {"tokens": 0, "cache_read": 0, "provider_input": 0, "turns": set()}
        )
        bucket["tokens"] += source_bucket["tokens"]
        bucket["cache_read"] += source_bucket["cache_read"]
        bucket["provider_input"] += source_bucket["provider_input"]
        bucket["turns"].update(source_bucket["turns"])
    target["turn_ids"].update(source["turn_ids"])
    for name, source_bucket in source["tools"].items():
        bucket = target["tools"].setdefault(name, {"count": 0, "success": 0, "failure": 0})
        bucket["count"] += source_bucket["count"]
        bucket["success"] += source_bucket["success"]
        bucket["failure"] += source_bucket["failure"]
    target["tool_records"] += source["tool_records"]


def _namespace_aggregate(agg: dict[str, Any], namespace: str) -> dict[str, Any]:
    """Copy session identifiers into a project namespace before multi-project merge."""
    for bucket in agg["days"].values():
        bucket["threads"] = {f"{namespace}:{thread_id}" for thread_id in bucket["threads"]}
    for slots in agg["hour_days"].values():
        for slot in slots:
            if slot is not None:
                slot["threads"] = {
                    f"{namespace}:{thread_id}" for thread_id in slot["threads"]
                }
    agg["threads"] = {
        f"{namespace}:{thread_id}": bucket for thread_id, bucket in agg["threads"].items()
    }
    agg["turn_ids"] = {f"{namespace}:{turn_id}" for turn_id in agg["turn_ids"]}
    return agg


def _collect_workspace_aggregate(
    workspace: Path,
    start: date | None,
    end: date | None,
    model_filter: str | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read one project workspace and return usage plus session metadata."""
    agg = _empty_aggregate()
    synapse_dir = workspace / ".synapse"
    tool_con = _open_readonly(synapse_dir / "tool-outputs.sqlite")
    try:
        if tool_con is not None:
            if _table_exists(tool_con, "model_request_compression_events"):
                _consume_events(tool_con, start, end, agg, model_filter=model_filter)
            if _table_exists(tool_con, "tool_output_refs"):
                allowed_threads = set(agg["threads"].keys()) if model_filter else None
                _consume_tool_refs(tool_con, start, end, agg, allowed_threads=allowed_threads)
    finally:
        if tool_con is not None:
            tool_con.close()

    sessions_meta: dict[str, dict[str, Any]] = {}
    session_con = _open_readonly(synapse_dir / "sessions.sqlite")
    try:
        if session_con is not None and _table_exists(session_con, "sessions"):
            cur = session_con.cursor()
            cur.execute("SELECT thread_id, title, model FROM sessions")
            for row in cur:
                sessions_meta[str(row["thread_id"])] = {
                    "title": str(row["title"] or ""),
                    "model": row["model"],
                }
    finally:
        if session_con is not None:
            session_con.close()
    return agg, sessions_meta


def _collect_workspace_models(workspace: Path) -> set[str]:
    """Discover distinct models recorded in this workspace's sqlite stores."""
    models: set[str] = set()
    synapse_dir = workspace / ".synapse"

    tool_con = _open_readonly(synapse_dir / "tool-outputs.sqlite")
    if tool_con is not None:
        try:
            # The rollup carries the model as a plain column, so the discovery
            # list costs a scan of a ~2 MB table instead of a ``json_extract``
            # over every ~90 KB event (~4 s on a large store).
            if _table_exists(tool_con, "model_request_compression_rollup"):
                for row in tool_con.execute(
                    "SELECT DISTINCT model FROM model_request_compression_rollup"
                ):
                    if row[0]:
                        clean = _clean_model_name(str(row[0]))
                        if clean:
                            models.add(clean)
            # A store written before the rollup existed has nothing to read
            # there yet; fall back to a bounded slice of the newest events.  The
            # list is a discovery aid (the browser merges it across loads), so
            # reading the recent tail is enough to keep it useful.
            if not models and _table_exists(tool_con, "model_request_compression_events"):
                cur = tool_con.cursor()
                try:
                    cur.execute(
                        "SELECT DISTINCT json_extract(event_json, '$.model') FROM ("
                        "  SELECT event_json FROM model_request_compression_events"
                        "  ORDER BY id DESC LIMIT ?"
                        ")",
                        (_MODEL_DISCOVERY_ROWS,),
                    )
                    for row in cur:
                        if row[0]:
                            clean = _clean_model_name(str(row[0]))
                            if clean:
                                models.add(clean)
                except sqlite3.OperationalError:
                    cur.execute(
                        "SELECT event_json FROM model_request_compression_events "
                        "ORDER BY id DESC LIMIT ?",
                        (_MODEL_DISCOVERY_ROWS,),
                    )
                    for row in cur:
                        try:
                            ev = jsonio.loads(row[0])
                            if isinstance(ev, dict) and ev.get("model"):
                                clean = _clean_model_name(str(ev["model"]))
                                if clean:
                                    models.add(clean)
                        except Exception:
                            continue
        finally:
            tool_con.close()

    sess_con = _open_readonly(synapse_dir / "sessions.sqlite")
    if sess_con is not None:
        try:
            if _table_exists(sess_con, "sessions"):
                cur = sess_con.cursor()
                cur.execute(
                    "SELECT DISTINCT model FROM sessions WHERE model IS NOT NULL AND model != ''"
                )
                for row in cur:
                    if row[0]:
                        clean = _clean_model_name(str(row[0]))
                        if clean:
                            models.add(clean)
        finally:
            sess_con.close()

    return models


def _collect_configured_models(workspace: Path) -> set[str]:
    """Discover configured models declared in user or project models.json."""
    models: set[str] = set()
    candidate_paths = [
        Path.home() / ".synapse" / "models.json",
        workspace / ".synapse" / "models.json",
    ]
    for p in candidate_paths:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    models_dict = data.get("models")
                    if isinstance(models_dict, dict):
                        for k, v in models_dict.items():
                            clean_k = _clean_model_name(k)
                            if clean_k:
                                models.add(clean_k)
                            if isinstance(v, dict) and v.get("model"):
                                clean_v = _clean_model_name(str(v["model"]))
                                if clean_v:
                                    models.add(clean_v)
            except Exception:
                continue
    return models


def _project_contexts(
    workspace: Path,
    current_name: str,
    project_entries: list[dict[str, Any]] | None,
) -> list[tuple[str, Path]]:
    """Normalize the catalog rows supplied by the host into project contexts."""
    if not project_entries:
        return [(current_name, workspace)]
    contexts: list[tuple[str, Path]] = []
    for entry in project_entries:
        name = str(entry.get("workspace_name") or entry.get("name") or "").strip()
        raw_path = entry.get("workspace_path") or entry.get("path")
        if name and raw_path:
            contexts.append((name, Path(str(raw_path)).expanduser()))
    if not any(name == current_name for name, _ in contexts):
        contexts.insert(0, (current_name, workspace))
    return contexts


def _git_state(workspace: Path) -> tuple[str, bool]:
    """Best-effort ``(branch, dirty)``; never raises, never blocks for long."""
    git = shutil.which("git")
    if git is None:
        return "", False
    branch = ""
    dirty = False
    try:
        res = subprocess.run(
            [git, "-C", str(workspace), "branch", "--show-current"],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if res.returncode == 0:
            # Git output must not pass through Windows' locale decoder (GBK).
            # Branch names are display-only metadata; tolerate non-UTF-8 bytes.
            branch = (res.stdout or b"").decode("utf-8", errors="replace").strip()
        res = subprocess.run(
            [git, "-C", str(workspace), "status", "--porcelain"],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        # Only presence matters here, so filenames need no decoding at all.
        if res.returncode == 0 and (res.stdout or b"").strip():
            dirty = True
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - best-effort
        return branch, dirty
    return branch, dirty


def _build_breakdown(items: list[tuple[str, int]], total: int) -> list[dict[str, Any]]:
    """Percent/offset/dash rows for the donut, computed from real token totals."""
    rows: list[dict[str, Any]] = []
    offset = 0.0
    for index, (name, tokens) in enumerate(items):
        pct = round(tokens / total * 100, 1) if total > 0 else 0.0
        rows.append(
            {
                "name": name,
                "tokens": tokens,
                "pct": pct,
                "color": _CHART_PALETTE[index % len(_CHART_PALETTE)],
                "offset": round(offset, 1),
                "dash": pct,
            }
        )
        offset += pct
    return rows


def get_usage_statistics(
    workspace_dir: Path | str | None = None,
    *,
    project: str = PROJECT_ALL,
    range_key: str = "today",
    start_date: str = "",
    end_date: str = "",
    model: str = "all",
    project_name: str | None = None,
    project_entries: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate real usage statistics for the selected catalog projects.

    Raises :class:`UsageStatsError` for an unknown ``project``/``range`` or a
    malformed custom date window (the host turns that into a 400).
    """
    ws = Path(workspace_dir).expanduser() if workspace_dir is not None else Path.cwd()
    try:
        ws = ws.resolve()
    except OSError:  # pragma: no cover - resolve is non-strict on modern Python
        pass
    current_name = (project_name or ws.name or str(ws)).strip()
    catalog_names = {
        str(entry.get("workspace_name") or entry.get("name") or "").strip()
        for entry in (project_entries or [])
    }
    if project not in ({PROJECT_ALL, current_name} | catalog_names):
        raise UsageStatsError(f"unknown project: {project!r}")

    raw_model = (model or "all").strip()
    model_filter = None if raw_model in ("", "all") else raw_model

    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    start, end = _resolve_window(range_key, start_date, end_date, today)

    contexts = _project_contexts(ws, current_name, project_entries)

    available_models: set[str] = set()
    for _, ctx_path in contexts:
        available_models.update(_collect_workspace_models(ctx_path))
    available_models.update(_collect_configured_models(ws))

    selected_contexts = contexts if project == PROJECT_ALL else [
        context for context in contexts if context[0] == project
    ]
    if not selected_contexts:
        raise UsageStatsError(f"unknown project: {project!r}")
    aggregates: list[tuple[str, Path, dict[str, Any]]] = []
    sessions_meta: dict[str, dict[str, Any]] = {}
    agg = _empty_aggregate()
    namespace_sessions = len(selected_contexts) > 1
    for name, context_path in selected_contexts:
        context_agg, context_sessions = _collect_workspace_aggregate(
            context_path, start, end, model_filter=model_filter
        )
        if namespace_sessions:
            context_agg = _namespace_aggregate(context_agg, name)
            context_sessions = {
                f"{name}:{thread_id}": meta for thread_id, meta in context_sessions.items()
            }
        _merge_aggregates(agg, context_agg)
        aggregates.append((name, context_path, context_agg))
        sessions_meta.update(context_sessions)

    total = agg["total"]
    total_tokens = int(total["tokens"])
    provider_input_total = int(total["cache_read"] + total["net_input"] + total["cache_write"])
    cache_hit_rate = (
        round(total["cache_read"] / provider_input_total * 100, 1)
        if provider_input_total > 0
        else None
    )
    saved_pct = (
        round(total["saved"] / total["baseline"] * 100, 1) if total["baseline"] > 0 else None
    )

    days_sorted = sorted(agg["days"].keys())

    if start is not None:
        range_start: str | None = start.isoformat()
    else:
        range_start = days_sorted[0] if days_sorted else None
    if end is not None:
        range_end: str | None = end.isoformat()
    else:
        range_end = days_sorted[-1] if days_sorted else None

    # The heatmap is one cell per *calendar* day, so it must be continuous: a gap
    # day is a real zero, not a missing column (a missing column shifts every
    # later cell's weekday and mislabels the grid).  ``all`` has no fixed window,
    # so it spans the first..last active day; either way the row is capped at the
    # last ``MAX_HEATMAP_DAYS`` days and the payload flags the truncation.  Only
    # the heatmap is capped -- the aggregate totals above still cover everything.
    heatmap_start: date | None = None
    heatmap_end: date | None = None
    if start is not None and end is not None:
        heatmap_start, heatmap_end = start, end
    elif days_sorted:
        heatmap_start = date.fromisoformat(days_sorted[0])
        heatmap_end = date.fromisoformat(days_sorted[-1])

    heatmap_days: list[dict[str, Any]] = []
    heatmap_truncated = False
    heatmap_hours: list[dict[str, Any]] = []
    heatmap_hours_truncated = False
    if heatmap_start is not None and heatmap_end is not None:
        heatmap_span = (heatmap_end - heatmap_start).days + 1
        if heatmap_span > MAX_HEATMAP_DAYS:
            heatmap_truncated = True
            heatmap_start = heatmap_end - timedelta(days=MAX_HEATMAP_DAYS - 1)
            heatmap_span = MAX_HEATMAP_DAYS
        for offset in range(heatmap_span):
            day = (heatmap_start + timedelta(days=offset)).isoformat()
            bucket = agg["days"].get(day)
            heatmap_days.append(
                {
                    "date": day,
                    "tokens": int(bucket["tokens"]) if bucket else 0,
                    "sessions": len(bucket["threads"]) if bucket else 0,
                }
            )

    # The hourly matrix is built from the buckets the scan kept (the window's last
    # ``MAX_HEATMAP_HOUR_DAYS`` local days), relabelled into the host's zone.  It is
    # independent of the daily strip above: rows carry their own date, so a day
    # without activity is simply absent instead of a padded row.
    heatmap_hours, heatmap_hours_truncated = _local_hour_matrix(agg["hour_days"])

    # Trend: hourly buckets for "today", daily buckets otherwise.
    if range_key == "today":
        trend_items = []
        for hour in range(24):
            bucket = agg["hours"].get(hour)
            trend_items.append(
                {
                    "date": f"{hour:02d}:00",
                    "cache": int(bucket["cache_read"]) if bucket else 0,
                    "input": (int(bucket["net_input"]) + int(bucket["cache_write"]))
                    if bucket
                    else 0,
                    "output": int(bucket["output"]) if bucket else 0,
                    "raw": int(bucket["baseline"]) if bucket else 0,
                }
            )
        granularity = "hour"
        title = "今日 Token 消耗（UTC 小时级）"
        subtitle = f"{today.isoformat()} 00:00 起 UTC 各小时真实用量（闭区间）"
    else:
        if start is not None and end is not None:
            span = (end - start).days + 1
            day_keys = [(start + timedelta(days=i)).isoformat() for i in range(span)]
        else:
            day_keys = days_sorted
        trend_items = []
        for day in day_keys:
            bucket = agg["days"].get(day)
            trend_items.append(
                {
                    "date": day[5:] if len(day) == 10 else day,
                    "cache": int(bucket["cache_read"]) if bucket else 0,
                    "input": (int(bucket["net_input"]) + int(bucket["cache_write"]))
                    if bucket
                    else 0,
                    "output": int(bucket["output"]) if bucket else 0,
                    "raw": int(bucket["baseline"]) if bucket else 0,
                }
            )
        granularity = "day"
        titles = {
            "7d": "近 7 天每日 Token 消耗",
            "30d": "近 30 天每日 Token 消耗",
            "all": "全部历史活跃日 Token 消耗",
            "custom": f"指定区间每日 Token 消耗（{range_start} ~ {range_end}）",
        }
        title = titles.get(range_key, "每日 Token 消耗")
        subtitle = "缓存读取 / 非缓存读取输入 / 输出 三层真实用量（UTC 闭区间）"
        if range_key == "all":
            # ``all`` has no fixed window, so only active days are plotted (an
            # empty day carries no information and the series must stay bounded).
            subtitle = "缓存读取 / 非缓存读取输入 / 输出（仅活跃日，UTC 闭区间）"

    model_rows = sorted(agg["models"].items(), key=lambda kv: kv[1], reverse=True)[:6]
    breakdown_model = _build_breakdown(model_rows, total_tokens)
    project_totals = [
        (name, int(context_agg["total"]["tokens"]))
        for name, _, context_agg in aggregates
        if context_agg["total"]["tokens"] > 0
    ]
    breakdown_project = _build_breakdown(project_totals, total_tokens)

    top_sessions: list[dict[str, Any]] = []
    ranked_threads = sorted(agg["threads"].items(), key=lambda kv: kv[1]["tokens"], reverse=True)[
        :MAX_TOP_SESSIONS
    ]
    for thread_id, stats in ranked_threads:
        meta = sessions_meta.get(thread_id) or {}
        provider_input = int(stats["provider_input"])
        top_sessions.append(
            {
                "thread_id": thread_id[:12],
                "title": meta.get("title") or thread_id[:12],
                "model": meta.get("model") or None,
                "turns": len(stats["turns"]),
                "tokens": int(stats["tokens"]),
                "cache_rate": (
                    round(int(stats["cache_read"]) / provider_input * 100, 1)
                    if provider_input > 0
                    else None
                ),
            }
        )

    top_tools: list[dict[str, Any]] = []
    for name, stats in sorted(agg["tools"].items(), key=lambda kv: kv[1]["count"], reverse=True)[
        :MAX_TOP_TOOLS
    ]:
        count = int(stats["count"])
        success = int(stats["success"])
        top_tools.append(
            {
                "name": name,
                "count": count,
                "success_count": success,
                "failure_count": int(stats["failure"]),
                "success_rate": round(success / count * 100, 1) if count else None,
                "avg_ms": None,
            }
        )

    project_matrix = []
    for name, context_path, context_agg in aggregates:
        branch, dirty = _git_state(context_path)
        context_tokens = int(context_agg["total"]["tokens"])
        project_matrix.append(
            {
                "name": name,
                "is_current": name == current_name,
                "path": str(context_path).replace("\\", "/"),
                "branch": branch or None,
                "dirty": dirty,
                "tokens": context_tokens,
                "share_pct": round(context_tokens / total_tokens * 100, 1)
                if total_tokens > 0
                else 0.0,
                "cost": None,
                "sessions_count": len(context_agg["threads"]),
                "turns_count": len(context_agg["turn_ids"]),
                "lines_added": None,
                "lines_removed": None,
                "efficiency": None,
            }
        )

    return {
        "generated_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "range": {"key": range_key, "start": range_start, "end": range_end},
        "project": {
            "selected": project,
            "connected_count": len(contexts),
            "current": {
                "name": current_name,
                "path": str(ws).replace("\\", "/"),
                "branch": branch or None,
                "dirty": dirty,
            },
        },
        "kpi": {
            "total_tokens": total_tokens,
            "provider_input_tokens": provider_input_total,
            "net_input_tokens": int(total["net_input"]),
            "cache_read_tokens": int(total["cache_read"]),
            "cache_write_tokens": int(total["cache_write"]),
            "output_tokens": int(total["output"]),
            "cache_hit_rate": cache_hit_rate,
            "saved_tokens": int(total["saved"]),
            "saved_pct": saved_pct,
            "call_count": int(total["calls"]),
            "turn_count": len(agg["turn_ids"]),
            "active_duration_ms": int(total["duration_ms"]),
            # No pricing source: reported as null, never fabricated.
            "estimated_cost": None,
            "loc_added": None,
            "loc_removed": None,
        },
        "project_matrix": project_matrix,
        "heatmap": {
            "days": heatmap_days,
            "truncated": heatmap_truncated,
            "hourly": {
                "rows": heatmap_hours,
                "truncated": heatmap_hours_truncated,
                # The host's current zone, so the panel can name what the 24
                # columns mean instead of claiming UTC.
                "timezone": _local_offset_label(),
            },
        },
        "trend": {
            "range_key": range_key,
            "granularity": granularity,
            "title": title,
            "subtitle": subtitle,
            "items": trend_items,
        },
        "breakdowns": {
            "project": breakdown_project,
            "model": breakdown_model,
            # No reliable per-agent attribution exists in these stores.
            "agent": None,
        },
        "top_tools": {
            "supported": True,
            "partial": True,
            "source": "tool_output_refs",
            "scope_note": (
                "仅统计被压缩并写入引用存储的工具输出记录，不代表全部工具调用；平均耗时无数据来源。"
            ),
            "recorded_total": int(agg["tool_records"]),
            "items": top_tools,
        },
        "top_sessions": top_sessions,
        "available_models": sorted(m for m in available_models if m),
        "selected_model": raw_model,
        "notes": [
            "累计吞吐 = provider_input + output（provider_input 含缓存读取与缓存写入）；"
            "净输入 = provider_input - cache_read - cache_write。",
            "趋势中『非缓存读取输入』= provider_input - cache_read（含 cache_write），"
            "与缓存读取、输出共同构成累计吞吐；输出已含 reasoning，不重复累加。",
            "所有时间均为 UTC，日期区间为闭区间；自定义区间最长 "
            f"{MAX_CUSTOM_SPAN_DAYS} 天，热力图最多显示最近 {MAX_HEATMAP_DAYS} 天（不影响累计）。",
            f"小时热力矩阵按宿主本地时区（当前 {_local_offset_label()}）聚合，"
            f"最多显示最近 {MAX_HEATMAP_HOUR_DAYS} 个有活动的本地日、每天 24 小时"
            "（不影响累计；其余面板仍为 UTC 闭区间）。",
            "费用、代码行数、工具平均耗时当前无数据来源，显示为 —。",
            "工具维度仅覆盖被压缩并写入引用存储的工具输出，不代表全部工具调用。",
            "Agent/角色维度当前无数据来源，已停用。",
            "统计范围来自宿主 catalog 的可见项目；浏览器不能提交任意工作区路径。",
        ],
    }
