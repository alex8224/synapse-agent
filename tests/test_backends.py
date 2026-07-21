"""Tests for CodingLocalShellBackend encoding / shell options."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from synapse.backends import (
    DEFAULT_SHELL_EXECUTABLE,
    CodingLocalShellBackend,
    build_backend,
    resolve_shell_invocation,
)
from synapse.config import Settings, load_settings


def test_default_shell_is_pwsh():
    assert DEFAULT_SHELL_EXECUTABLE == "pwsh"
    settings = Settings(_env_file=None)
    assert settings.shell_executable == "pwsh"


def test_resolve_pwsh_uses_argument_list():
    args, shell, executable = resolve_shell_invocation("Get-Location", "pwsh")
    assert shell is False
    assert executable is None
    assert isinstance(args, list)
    exe = args[0].lower()
    assert exe.endswith("pwsh.exe") or exe.endswith("pwsh") or "powershell" in exe
    assert args[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert args[4] == "Get-Location"


def test_resolve_cmd_uses_shell_true():
    args, shell, executable = resolve_shell_invocation("echo hi", "cmd")
    assert args == "echo hi"
    assert shell is True


def test_resolve_system_uses_shell_true():
    args, shell, executable = resolve_shell_invocation("echo hi", "system")
    assert args == "echo hi"
    assert shell is True
    assert executable is None


def test_resolve_bash_uses_argument_list():
    with patch("synapse.backends.shutil.which", return_value="/usr/bin/bash"):
        args, shell, executable = resolve_shell_invocation("ls -la", "bash")
    assert shell is False
    assert executable is None
    assert args == ["/usr/bin/bash", "-lc", "ls -la"]


def test_build_backend_defaults_to_pwsh(tmp_path: Path):
    settings = load_settings(
        workspace=tmp_path,
        inherit_env=True,
        virtual_mode=True,
        shell_encoding="utf-8",
        shell_encoding_errors="replace",
        checkpoint_backend="memory",
    )
    backend = build_backend(settings)
    assert isinstance(backend, CodingLocalShellBackend)
    assert backend._shell_executable == "pwsh"
    assert backend._shell_encoding == "utf-8"
    assert backend._env.get("PYTHONUTF8") == "1"


def test_build_backend_shell_executable_override(tmp_path: Path):
    settings = load_settings(
        workspace=tmp_path,
        shell_executable="  cmd  ",
        checkpoint_backend="memory",
    )
    backend = build_backend(settings)
    assert backend._shell_executable == "cmd"


def test_execute_pwsh_invocation_kwargs(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
        shell_executable="pwsh",
        shell_encoding="utf-8",
        shell_encoding_errors="replace",
    )

    completed = MagicMock()
    completed.stdout = "ok-中文"
    completed.stderr = ""
    completed.returncode = 0

    pwsh_args = ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "echo hi"]
    with (
        patch(
            "synapse.backends.resolve_shell_invocation",
            return_value=(pwsh_args, False, None),
        ),
        patch("synapse.backends.subprocess.run", return_value=completed) as mock_run,
    ):
        resp = backend.execute("echo hi")
        assert resp.exit_code == 0
        assert "ok-中文" in resp.output
        kwargs = mock_run.call_args.kwargs
        assert kwargs["shell"] is False
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["args"][0] == "pwsh"
        assert "executable" not in kwargs


def test_ripgrep_search_uses_utf8_encoding(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
        shell_encoding="utf-8",
        shell_encoding_errors="replace",
    )
    (tmp_path / "a.py").write_text("hello_token = 1\n", encoding="utf-8")

    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = (
        '{"type":"match","data":{"path":{"text":"a.py"},'
        '"line_number":1,"lines":{"text":"hello_token = 1\\n"}}}\n'
    )
    completed.stderr = ""

    with (
        patch("deepagents.backends.filesystem._resolve_ripgrep_path", return_value="rg"),
        patch("synapse.backends.subprocess.run", return_value=completed) as mock_run,
    ):
        results = backend._ripgrep_search("hello_token", tmp_path, None)
        kwargs = mock_run.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["text"] is True
    assert results is not None
    assert any("hello_token" in line for items in results.values() for _, line in items)


def test_ripgrep_search_handles_none_stdout(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
    )
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = None
    completed.stderr = None

    with (
        patch("deepagents.backends.filesystem._resolve_ripgrep_path", return_value="rg"),
        patch("synapse.backends.subprocess.run", return_value=completed),
    ):
        results = backend._ripgrep_search("x", tmp_path, None)
    assert results == {}


def test_execute_survives_non_utf8_bytes_via_replace(tmp_path: Path):
    """Real subprocess: UTF-8 decode with replace must not raise UnicodeDecodeError."""
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=True,
        shell_executable="pwsh",
        shell_encoding="utf-8",
        shell_encoding_errors="replace",
    )
    # Emit a single invalid-as-utf8 byte 0xaa (same family as the Windows GBK crash).
    code = "import sys; sys.stdout.buffer.write(b'hello\\xaa world\\n')"
    cmd = f"& '{sys.executable}' -c \"{code}\""
    resp = backend.execute(cmd)
    assert resp.exit_code == 0
    assert "hello" in resp.output
    assert "world" in resp.output
    assert not resp.output.startswith(
        "Error executing command (UnicodeDecodeError)"
    )
