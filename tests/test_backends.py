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
from synapse.tools.filesystem_search import build_filesystem_search_tools


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
        # pwsh found: expect PowerShell-style invocation with UTF-8 bootstrap
        assert args[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
        assert "OutputEncoding=[System.Text.Encoding]::UTF8" in args[4]
        assert args[4].endswith("Get-Location")
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


def test_native_grep_normalizes_leading_slash_in_include_glob(tmp_path: Path):
    """include_glob must be relative (no leading slash) so the native globset
    matches relative paths; there is no per-file retry fallback anymore."""
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("TODO: recovered\n", encoding="utf-8")
    payload = {
        "matches": [{"path": "app.py", "line": 1, "text": "TODO: recovered"}],
        "total_matches": 1,
        "truncated": False,
    }

    with patch("synapse_core_tool.grep", return_value=payload) as grep:
        result = backend.grep("TODO", path="/src", glob="/**/*.py", max_results=10)

    assert result.error is None
    assert result.matches == [{"path": "/src/app.py", "line": 1, "text": "TODO: recovered"}]
    grep.assert_called_once()
    assert grep.call_args.kwargs["include_glob"] == "**/*.py"


def test_search_files_tool_applies_glob_as_include_filter(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("TODO: include me\n", encoding="utf-8")
    (src / "app.txt").write_text("TODO: exclude me\n", encoding="utf-8")
    search_files = {
        tool.name: tool for tool in build_filesystem_search_tools(backend)
    }["search_files"]

    result = search_files.invoke(
        {
            "pattern": "TODO",
            "path": "/src",
            "glob": "**/*.py",
            "output_mode": "content",
            "max_results": 10,
            "head_limit": 10,
        }
    )

    assert result == "/src/app.py:1: TODO: include me"


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


def test_native_read_returns_requested_raw_line_window(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
    )
    (tmp_path / "sample.txt").write_bytes(b"first\r\nsecond\r\nthird\r\n")

    result = backend.read("/sample.txt", offset=1, limit=1)

    assert result.error is None
    assert result.file_data == {"content": "second\r\n", "encoding": "utf-8"}


def test_native_edit_preserves_utf16_encoding(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
    )
    path = tmp_path / "utf16.txt"
    path.write_bytes("before\r\nafter\r\n".encode("utf-16"))

    result = backend.edit("/utf16.txt", "before\nafter", "updated\ntext")

    assert result.error is None
    assert result.occurrences == 1
    assert path.read_bytes().startswith(b"\xff\xfe")
    assert path.read_bytes().decode("utf-16") == "updated\r\ntext\r\n"


def test_native_patch_preserves_crlf_and_trailing_newline(tmp_path: Path):
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
    )
    path = tmp_path / "patched.txt"
    path.write_bytes(b"old\r\nkeep\r\n")

    result = backend.patch(
        "/patched.txt",
        "@@ -1,2 +1,2 @@\n-old\n+new\n keep\n",
    )

    assert result == {"path": "/patched.txt", "hunks_applied": 1, "error": None}
    assert path.read_bytes() == b"new\r\nkeep\r\n"


def test_native_patch_respects_deny_paths(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir()
    path = private / "blocked.txt"
    path.write_text("old\n", encoding="utf-8")
    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
        deny_paths=["private/"],
    )

    result = backend.patch("/private/blocked.txt", "@@ -1 +1 @@\n-old\n+new\n")

    assert "denied by filesystem permissions" in str(result["error"])
    assert path.read_text(encoding="utf-8") == "old\n"


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