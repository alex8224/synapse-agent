"""Real-model acceptance check for the workflow engine (opt-in).

Skipped unless ``SYNAPSE_WORKFLOW_E2E`` is set, because it needs provider credentials and
spends tokens.  With the variable set it runs ``scripts/workflow_e2e.py``, which drives one
whole run against the configured model: draft, approval, worker subprocess, actor call,
host validation and usage accounting.

```powershell
$env:SYNAPSE_WORKFLOW_E2E = '1'
uv run --no-sync pytest tests/test_workflows_e2e.py -q
```
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.environ.get("SYNAPSE_WORKFLOW_E2E"),
    reason="needs provider credentials; set SYNAPSE_WORKFLOW_E2E=1 to run",
)


def test_the_engine_runs_one_real_workflow_end_to_end() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "workflow_e2e.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    # The script prints the outcome it verified; asserting on it keeps this test honest
    # about what "passed" means.
    assert "outcome: completed" in completed.stdout
    assert "result: {'items': ['ok']}" in completed.stdout


def test_the_engine_fixes_a_file_and_verifies_it_end_to_end() -> None:
    """The write path: a real actor edits a throwaway workspace and the engine branches on it.
    The script creates its own temporary workspace, so nothing in the repository is touched;
    it asserts the fix independently by running the workspace's own check itself.
    """
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "workflow_write_e2e.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=1800,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "outcome: completed" in completed.stdout
    assert '"passed": true' in completed.stdout
    # The check the script ran itself, after the workflow finished.
    assert "self-check: rc=0" in completed.stdout
