"""Tests for the build-time environment, git, and date context sections."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from synapse.content.environment import (
    MAX_COMMITS,
    MAX_STATUS_LINES,
    _run_git,
    build_context_sections,
    collect_git_context,
    render_date_section,
)
from synapse.content.prompt_sections import render_system_prompt, stable_prefix
from synapse.content.prompts import build_system_prompt_sections

TODAY = date(2026, 9, 15)


def _responses(
    overrides: dict[tuple[str, ...], str] | None = None,
) -> dict[tuple[str, ...], str]:
    base = {
        ("rev-parse", "--is-inside-work-tree"): "true",
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/main",
        ("config", "user.name"): "alexz",
        ("status", "--porcelain=v1", "--branch"): "## main\n M a.py",
        ("log", "--oneline", "-n", str(MAX_COMMITS)): "abc1234 first",
    }
    base.update(overrides or {})
    return base


def _fake_git(responses: dict[tuple[str, ...], str]):
    def runner(args, _root):  # noqa: ANN001, ANN202
        return responses.get(tuple(args))

    return runner


def test_non_git_directory_drops_the_git_section(tmp_path: Path) -> None:
    sections = build_context_sections(tmp_path, shell="pwsh", today=TODAY)

    assert [section.name for section in sections] == ["Environment", "Current Date"]
    assert "- Is a git repository: no" in sections[0].content


def test_git_section_is_added_for_a_repository(tmp_path: Path) -> None:
    sections = build_context_sections(
        tmp_path, shell="pwsh", run_git=_fake_git(_responses()), today=TODAY
    )
    git_section = next(section for section in sections if section.name == "Git Context")

    assert [section.name for section in sections] == [
        "Environment",
        "Git Context",
        "Current Date",
    ]
    assert "- Current branch: main" in git_section.content
    assert "- Main branch (you will usually use this for PRs): origin/main" in git_section.content
    assert "- Git user: alexz" in git_section.content
    assert " M a.py" in git_section.content
    assert "abc1234 first" in git_section.content
    assert "- Is a git repository: yes" in sections[0].content


def test_context_sections_are_dynamic(tmp_path: Path) -> None:
    sections = build_context_sections(tmp_path, shell="pwsh", today=TODAY)

    assert all(section.cache_hint == "dynamic" for section in sections)
    assert all(section.injection_target == "system" for section in sections)


def test_environment_reports_shell_and_model(tmp_path: Path) -> None:
    sections = build_context_sections(
        tmp_path, shell="pwsh", model_spec="openai:gpt-4.1", today=TODAY
    )

    assert "- Shell: pwsh" in sections[0].content
    assert "- You are powered by the model named openai:gpt-4.1." in sections[0].content


def test_git_output_is_bounded(tmp_path: Path) -> None:
    status = "## main\n" + "\n".join(
        f" M f{index}.py" for index in range(MAX_STATUS_LINES + 20)
    )
    commits = "\n".join(f"c{index:04d} msg" for index in range(MAX_COMMITS + 20))

    git = collect_git_context(
        tmp_path,
        run_git=_fake_git(
            _responses(
                {
                    ("status", "--porcelain=v1", "--branch"): status,
                    ("log", "--oneline", "-n", str(MAX_COMMITS)): commits,
                }
            )
        ),
    )

    assert git is not None
    assert len(git.status_lines) == MAX_STATUS_LINES
    assert len(git.recent_commits) == MAX_COMMITS


def test_missing_git_binary_drops_the_section(tmp_path: Path) -> None:
    sections = build_context_sections(
        tmp_path, shell="pwsh", run_git=lambda _args, _root: None, today=TODAY
    )

    assert [section.name for section in sections] == ["Environment", "Current Date"]


def test_run_git_returns_none_for_a_missing_directory(tmp_path: Path) -> None:
    assert _run_git(["status"], tmp_path / "does-not-exist") is None


def test_date_section_uses_iso_format() -> None:
    assert render_date_section(TODAY) == "## Current date\nToday's date is 2026-09-15."


def test_stable_prefix_stops_before_the_context_sections(tmp_path: Path) -> None:
    stable = build_system_prompt_sections(tmp_path, shell_executable="pwsh")
    sections = [*stable, *build_context_sections(tmp_path, shell="pwsh", today=TODAY)]

    prefix = stable_prefix(sections)
    rendered = render_system_prompt(sections)

    assert rendered.startswith(prefix)
    assert prefix.endswith("\n\n")
    assert "## Environment" not in prefix
    assert rendered[len(prefix) :].startswith("## Environment")
