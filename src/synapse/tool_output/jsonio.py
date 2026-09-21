"""Fast JSON for tool-output and compression event payloads.

Those payloads are large -- a compression event is 30-90 KB, dominated by
``live_zone_plan`` and ``wire_fingerprints`` -- and they are written once per
model call and re-read by the usage dashboard and the ``/compression`` tools, so
the codec shows up in profiles: measured on real payloads, ``orjson`` parses at
~247 MB/s against the standard library's ~110 MB/s (2.3x) and serialises at
~730 MB/s against ~104 MB/s (7x).

``orjson`` is the accelerator here, never a requirement: if the wheel is
unavailable (or on PyPy, where it is not published) this module falls back to the
standard library, and the only thing that changes is speed.  Both paths produce
JSON the other can read -- ``orjson`` always emits UTF-8, which is what the
``ensure_ascii=False`` calls it replaces asked for.
"""
from __future__ import annotations

import json
from typing import Any

try:  # pragma: no cover - which branch runs depends on the installed wheel
    import orjson
except ImportError:  # pragma: no cover
    orjson = None

#: Whether the accelerated codec is in use (diagnostics and tests read this).
USING_ORJSON = orjson is not None


def dumps(value: Any) -> str:
    """Serialise ``value`` to JSON text, leaving non-ASCII characters unescaped.

    The stores keep these payloads in TEXT columns, so ``orjson``'s UTF-8 bytes
    are decoded back to ``str``.  ``OPT_NON_STR_KEYS`` preserves the standard
    library's coercion of non-string mapping keys (integer-keyed breakdown maps
    exist in these events) instead of raising.
    """
    if orjson is None:
        return json.dumps(value, ensure_ascii=False)
    return orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")


def loads(text: str | bytes) -> Any:
    """Parse JSON text.

    Malformed input raises ``ValueError`` on both paths (``orjson`` raises its
    own ``JSONDecodeError``, which is a ``ValueError`` subclass), so callers keep
    catching one exception type.
    """
    if orjson is None:
        return json.loads(text)
    return orjson.loads(text)
