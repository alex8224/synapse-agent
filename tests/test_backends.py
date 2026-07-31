"""Tests for CodingLocalShellBackend encoding / shell options."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from synapse.config import Settings, load_settings
from synapse.runtime.backends import (
    DEFAULT_SHELL_EXECUTABLE,
    CodingLocalShellBackend,
    build_backend,
    resolve_shell_invocation,
)
from synapse.runtime.execute_capture import begin_execute_capture, end_execute_capture


def test_default_shell_platform_aware():
    """Default shell: pwsh on Windows, bash elsewhere."""
    if sys.platform == "win32":
        assert DEFAULT_SHELL_EXECUTABLE == "pwsh"
    else:
        assert DEFAULT_SHELL_EXECUTABLE == "bash"
    # config default is None => backend auto-detects via DEFAULT_SHELL_EXECUTABLE
    settings = Settings(_env_file=None)
    assert settings.shell_executable is None


def test_resolve_pwsh_uses_argument_list():
    """pwsh -> argument list; falls back to bash on non-Windows if not found."""
    args, shell, executable = resolve_shell_invocation("Get-Location", "pwsh")
    assert shell is False
    assert executable is None
    assert isinstance(args, list)
    exe = args[0].lower()
    if "pwsh" in exe or "powershell" in exe:
        # pwsh found: expect PowerShell-style invocation
        assert args[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
        assert args[4] == "Get-Location"
    else:
        # Non-Windows fallback to bash
        assert args[1:] == ["-lc", "Get-Location"]


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
    with patch("synapse.runtime.backends.shutil.which", return_value="/usr/bin/bash"):
        args, shell, executable = resolve_shell_invocation("ls -la", "bash")
    assert shell is False
    assert executable is None
    assert args == ["/usr/bin/bash", "-lc", "ls -la"]


def test_build_backend_default_shell_platform_aware(tmp_path: Path):
    """build_backend uses platform-aware default when shell_executable is None."""
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
    expected = "pwsh" if sys.platform == "win32" else "bash"
    assert Path(backend.shell_executable).name.lower() in {
        expected,
        f"{expected}.exe",
        "powershell",
        "powershell.exe",
        "sh",
    }
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

    pwsh_args = ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "echo hi"]
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = ("ok-中文", "")
    mock_proc.returncode = 0
    with (
        patch(
            "synapse.runtime.backends.resolve_shell_invocation",
            return_value=(pwsh_args, False, None),
        ),
        patch("synapse.runtime.backends.subprocess.Popen", return_value=mock_proc) as mock_popen,
    ):
        resp = backend.execute("echo hi")
        assert resp.exit_code == 0
        assert "ok-中文" in resp.output
        kwargs = mock_popen.call_args.kwargs
        assert kwargs["shell"] is False
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["args"][0] == "pwsh"
        assert "executable" not in kwargs


def test_execute_captures_full_output_before_response_truncation(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
        shell_executable="pwsh",
        max_output_bytes=10,
    )
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = ("0123456789ABCDEFGHIJ", "")
    mock_proc.returncode = 0
    capture, token = begin_execute_capture()
    try:
        with (
            patch(
                "synapse.runtime.backends.resolve_shell_invocation",
                return_value=(["pwsh", "-Command", "echo hi"], False, None),
            ),
            patch("synapse.runtime.backends.subprocess.Popen", return_value=mock_proc),
        ):
            response = backend.execute("echo hi")
    finally:
        end_execute_capture(token)

    assert response.truncated is True
    assert "Output truncated" in response.output
    assert capture.truncated is True
    assert capture.full_output == "0123456789ABCDEFGHIJ"
    assert capture.displayed_output == response.output


def test_native_grep_supports_regex_glob_and_virtual_paths(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("TODO: improve\nvalue = 42\n", encoding="utf-8")
    (src / "app.txt").write_text("TODO: ignored by include glob\n", encoding="utf-8")

    result = backend.grep(r"TODO|value\s*=\s*\d+", path="/src", glob="**/*.py")

    assert result.error is None
    assert result.matches == [
        {"path": "/src/app.py", "line": 1, "text": "TODO: improve"},
        {"path": "/src/app.py", "line": 2, "text": "value = 42"},
    ]


def test_native_glob_respects_gitignore_and_deny_paths(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "visible.py").write_text("pass\n", encoding="utf-8")
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    (ignored / "hidden.py").write_text("pass\n", encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir()
    (private / "hidden.py").write_text("pass\n", encoding="utf-8")
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
        deny_paths=["private/"],
    )

    result = backend.glob("**/*.py")

    assert result.error is None
    assert [item["path"] for item in result.matches] == ["/src/visible.py"]


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
