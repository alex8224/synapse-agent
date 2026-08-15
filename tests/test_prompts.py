"""Tests for dynamic workspace and shell context in the system prompt."""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import SystemMessage

from synapse.content.prompts import build_system_prompt
from synapse.runtime.filesystem_tool_prompt_middleware import (
    build_filesystem_tool_prompt_middleware,
)


def test_build_system_prompt_injects_powershell_rules(tmp_path: Path):
    prompt = build_system_prompt(tmp_path, shell_executable="pwsh")

    assert "## Shell environment" in prompt
    assert "The `execute` tool uses `pwsh`." in prompt
    assert "Use PowerShell syntax, not Bash syntax." in prompt
    assert "Do not use Bash heredocs" in prompt
    assert "`python - <<'PY'`" in prompt
    assert "PowerShell here-string" in prompt


def test_default_prompt_describes_search_glob_as_include_filter(
    tmp_path: Path, monkeypatch
) -> None:
    from synapse.content import prompts as prompts_mod

    monkeypatch.setattr(prompts_mod, "user_config_dir", lambda: tmp_path / "missing-user")
    prompt = build_system_prompt(tmp_path)

    assert "`glob` only as an optional\ninclude filter" in prompt
    assert "does not express\ncache-directory exclusions" in prompt
    assert "exclude common build artifacts and caches via `glob`" not in prompt


def test_default_prompt_matches_active_filesystem_tools(tmp_path: Path, monkeypatch) -> None:
    from synapse.content import prompts as prompts_mod

    monkeypatch.setattr(prompts_mod, "user_config_dir", lambda: tmp_path / "missing-user")
    prompt = build_system_prompt(tmp_path)

    assert "## Active filesystem tools (authoritative)" in prompt
    assert "The model-facing `ls`, `glob`, and `grep` tools are hidden" in prompt
    assert "find_files(pattern, path, max_results, head_limit, offset)" in prompt
    assert "read_file(file_path, offset, limit)" in prompt
    assert "patch(file_path, patch)" in prompt
    assert "Use `ls /`" not in prompt


def test_filesystem_tool_prompt_middleware_appends_authoritative_guidance() -> None:
    class _Request:
        def __init__(self, system_message: SystemMessage) -> None:
            self.system_message = system_message

        def override(self, **changes):  # noqa: ANN003
            return _Request(changes.get("system_message", self.system_message))

    middleware = build_filesystem_tool_prompt_middleware()
    request = _Request(SystemMessage(content="generic guidance for ls, glob, and grep"))

    updated = middleware.wrap_model_call(request, lambda current: current)
    text = "\n".join(
        str(block.get("text", ""))
        for block in updated.system_message.content_blocks
        if isinstance(block, dict)
    )

    assert text.startswith("generic guidance")
    assert "## Active filesystem tools (authoritative)" in text
    assert text.rfind("never call them") > text.find("generic guidance")


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
    assert "## Active filesystem tools (authoritative)" in prompt
    assert "include-only path filter" in prompt
    assert "cannot express exclusions" in prompt
    assert "## Shell environment" in prompt
    assert "Do not use Bash heredocs" in prompt


def test_mandatory_path_rules_survive_external_prompt_override(tmp_path: Path):
    config_dir = tmp_path / ".synapse"
    config_dir.mkdir()
    (config_dir / "system_prompt.md").write_text("CUSTOM PROMPT", encoding="utf-8")

    prompt = build_system_prompt(tmp_path, shell_executable="pwsh")

    assert prompt.startswith("CUSTOM PROMPT")
    assert "## File-tool paths (mandatory)" in prompt
    assert "Never use Windows drive paths, host absolute paths" in prompt
    assert "convert the path to `/...`" in prompt