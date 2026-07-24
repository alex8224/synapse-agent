"""Windows path normalization regression tests for Codex session discovery."""

from __future__ import annotations

from pathlib import Path

from synapse.codex_sessions import _same_path


def test_scanner_accepts_windows_extended_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    extended_workspace = Path("\\\\?\\" + str(workspace.resolve()))

    assert _same_path(extended_workspace, workspace)

