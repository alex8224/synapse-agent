"""Tests for dynamic workspace and shell context in the system prompt."""

from __future__ import annotations

from pathlib import Path

from synapse.content.prompts import build_system_prompt


def test_build_system_prompt_injects_powershell_rules(tmp_path: Path):
    prompt = build_system_prompt(tmp_path, shell_executable="pwsh")

    assert "## Shell environment" in prompt
    assert "The `execute` tool uses `pwsh`." in prompt
    assert "Use PowerShell syntax, not Bash syntax." in prompt
    assert "Do not use Bash heredocs" in prompt
    assert "`python - <<'PY'`" in prompt
    assert "PowerShell here-string" in prompt


def test_build_system_prompt_uses_effective_shell_path(tmp_path: Path):
    shell = r"C:\Program Files\PowerShell\7\pwsh.exe"
    prompt = build_system_prompt(tmp_path, shell_executable=shell)

    assert f"The `execute` tool uses `{shell}`." in prompt
    assert "Use PowerShell syntax, not Bash syntax." in prompt


def test_build_system_prompt_injects_bash_rules(tmp_path: Path):
    prompt = build_system_prompt(tmp_path, shell_executable="bash")

    assert "The `execute` tool uses `bash`." in prompt
    assert "Use Bash/POSIX shell syntax" in prompt
    assert "Do not use Bash heredocs" not in prompt


def test_shell_context_survives_external_prompt_override(tmp_path: Path):
    config_dir = tmp_path / ".synapse"
    config_dir.mkdir()
    (config_dir / "system_prompt.md").write_text("CUSTOM PROMPT", encoding="utf-8")

    prompt = build_system_prompt(tmp_path, shell_executable="pwsh")

    assert prompt.startswith("CUSTOM PROMPT")
    assert "## Current workspace" in prompt
    assert "## Shell environment" in prompt
    assert "Do not use Bash heredocs" in prompt
