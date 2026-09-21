"""Shared test isolation.

The ACP adapter writes session metadata into two stores: its own user-scoped
catalog (``~/.synapse/acp-sessions.sqlite``) and the shared session store of the
workspace a session was created in.  A test that injects neither a catalog path
nor a settings factory therefore writes into the developer's real data, which is
how blank ``session sess_<uuid>`` rows ended up in a real project store.  The
catalog default is redirected here for every ACP test module; the workspace store
is isolated by injecting ``IsolatedACPSettings`` (``tests/acp_service_fakes.py``)
into the agent under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_acp_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Point the ACP default catalog at a per-test file for ACP tests."""
    module = Path(request.node.nodeid.split("::")[0]).name
    if not module.startswith("test_acp"):
        return
    from synapse.acp import lifecycle

    monkeypatch.setattr(
        lifecycle, "default_catalog_path", lambda: tmp_path / "acp-catalog.sqlite"
    )
