"""One-shot Python subprocess PTC (programmatic tool calling) runtime.

The public surface is :func:`run_code` and :class:`PtcLimits`.  ``worker.py`` is
a subprocess entry point and is intentionally *not* imported here.
"""

from __future__ import annotations

from .process import run_code
from .protocol import PtcLimits

__all__ = ["PtcLimits", "run_code"]
