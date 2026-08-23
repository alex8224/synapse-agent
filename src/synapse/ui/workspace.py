"""Lightweight workspace-path resolution for UI image rendering.

The file-tool and model-facing tools resolve paths against the session
workspace; the Rich / Textual render path needs the same anchor so a Markdown
image reference like ``![alt](chart.png)`` is found regardless of the process
working directory (started from anywhere, or after a project switch).
"""

from __future__ import annotations

from pathlib import Path

_ACTIVE_WORKSPACE: Path | None = None


def set_current_workspace(workspace: Path | str | None) -> None:
    """Pin the workspace used for UI image-path resolution.

    Call this where the active workspace is (re)established — TUI startup and
    in-process project switches. Passing ``None`` clears the override so
    ``current_workspace`` falls back to the process working directory.
    """
    global _ACTIVE_WORKSPACE
    _ACTIVE_WORKSPACE = Path(workspace).resolve() if workspace is not None else None


def clear_workspace_cache() -> None:
    """Drop the pinned workspace so resolution falls back to the cwd."""
    set_current_workspace(None)


def current_workspace() -> Path:
    """Return the active workspace, or the cwd when none is pinned."""
    return _ACTIVE_WORKSPACE if _ACTIVE_WORKSPACE is not None else Path.cwd().resolve()


def resolve_workspace_path(
    raw: str | None,
    *,
    workspace: Path | str | None = None,
) -> Path | None:
    """Resolve ``raw`` against the workspace, or ``None`` for URLs/missing files.

    ``raw`` may be workspace-relative or absolute. HTTP(S) URLs and files that
    do not exist return ``None`` so callers can fall back to an image
    placeholder. ``workspace`` overrides the process workspace for tests and
    explicit contexts.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text or text.startswith(("http://", "https://")):
        return None
    root = Path(workspace).resolve() if workspace is not None else current_workspace()
    path = Path(text)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
    except OSError:
        return None
    if not path.is_file():
        return None
    return path
