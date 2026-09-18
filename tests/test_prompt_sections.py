"""Tests for the prompt section registry and its rendering contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse.content import prompts as prompts_mod
from synapse.content.prompt_sections import (
    DYNAMIC,
    META_USER_TARGET,
    STABLE,
    SYSTEM_TARGET,
    PromptSection,
    render_system_prompt,
    section_stats,
    stable_prefix,
)

EXPECTED_SECTIONS = [
    ("Coding Body", "body"),
    ("Mandatory Rules", "mandatory_rules"),
    ("Workspace", "workspace"),
    ("Filesystem Tools", "filesystem_tools"),
    ("Shell", "shell"),
]


@pytest.fixture(autouse=True)
def _no_user_prompt(monkeypatch, tmp_path):
    """Keep any real user-level system prompt file out of the way."""
    monkeypatch.setattr(prompts_mod, "user_config_dir", lambda: tmp_path / "missing-user")


def test_sections_are_named_and_ordered(tmp_path: Path) -> None:
    sections = prompts_mod.build_system_prompt_sections(tmp_path, shell_executable="pwsh")

    assert [(section.name, section.source) for section in sections] == EXPECTED_SECTIONS
    assert all(section.cache_hint == STABLE for section in sections)
    assert all(section.injection_target == SYSTEM_TARGET for section in sections)


def test_render_reproduces_historical_layout(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    rendered = prompts_mod.build_system_prompt(tmp_path, shell_executable="pwsh")

    expected = (
        f"{prompts_mod.DEFAULT_CODING_SYSTEM_PROMPT.strip()}\n\n"
        f"{prompts_mod.MANDATORY_CODING_RULES}\n"
        f"## Current workspace\n"
        f"- Host root (shell/git only): `{root}`\n"
        f"- File-tool virtual root: `/` maps to the host root above\n"
        f"- Mapping example: `{root / 'README.md'}` -> `/README.md`\n"
        f"- Shell commands run on the host, inside the workspace root.\n\n"
        f"{prompts_mod.filesystem_tool_prompt(None)}\n"
        f"{prompts_mod._shell_prompt('pwsh')}"
    )

    assert rendered == expected


def test_build_system_prompt_delegates_to_sections(tmp_path: Path) -> None:
    sections = prompts_mod.build_system_prompt_sections(tmp_path, shell_executable="pwsh")

    assert prompts_mod.build_system_prompt(tmp_path, shell_executable="pwsh") == (
        render_system_prompt(sections)
    )


@pytest.mark.parametrize("shell", ["pwsh", "bash", "cmd.exe", "/opt/weird-shell"])
def test_stable_prefix_is_a_literal_prefix(tmp_path: Path, shell: str) -> None:
    sections = prompts_mod.build_system_prompt_sections(tmp_path, shell_executable=shell)

    rendered = render_system_prompt(sections)
    prefix = stable_prefix(sections)

    assert prefix
    assert rendered.startswith(prefix)
    assert prefix.endswith("\n")


def test_stable_prefix_stops_at_first_dynamic_section() -> None:
    sections = [
        PromptSection("A", "a", "alpha"),
        PromptSection("B", "b", "beta", cache_hint=DYNAMIC),
        PromptSection("C", "c", "gamma"),
    ]

    assert stable_prefix(sections) == "alpha\n\n"
    assert render_system_prompt(sections) == "alpha\n\nbeta\n\ngamma\n"


def test_stable_prefix_stops_at_meta_user_section() -> None:
    sections = [
        PromptSection("A", "a", "alpha"),
        PromptSection("S", "skills", "skills", injection_target=META_USER_TARGET),
    ]

    assert stable_prefix(sections) == "alpha\n\n"


def test_render_skips_empty_sections() -> None:
    sections = [
        PromptSection("A", "a", "alpha"),
        PromptSection("Empty", "empty", "   \n  "),
        PromptSection("C", "c", "gamma"),
    ]

    assert render_system_prompt(sections) == "alpha\n\ngamma\n"
    assert stable_prefix(sections) == "alpha\n\ngamma\n"


def test_section_content_is_normalized() -> None:
    section = PromptSection("A", "a", "\n\nalpha\n\n")

    assert section.content == "alpha"
    assert section.chars == 5


def test_invalid_hints_are_rejected() -> None:
    with pytest.raises(ValueError):
        PromptSection("A", "a", "alpha", cache_hint="sometimes")
    with pytest.raises(ValueError):
        PromptSection("A", "a", "alpha", injection_target="tool")


def test_section_stats_report_size_and_preview() -> None:
    stats = section_stats([PromptSection("A", "a", "alpha")])

    assert stats == [
        {
            "name": "A",
            "source": "a",
            "injection_target": SYSTEM_TARGET,
            "cache_hint": STABLE,
            "chars": 5,
            "preview": "alpha",
        }
    ]


def test_excluded_tools_shrink_the_filesystem_section(tmp_path: Path) -> None:
    full = prompts_mod.build_system_prompt_sections(tmp_path, shell_executable="pwsh")
    reduced = prompts_mod.build_system_prompt_sections(
        tmp_path,
        shell_executable="pwsh",
        excluded_tools=["find_files", "search_files", "read_file", "patch"],
    )

    full_fs = next(section for section in full if section.source == "filesystem_tools")
    reduced_fs = next(section for section in reduced if section.source == "filesystem_tools")

    assert "find_files(pattern" in full_fs.content
    assert "find_files(pattern" not in reduced_fs.content
    assert reduced_fs.chars < full_fs.chars
