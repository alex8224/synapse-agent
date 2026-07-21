"""Workspace path helpers for virtual-mode file tools."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Common filesystem tool argument keys used by deepagents / our tools.
_PATH_KEYS = (
    "path",
    "file_path",
    "filename",
    "file",
    "target",
    "target_file",
    "source",
    "src",
    "dst",
    "destination",
    "directory",
    "dir",
    "glob",
    "pattern_path",
)


_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_EXTENDED_PREFIX = re.compile(r"^(?:\\\\\?\\|//\?/)", re.IGNORECASE)


def is_windows_absolute(path: str) -> bool:
    s = _strip_windows_extended_prefix(str(path))
    return bool(_WIN_DRIVE.match(s)) or s.startswith("\\\\")


def _strip_windows_extended_prefix(path: str) -> str:
    r"""Convert Windows ``\\?\`` paths to their normal drive / UNC form."""
    s = str(path).strip()
    if not _WIN_EXTENDED_PREFIX.match(s):
        return s
    remainder = _WIN_EXTENDED_PREFIX.sub("", s, count=1)
    if remainder.upper().startswith("UNC\\"):
        return "\\\\" + remainder[4:]
    return remainder


def is_virtual_path(path: str) -> bool:
    """True for POSIX-style virtual paths like ``/src/a.py`` (not ``C:/...``)."""
    s = str(path).strip()
    if not s.startswith("/"):
        return False
    # Reject /C:/... style mistakes
    if len(s) >= 3 and s[2] == ":" and s[1].isalpha():
        return False
    return True


def to_virtual_path(path: str | os.PathLike[str] | None, workspace: Path) -> str | None:
    """Map host paths under workspace to virtual paths starting with ``/``.

    Examples (workspace = F:/project/repo):
    - ``F:/project/repo/src/a.py`` -> ``/src/a.py``
    - ``src/a.py`` -> ``/src/a.py``
    - ``/src/a.py`` -> ``/src/a.py`` (unchanged)
    - ``/`` -> ``/``
    """
    if path is None:
        return None
    s = _strip_windows_extended_prefix(str(path))
    if not s:
        return s

    if is_virtual_path(s):
        return s if s.startswith("/") else f"/{s}"

    root = Path(workspace).resolve()

    # Windows absolute / UNC / host absolute under workspace root
    if is_windows_absolute(s) or os.path.isabs(s):
        try:
            cand = Path(s)
            # resolve() may fail for non-existing; still ok for relative_to with abs paths
            try:
                cand_res = cand.resolve()
            except OSError:
                cand_res = cand
            try:
                rel = cand_res.relative_to(root)
                rel_s = rel.as_posix()
                return "/" if rel_s in {"", "."} else f"/{rel_s}"
            except ValueError:
                # Outside workspace: leave as-is
                return s
        except Exception:  # noqa: BLE001
            return s

    # Relative host path -> virtual
    rel = s.replace("\\", "/").lstrip("./")
    return "/" + rel if rel else "/"


def rewrite_tool_args_paths(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Return a shallow-copied args dict with path-like fields virtualized."""
    out = dict(args)
    for key in _PATH_KEYS:
        if key not in out or out[key] is None:
            continue
        val = out[key]
        if isinstance(val, str):
            out[key] = to_virtual_path(val, workspace)
        elif isinstance(val, list):
            out[key] = [
                to_virtual_path(v, workspace) if isinstance(v, str) else v for v in val
            ]
    return out


def summarize_tool_result(content: Any, *, limit: int = 80) -> str:
    """Compact tool-result status for CLI (no file body dump)."""
    try:
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        elif isinstance(content, (bytes, bytearray)):
            text = bytes(content).decode("utf-8", errors="replace")
        else:
            text = str(content)
    except Exception:  # noqa: BLE001
        text = ""
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped:
        return "ok"
    lower = stripped.lower()
    if lower.startswith("error") or "not supported" in lower or "traceback" in lower:
        # Keep short error reason only
        one = stripped.replace("\n", " ")
        if len(one) > limit:
            one = one[: limit - 3] + "..."
        return one
    # Success: size only, never dump body.
    n = len(stripped)
    lines = stripped.count("\n") + 1
    return f"ok ({n} chars, {lines} lines)"