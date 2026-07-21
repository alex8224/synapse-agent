"""Git inspection tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.tools import tool


def _run_git(args: list[str], cwd: str | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except FileNotFoundError:
        return "ERROR: git executable not found on PATH"
    except subprocess.TimeoutExpired:
        return "ERROR: git command timed out"

    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    if completed.returncode != 0:
        return f"exit={completed.returncode}\n{out}\n{err}".strip()
    return out or "(empty)"


@tool
def git_status(workspace: str = ".") -> str:
    """查看 git 工作区状态（短格式 + 分支）。

    Args:
        workspace: 仓库根目录。默认为当前工作区。
    """
    root = str(Path(workspace).expanduser().resolve())
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    status = _run_git(["status", "--short", "--branch"], cwd=root)
    return f"branch: {branch}\n{status}"


@tool
def git_diff(workspace: str = ".", staged: bool = False) -> str:
    """查看 git diff（工作区变更）。

    Args:
        workspace: 仓库根目录。
        staged: 为 true 时仅查看已暂存变更。
    """
    root = str(Path(workspace).expanduser().resolve())
    args = ["diff", "--stat", "--patch"]
    if staged:
        args.append("--cached")
    return _run_git(args, cwd=root)
