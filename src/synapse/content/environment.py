"""Build-time environment, git, and date context sections.

These sections are ``dynamic``: they describe the machine and repository the
agent was started in rather than the agent's own instructions, so they must stay
outside the cacheable stable prefix.

Every git lookup is read-only, bounded, and fail-open: a missing repository, a
missing ``git`` binary, or a timeout simply drops the section instead of failing
agent construction.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from synapse.content.prompt_sections import DYNAMIC, SYSTEM_TARGET, PromptSection

#: Bound every git call so a slow repository cannot stall agent construction.
GIT_TIMEOUT_SECONDS = 2
#: Cap the injected output; the model can always re-run git itself.
MAX_STATUS_LINES = 50
MAX_COMMITS = 5

GitRunner = Callable[[Sequence[str], Path], "str | None"]


@dataclass(frozen=True)
class EnvironmentInfo:
    """Machine and model facts for the current agent build."""

    working_directory: str
    is_git_repository: bool
    platform: str
    shell: str
    os_version: str
    model: str | None = None


@dataclass(frozen=True)
class GitContext:
    """Snapshot of the repository state at the start of the conversation."""

    branch: str | None = None
    main_branch: str | None = None
    user: str | None = None
    status_lines: tuple[str, ...] = ()
    recent_commits: tuple[str, ...] = ()


def _run_git(args: Sequence[str], root: Path) -> str | None:
    """Run a read-only git command; return stripped stdout, or None on failure."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _bounded_lines(text: str | None, limit: int) -> list[str]:
    """Return at most ``limit`` non-empty lines of ``text``."""
    if not text:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[:limit]


def collect_git_context(
    root: Path | str,
    *,
    run_git: GitRunner | None = None,
) -> GitContext | None:
    """Collect a bounded git snapshot, or None when ``root`` is not a repository."""
    runner = run_git or _run_git
    directory = Path(root)
    if runner(["rev-parse", "--is-inside-work-tree"], directory) != "true":
        return None
    return GitContext(
        branch=runner(["rev-parse", "--abbrev-ref", "HEAD"], directory) or None,
        main_branch=runner(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], directory)
        or None,
        user=runner(["config", "user.name"], directory) or None,
        status_lines=tuple(
            _bounded_lines(
                runner(["status", "--porcelain=v1", "--branch"], directory), MAX_STATUS_LINES
            )
        ),
        recent_commits=tuple(
            _bounded_lines(
                runner(["log", "--oneline", "-n", str(MAX_COMMITS)], directory), MAX_COMMITS
            )
        ),
    )


def collect_environment_info(
    root: Path | str,
    *,
    shell: str | None,
    model_spec: Any | None = None,
    is_git_repository: bool = False,
) -> EnvironmentInfo:
    """Describe the machine the agent was started on."""
    return EnvironmentInfo(
        working_directory=str(Path(root).resolve()),
        is_git_repository=is_git_repository,
        platform=sys.platform,
        shell=shell or ("pwsh" if sys.platform == "win32" else "bash"),
        os_version=platform.platform(),
        model=str(model_spec) if model_spec else None,
    )


def render_environment_section(info: EnvironmentInfo) -> str:
    """Render the ``## Environment`` block."""
    lines = [
        "## Environment",
        "You have been invoked in the following environment:",
        f"- Primary working directory: {info.working_directory}",
        f"- Is a git repository: {'yes' if info.is_git_repository else 'no'}",
        f"- Platform: {info.platform}",
        f"- Shell: {info.shell}",
        f"- OS Version: {info.os_version}",
    ]
    if info.model:
        lines.append(f"- You are powered by the model named {info.model}.")
    return "\n".join(lines)


def render_git_section(git: GitContext) -> str:
    """Render the ``## Git context`` block."""
    lines = [
        "## Git context",
        "This is the git status at the start of the conversation. It is a snapshot in "
        "time and will not update during the conversation.",
    ]
    if git.branch:
        lines.append(f"- Current branch: {git.branch}")
    if git.main_branch:
        lines.append(f"- Main branch (you will usually use this for PRs): {git.main_branch}")
    if git.user:
        lines.append(f"- Git user: {git.user}")
    if git.status_lines:
        lines.extend(["", "Status:"])
        lines.extend(git.status_lines)
    if git.recent_commits:
        lines.extend(["", "Recent commits:"])
        lines.extend(git.recent_commits)
    return "\n".join(lines)


def render_date_section(today: date) -> str:
    """Render the ``## Current date`` block."""
    return f"## Current date\nToday's date is {today.isoformat()}."


def build_context_sections(
    root: Path | str,
    *,
    shell: str | None = None,
    model_spec: Any | None = None,
    run_git: GitRunner | None = None,
    today: date | None = None,
) -> list[PromptSection]:
    """Build the dynamic environment/git/date sections appended after the stable ones."""
    git = collect_git_context(root, run_git=run_git)
    info = collect_environment_info(
        root,
        shell=shell,
        model_spec=model_spec,
        is_git_repository=git is not None,
    )
    sections = [
        PromptSection(
            name="Environment",
            source="env_info",
            content=render_environment_section(info),
            cache_hint=DYNAMIC,
            injection_target=SYSTEM_TARGET,
        )
    ]
    if git is not None:
        sections.append(
            PromptSection(
                name="Git Context",
                source="system_context",
                content=render_git_section(git),
                cache_hint=DYNAMIC,
                injection_target=SYSTEM_TARGET,
            )
        )
    sections.append(
        PromptSection(
            name="Current Date",
            source="current_date",
            content=render_date_section(today or date.today()),
            cache_hint=DYNAMIC,
            injection_target=SYSTEM_TARGET,
        )
    )
    return sections
