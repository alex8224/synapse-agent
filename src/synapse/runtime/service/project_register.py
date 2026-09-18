"""Project-registration DTOs for the Agent Runtime Service (v1 additive).

This module belongs to the contract layer: one frozen request dataclass plus the
bounded path limit.  It imports no catalog, transport, settings, UI, or
session-execution module, so the wire decoder, the in-process service, and the
daemon composition root can all depend on it without introducing a cycle.

The request carries a *host* filesystem path.  The service layer never resolves
or registers it itself: the daemon injects a catalog-backed registrar adapter,
so this module stays free of the project catalog.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MAX_WORKSPACE_PATH_BYTES",
    "RegisterProjectCommand",
]

#: One workspace path may not exceed 4096 UTF-8 bytes.
MAX_WORKSPACE_PATH_BYTES = 4096


@dataclass(frozen=True, slots=True)
class RegisterProjectCommand:
    """Register (or re-register) one host workspace directory as a project.

    Re-registering an already-known path reuses its stable ``project_id`` (the
    catalog upserts by workspace path), so the console can call this
    idempotently.
    """

    workspace_path: str

    def __post_init__(self) -> None:
        if type(self.workspace_path) is not str:
            raise ValueError("workspace_path must be a string")
        path = self.workspace_path.strip()
        if not path or "\x00" in path:
            raise ValueError("workspace_path must be a non-empty path without NUL")
        if len(path.encode("utf-8")) > MAX_WORKSPACE_PATH_BYTES:
            raise ValueError("workspace_path exceeds the size limit")
        object.__setattr__(self, "workspace_path", path)
