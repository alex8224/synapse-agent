"""Project-oriented helper tools."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from langchain_core.tools import tool


@tool
def run_tests(
    workspace: str = ".",
    target: str = "",
    extra_args: str = "",
) -> str:
    """运行项目 pytest 测试（可指定子集）。

    优先使用 `uv run pytest`；若无 `uv` 则回退到 `python -m pytest`。

    Args:
        workspace: 项目根目录。
        target: 可选，传给 pytest 的路径或节点 id。
        extra_args: 额外 CLI 参数字符串，例如 `-q -k smoke`。
    """
    root = Path(workspace).expanduser().resolve()
    if not root.exists():
        return f"ERROR: workspace does not exist: {root}"

    args: list[str]
    if _has_command("uv"):
        args = ["uv", "run", "pytest"]
    else:
        args = ["python", "-m", "pytest"]

    if target.strip():
        args.append(target.strip())
    if extra_args.strip():
        args.extend(extra_args.split())

    try:
        completed = subprocess.run(
            args,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
            env=os.environ.copy(),
        )
    except FileNotFoundError as exc:
        return f"ERROR: failed to start tests: {exc}"
    except subprocess.TimeoutExpired:
        return "ERROR: pytest timed out after 300s"

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    body = "\n".join(part for part in (stdout, stderr) if part)
    # Keep tool output bounded for model context.
    if len(body) > 20_000:
        body = body[:20_000] + "\n...[truncated]..."
    return f"cmd: {' '.join(args)}\nexit={completed.returncode}\n{body}".strip()


def _has_command(name: str) -> bool:
    from shutil import which

    return which(name) is not None
