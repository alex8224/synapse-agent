from __future__ import annotations

import json
import time
from pathlib import Path

import synapse.observability.heap_dump as heap_dump
from synapse.observability.heap_dump import collect_heap_stats, start_heap_dump_watchdog


def test_collect_heap_stats_shape() -> None:
    stats = collect_heap_stats()
    assert stats["gc_objects"] > 0
    assert stats["top_types"], "top_types must not be empty"
    # top_types entries are [type, count, bytes] sorted by bytes descending
    sizes = [entry[2] for entry in stats["top_types"]]
    assert sizes == sorted(sizes, reverse=True)
    assert stats["container_bytes"]["str"][0] > 0
    assert stats["container_bytes"]["str"][1] > 0
    assert isinstance(stats["gc_stats"], list)
    # Process-level memory info must be present and non-negative on all platforms.
    assert stats["process_memory"], "process_memory must not be empty"
    for value in stats["process_memory"].values():
        assert isinstance(value, int) and value >= 0


def test_watchdog_trigger_produces_dump(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setattr(heap_dump, "POLL_INTERVAL_SECONDS", 0.1)
    trigger = tmp_path / "heap.trigger"
    thread = start_heap_dump_watchdog(str(trigger))
    assert thread is not None
    assert heap_dump.POLL_INTERVAL_SECONDS == 0.1
    try:
        trigger.write_text("go", encoding="utf-8")
        output = tmp_path / "heap.trigger.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if output.exists():
                break
            time.sleep(0.2)
        assert output.exists(), "watchdog did not produce the dump file"
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["gc_objects"] > 0
        assert "top_types" in data
        assert not trigger.exists(), "trigger file should be consumed"
    finally:
        thread.join(timeout=1)
