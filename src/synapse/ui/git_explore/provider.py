"""Load old/new text for a changed file (working / staged / unstaged)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DiffMode = Literal["working", "staged", "unstaged"]
DIFF_MODES: tuple[DiffMode, ...] = ("working", "staged", "unstaged")

# Soft limits for TUI readability (bytes / lines of each side).
_MAX_BYTES = 512 * 1024
_MAX_LINES = 8_000

_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".css": "css",
    ".html": "html",
    ".xml": "xml",
    ".sql": "sql",
}


@dataclass(frozen=True, slots=True)
class DiffPayload:
    """Old/new text for one path under a compare mode."""

    path: str
    text_a: str
    text_b: str
    mode: DiffMode = "working"
    language_hint: str | None = None
    binary: bool = False
    truncated: bool = False
    error: str | None = None
    missing_a: bool = False
    missing_b: bool = False


def language_hint_for_path(path: str) -> str | None:
    ext = Path(path or "").suffix.lower()
    return _EXT_LANG.get(ext)


def _run_git_bytes(args: list[str], *, cwd: Path, timeout: float = 2.0) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or b""


def _run_git_text(args: list[str], *, cwd: Path, timeout: float = 2.0) -> str | None:
    raw = _run_git_bytes(args, cwd=cwd, timeout=timeout)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _is_binary(data: bytes) -> bool:
    if not data:
        return False
    # NUL in first 8KiB is a strong binary signal.
    sample = data[:8192]
    return b"\x00" in sample


def _decode_and_cap(data: bytes | None) -> tuple[str, bool, bool]:
    """Return ``(text, binary, truncated)``."""
    if data is None:
        return "", False, False
    if _is_binary(data):
        return "", True, False
    truncated = False
    body = data
    if len(body) > _MAX_BYTES:
        body = body[:_MAX_BYTES]
        truncated = True
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if len(lines) > _MAX_LINES:
        text = "".join(lines[:_MAX_LINES])
        if not text.endswith("\n"):
            text += "\n"
        truncated = True
    return text, False, truncated


def _read_worktree(cwd: Path, path: str) -> tuple[str, bool, bool, bool]:
    """Return ``(text, binary, truncated, missing)``."""
    full = cwd / path
    try:
        if not full.is_file():
            return "", False, False, True
        data = full.read_bytes()
    except Exception:  # noqa: BLE001
        return "", False, False, True
    text, binary, truncated = _decode_and_cap(data)
    return text, binary, truncated, False


def _read_blob(cwd: Path, spec: str) -> tuple[str, bool, bool, bool]:
    """Read ``git show <spec>``. Returns ``(text, binary, truncated, missing)``."""
    raw = _run_git_bytes(["show", spec], cwd=cwd, timeout=2.0)
    if raw is None:
        return "", False, False, True
    text, binary, truncated = _decode_and_cap(raw)
    return text, binary, truncated, False


def load_file_diff(
    cwd: Path | str,
    path: str,
    *,
    mode: DiffMode = "working",
    is_untracked: bool = False,
) -> DiffPayload:
    """Load left/right text for ``path`` under ``mode``.

    Semantics:
    - working:  HEAD:path  vs worktree
    - staged:   HEAD:path  vs index (:path)
    - unstaged: index (:path) vs worktree
    - untracked: empty vs worktree (any mode)
    """
    root = Path(cwd)
    rel = (path or "").replace("\\", "/").lstrip("./")
    if not rel:
        return DiffPayload(
            path=path or "",
            text_a="",
            text_b="",
            mode=mode,
            error="empty path",
        )

    lang = language_hint_for_path(rel)
    truncated = False
    binary = False

    if is_untracked:
        text_b, bin_b, trunc_b, miss_b = _read_worktree(root, rel)
        binary = bin_b
        truncated = trunc_b
        if binary:
            return DiffPayload(
                path=rel,
                text_a="",
                text_b="",
                mode=mode,
                language_hint=lang,
                binary=True,
                missing_a=True,
                missing_b=miss_b,
            )
        return DiffPayload(
            path=rel,
            text_a="",
            text_b=text_b,
            mode=mode,
            language_hint=lang,
            truncated=truncated,
            missing_a=True,
            missing_b=miss_b,
        )

    if mode == "staged":
        text_a, bin_a, trunc_a, miss_a = _read_blob(root, f"HEAD:{rel}")
        text_b, bin_b, trunc_b, miss_b = _read_blob(root, f":{rel}")
    elif mode == "unstaged":
        text_a, bin_a, trunc_a, miss_a = _read_blob(root, f":{rel}")
        # If not in index, fall back to HEAD as left side.
        if miss_a:
            text_a, bin_a, trunc_a, miss_a = _read_blob(root, f"HEAD:{rel}")
        text_b, bin_b, trunc_b, miss_b = _read_worktree(root, rel)
    else:  # working
        text_a, bin_a, trunc_a, miss_a = _read_blob(root, f"HEAD:{rel}")
        text_b, bin_b, trunc_b, miss_b = _read_worktree(root, rel)

    binary = bin_a or bin_b
    truncated = trunc_a or trunc_b
    if binary:
        return DiffPayload(
            path=rel,
            text_a="",
            text_b="",
            mode=mode,
            language_hint=lang,
            binary=True,
            truncated=truncated,
            missing_a=miss_a,
            missing_b=miss_b,
        )

    return DiffPayload(
        path=rel,
        text_a=text_a,
        text_b=text_b,
        mode=mode,
        language_hint=lang,
        truncated=truncated,
        missing_a=miss_a,
        missing_b=miss_b,
    )
