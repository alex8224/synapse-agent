"""In-process Python heap snapshot via a trigger file.

A daemon watchdog polls a trigger file every few seconds. When the file
appears (created externally), the running process snapshots its own Python
object heap (``gc.get_objects`` + ``sys.getsizeof`` aggregated by type) and
writes the JSON result next to the trigger file. This lets an external tool
inspect the true in-process allocation breakdown without attaching a
debugger or injecting code into the running process.

Usage::

    # inside synapse (enabled by default from cli.main)
    from synapse.observability.heap_dump import start_heap_dump_watchdog
    start_heap_dump_watchdog()

    # externally, to trigger a snapshot:
    #   New-Item -ItemType File %TEMP%\\synapse_heap_dump.trigger
    # then read the generated ``%TEMP%\\synapse_heap_dump.trigger.json``
"""

from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import threading
from collections import defaultdict
from typing import Any

POLL_INTERVAL_SECONDS = 2.0
TRIGGER_ENV = "SYNAPSE_HEAP_DUMP_TRIGGER"

# Sizes above which objects are reported individually (bytes).
_BIG_OBJECT_THRESHOLD = 1_000_000
_BIG_STRING_THRESHOLD = 500_000
_BIG_LIST_THRESHOLD = 100_000
_BIG_DICT_THRESHOLD = 50_000


def _sizeof_safe(obj: Any) -> int:
    try:
        return sys.getsizeof(obj)
    except Exception:  # noqa: BLE001 - some objects refuse __sizeof__
        return 0


def _process_memory_info() -> dict[str, int]:
    """Best-effort process-level memory in bytes, stdlib only, cross-platform.

    ``gc.get_objects()``-based stats only cover Python-managed objects; the
    real process footprint additionally includes untracked scalars, C-extension
    out-of-heap allocations, interpreter/DLL overhead and allocator waste. This
    function adds the OS-level numbers so dumps can show the full picture.
    """
    info: dict[str, int] = {}
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCountersEx(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            # GetProcessMemoryInfo lives in psapi.dll (kernel32 only exports
            # K32GetProcessMemoryInfo); declare argtypes/restype so the 64-bit
            # HANDLE is not truncated.
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_ProcessMemoryCountersEx),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = _ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(_ProcessMemoryCountersEx)
            if psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                info["private_bytes"] = counters.PrivateUsage
                info["working_set"] = counters.WorkingSetSize
                info["peak_working_set"] = counters.PeakWorkingSetSize
                info["pagefile"] = counters.PagefileUsage
        except Exception:  # noqa: BLE001 - best-effort fallback
            pass
    else:
        try:
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        info["rss"] = int(line.split()[1]) * 1024
                    elif line.startswith("VmSize:"):
                        info["vms"] = int(line.split()[1]) * 1024
                    elif line.startswith("VmHWM:"):
                        info["peak_rss"] = int(line.split()[1]) * 1024
        except OSError:
            pass
        try:
            import resource

            # ru_maxrss: KiB on Linux, bytes on macOS.
            unit = 1024 if sys.platform.startswith("linux") else 1
            info.setdefault("peak_rss", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit)
        except Exception:  # noqa: BLE001 - best-effort fallback
            pass
    return info


def _collect_scalar_stats(
    roots: list[Any],
) -> tuple[dict[str, list[int]], list[list[Any]]]:
    """Walk containers reachable from gc-tracked roots, summing scalar payloads.

    ``gc.get_objects()`` only returns *tracked* objects, so plain ``str`` /
    ``bytes`` / ``bytearray`` values are invisible unless reached through a
    tracked container. This walk visits the closure of tracked containers
    (deduplicated by identity) and aggregates those scalar values. The result
    is an approximation: scalars held only by untracked objects, C-level
    references or local frames are not covered.
    """
    sums: dict[str, list[int]] = {
        "str": [0, 0],
        "bytes": [0, 0],
        "bytearray": [0, 0],
    }
    big_scalars: list[list[Any]] = []
    seen: set[int] = set()
    stack: list[Any] = list(roots)
    while stack:
        o = stack.pop()
        oid = id(o)
        if oid in seen:
            continue
        seen.add(oid)
        t = type(o)
        if t is str:
            sz = _sizeof_safe(o)
            sums["str"][0] += 1
            sums["str"][1] += sz
            if sz >= _BIG_STRING_THRESHOLD:
                big_scalars.append(["str", len(o), sz, o[:150]])
        elif t is bytes:
            sz = _sizeof_safe(o)
            sums["bytes"][0] += 1
            sums["bytes"][1] += sz
            if sz >= _BIG_STRING_THRESHOLD:
                big_scalars.append(["bytes", len(o), sz, ""])
        elif t is bytearray:
            sz = _sizeof_safe(o)
            sums["bytearray"][0] += 1
            sums["bytearray"][1] += sz
            if sz >= _BIG_STRING_THRESHOLD:
                big_scalars.append(["bytearray", len(o), sz, ""])
        elif t is list or t is tuple:
            stack.extend(o)
        elif t is dict:
            stack.extend(o.keys())
            stack.extend(o.values())
        elif t is set or t is frozenset:
            stack.extend(o)
    return sums, big_scalars


def collect_heap_stats() -> dict[str, Any]:
    """Collect in-process heap statistics as a JSON-serializable dict."""
    out: dict[str, Any] = {}
    out["pid"] = os.getpid()
    out["python"] = sys.version.split()[0]
    out["executable"] = sys.executable
    out["process_memory"] = _process_memory_info()
    out["gc_stats"] = gc.get_stats()
    objs = gc.get_objects()
    out["gc_objects"] = len(objs)
    out["gc_garbage"] = len(gc.garbage)

    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    big: list[list[Any]] = []
    big_objs: list[list[Any]] = []
    for o in objs:
        t = type(o)
        sz = _sizeof_safe(o)
        key = f"{t.__module__}.{t.__qualname__}"
        entry = by_type[key]
        entry[0] += 1
        entry[1] += sz
        if sz >= _BIG_OBJECT_THRESHOLD:
            try:
                snippet = o[:120] if isinstance(o, str) else repr(o)[:120]
            except Exception:  # noqa: BLE001 - repr can fail for odd objects
                snippet = "<repr failed>"
            big.append([key, sz, snippet])
            big_objs.append([key, sz, o])

    out["top_types"] = sorted(
        ([k, v[0], v[1]] for k, v in by_type.items()), key=lambda x: -x[2]
    )[:80]
    out["big_objects"] = sorted(big, key=lambda x: -x[1])[:80]

    container_sums: dict[str, list[int]] = {}
    for tname in ("list", "tuple", "dict", "set", "frozenset"):
        total = 0
        count = 0
        for o in objs:
            if type(o).__name__ == tname:
                total += _sizeof_safe(o)
                count += 1
        container_sums[tname] = [count, total]
    scalar_sums, big_scalars = _collect_scalar_stats(objs)
    container_sums.update(scalar_sums)
    out["container_bytes"] = container_sums

    big_str: list[list[Any]] = []
    big_bytes: list[list[Any]] = []
    for kind, length, sz, snippet in big_scalars:
        entry = [length, sz, snippet] if kind == "str" else [kind, length, sz]
        (big_str if kind == "str" else big_bytes).append(entry)
    out["big_str"] = sorted(big_str, reverse=True)[:50]
    out["big_bytes"] = sorted(big_bytes, reverse=True)[:50]

    big_lists: list[list[Any]] = []
    big_dicts: list[list[Any]] = []
    for o in objs:
        if isinstance(o, list) and len(o) >= _BIG_LIST_THRESHOLD:
            big_lists.append([len(o), _sizeof_safe(o), repr(o)[:100]])
        elif isinstance(o, dict) and len(o) >= _BIG_DICT_THRESHOLD:
            big_dicts.append([len(o), _sizeof_safe(o)])
    out["big_lists"] = sorted(big_lists, reverse=True)[:30]
    out["big_dicts"] = sorted(big_dicts, reverse=True)[:30]

    referrers: list[list[Any]] = []
    for key, sz, obj in sorted(big_objs, key=lambda x: -x[1])[:20]:
        refs: list[str] = []
        try:
            for r in gc.get_referrers(obj)[:3]:
                refs.append(f"{type(r).__module__}.{type(r).__qualname__}")
        except Exception:  # noqa: BLE001 - referrer scan may hit odd objects
            pass
        referrers.append([key, sz, refs])
    out["top20_with_referrers"] = referrers
    return out


def default_trigger_path() -> str:
    """Trigger file path used when ``SYNAPSE_HEAP_DUMP_TRIGGER`` is unset."""
    return os.path.join(tempfile.gettempdir(), "synapse_heap_dump.trigger")


def start_heap_dump_watchdog(trigger_path: str | None = None) -> threading.Thread | None:
    """Start a daemon watchdog that snapshots the heap when a trigger file appears.

    The watchdog only stats one file every ``POLL_INTERVAL_SECONDS``, so normal
    runs are unaffected. Returns the watchdog thread, or ``None`` when disabled.
    """
    if trigger_path is None:
        trigger_path = os.environ.get(TRIGGER_ENV) or default_trigger_path()
    if not trigger_path:
        return None

    def _watch() -> None:
        while True:
            try:
                if os.path.exists(trigger_path):
                    output = f"{trigger_path}.json"
                    try:
                        data = collect_heap_stats()
                        with open(output, "w", encoding="utf-8") as fh:
                            json.dump(data, fh, ensure_ascii=False, indent=1)
                    finally:
                        try:
                            os.remove(trigger_path)
                        except OSError:
                            pass
            except Exception:  # noqa: BLE001 - watchdog must never crash
                pass
            threading.Event().wait(POLL_INTERVAL_SECONDS)

    thread = threading.Thread(target=_watch, daemon=True, name="heap-dump-watchdog")
    thread.start()
    return thread
