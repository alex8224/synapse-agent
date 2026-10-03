"""A real daemon subprocess and a real agent graph; only the chat model is scripted.

The daemon entry, the two read-only fixture projects and the scripted model live in
``scripts/verify_session_collaboration.py``, so this regression and the browser
acceptance exercise exactly one harness.  What is asserted here is the wire-level
outcome of the whole loop: the peer really reads its own workspace file, replies
explicitly, the source really continues, and a restart of the composition neither
loses the transcripts nor replays the request.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_session_collaboration.py"


def _harness() -> Any:
    spec = importlib.util.spec_from_file_location(
        "collaboration_acceptance", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cross_project_collaboration_survives_daemon_restart(tmp_path: Path) -> None:
    asyncio.run(
        _harness().verify(
            25, server_entry=SCRIPT, server_args=["--scripted"], root=tmp_path,
        )
    )
