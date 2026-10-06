"""Wire protocol and resource limits for the one-shot PTC runtime.

PTC (programmatic tool calling) runs a model-authored ``async`` function body in
a *fresh* Python subprocess per invocation.  The host (``process.py``) and the
isolated worker (``worker.py``) exchange newline-delimited JSON (NDJSON) frames
over the worker's stdin/stdout.  The worker's stdout is reserved for the
protocol, so user ``print`` output is redirected into ``log`` frames and cannot
corrupt the stream.

This module is deliberately stdlib-only: the worker is started with
``python -I -u`` (isolated mode drops ``PYTHONPATH`` and the script directory)
and reaches this module through a fixed sibling path.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

# Frame type tags exchanged as newline-delimited JSON.
FRAME_INIT = "init"
FRAME_LOG = "log"
FRAME_TOOL_CALL = "tool_call"
FRAME_TOOL_RESULT = "tool_result"
FRAME_RESULT = "result"

# Validation bounds.  They are far above the defaults so an integrator can tune
# a run but cannot ask the host to allocate unbounded memory or wait forever.
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 3600.0
_MIN_MAX_CALLS = 1
_MAX_MAX_CALLS = 10_000
_MIN_MAX_PARALLEL = 1
_MAX_MAX_PARALLEL = 64
# ``max_output_bytes`` is the *combined* outer cap on the final
# ``{logs, value, error?}`` JSON.  Its floor only needs to leave room for the
# envelope plus a bounded error, so it can be much smaller than the per-result
# cap.  ``256`` comfortably fits ``{"logs":[],"value":null,"error":{...}}``.
_MIN_MAX_OUTPUT_BYTES = 256
_MAX_MAX_OUTPUT_BYTES = 16_000_000
_MIN_MAX_RESULT_BYTES = 1024
_MAX_MAX_RESULT_BYTES = 64_000_000
_MIN_MAX_CODE_BYTES = 256
_MAX_MAX_CODE_BYTES = 1_000_000


def _require_finite_float(name: str, value: Any, low: float, high: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if number < low or number > high:
        raise ValueError(f"{name} must be between {low} and {high}, got {number!r}")


def _require_int(name: str, value: Any, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    if value < low or value > high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value!r}")


@dataclass(frozen=True)
class PtcLimits:
    """Bounded resource budget for a single PTC run.

    ``max_output_bytes`` is the **combined** outer cap: the final
    ``{logs, value, error?}`` mapping is serialised exactly the way the host and
    middleware do (``ensure_ascii=False``, compact separators) and its UTF-8
    length must fit.  A run whose result would exceed it fails with an explicit
    ``output_limit`` error and a log prefix that fits, never a silently
    truncated success.

    ``max_result_bytes`` only bounds the *intermediate* values exchanged inside
    the run: an individual tool result and the ``result`` frame.  A larger tool
    result is reported to the script as a ``ToolCallError`` instead of being
    silently truncated.  It is intentionally not the model-context cap.
    """

    timeout_seconds: float = 120.0
    max_calls: int = 100
    max_parallel: int = 8
    max_output_bytes: int = 64000
    max_result_bytes: int = 4000000
    max_code_bytes: int = 64000

    def __post_init__(self) -> None:
        _require_finite_float(
            "timeout_seconds",
            self.timeout_seconds,
            _MIN_TIMEOUT_SECONDS,
            _MAX_TIMEOUT_SECONDS,
        )
        _require_int("max_calls", self.max_calls, _MIN_MAX_CALLS, _MAX_MAX_CALLS)
        _require_int(
            "max_parallel",
            self.max_parallel,
            _MIN_MAX_PARALLEL,
            _MAX_MAX_PARALLEL,
        )
        _require_int(
            "max_output_bytes",
            self.max_output_bytes,
            _MIN_MAX_OUTPUT_BYTES,
            _MAX_MAX_OUTPUT_BYTES,
        )
        _require_int(
            "max_result_bytes",
            self.max_result_bytes,
            _MIN_MAX_RESULT_BYTES,
            _MAX_MAX_RESULT_BYTES,
        )
        _require_int(
            "max_code_bytes",
            self.max_code_bytes,
            _MIN_MAX_CODE_BYTES,
            _MAX_MAX_CODE_BYTES,
        )

    def as_frame(self) -> dict[str, Any]:
        """Return the JSON-friendly mapping sent in the ``init`` frame."""
        return {
            "timeout_seconds": float(self.timeout_seconds),
            "max_calls": int(self.max_calls),
            "max_parallel": int(self.max_parallel),
            "max_output_bytes": int(self.max_output_bytes),
            "max_result_bytes": int(self.max_result_bytes),
            "max_code_bytes": int(self.max_code_bytes),
        }


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON value {token!r} is not allowed")


def encode_frame(frame: dict[str, Any]) -> bytes:
    """Serialise a frame to a single NDJSON line (bytes, trailing newline)."""
    return encode_json(frame) + b"\n"


def encode_json(value: Any) -> bytes:
    """Serialise a JSON value with the protocol's canonical settings.

    Host, worker and middleware must measure the combined ``max_output_bytes``
    budget with the *same* encoding, so every JSON size check routes through
    this helper: ``ensure_ascii=False`` keeps multibyte text at its real UTF-8
    length, compact separators drop cosmetic whitespace, and ``allow_nan=False``
    rejects non-finite numbers.
    """
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return text.encode("utf-8")


def decode_frame(line: bytes | bytearray | str) -> dict[str, Any]:
    """Parse one NDJSON frame, rejecting non-finite numbers and non-objects."""
    text = bytes(line).decode("utf-8") if isinstance(line, (bytes, bytearray)) else line
    value = json.loads(text, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("protocol frame must be a JSON object")
    return value
