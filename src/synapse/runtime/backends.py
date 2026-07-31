"""Backend factory for the coding agent.

No remote sandbox. Local host filesystem + shell only.

``LocalShellBackend.execute`` uses ``text=True`` without an explicit encoding, which
on Chinese Windows often decodes with GBK and crashes on UTF-8 tool output
(``UnicodeDecodeError: 'gbk' codec can't decode ...``).

This module subclasses ``LocalShellBackend`` and reimplements ``execute`` with:
- configurable output encoding (default UTF-8 + replace)
- default shell ``pwsh`` (PowerShell 7+), with cmd/bash/system overrides
- UTF-8-friendly env defaults for child Python processes
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from synapse.runtime.execute_capture import capture_execute_output
from synapse.runtime.tool_ignore import ToolIgnoreMatcher, relative_to_root
from synapse.settings import Settings

# Default shell for this project: pwsh on Windows, bash elsewhere.
DEFAULT_SHELL_EXECUTABLE = "pwsh" if sys.platform == "win32" else "bash"


def _basename(path_or_name: str) -> str:
    return Path(path_or_name).name.lower()


def resolve_shell_invocation(
    command: str,
    shell_executable: str | None,
) -> tuple[str | list[str], bool, str | None]:
    """Map configured shell name/path to subprocess ``(args, shell, executable)``.

    Notes
    -----
    On Windows, ``shell=True`` + ``executable=pwsh`` is unreliable because Python
    still builds a ``cmd /c ...`` command line. Known shells are therefore invoked
    as argument lists with ``shell=False``.
    """
    raw = (shell_executable or DEFAULT_SHELL_EXECUTABLE).strip()
    key = raw.lower()
    base = _basename(raw)

    # System / cmd: classic shell=True (Windows COMSPEC / Unix /bin/sh)
    if key in {"", "system", "default", "cmd", "cmd.exe"} or base in {"cmd", "cmd.exe"}:
        if base in {"cmd", "cmd.exe"} and key not in {"system", "default", ""}:
            exe = raw if Path(raw).is_file() else shutil.which(raw) or shutil.which("cmd")
            return command, True, exe
        return command, True, None

    # PowerShell 7+ / Windows PowerShell
    if base in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}:
        exe = raw if Path(raw).is_file() else shutil.which(raw)
        if exe is None and base.startswith("pwsh"):
            # Prefer pwsh; fall back to Windows PowerShell if Core is missing.
            exe = shutil.which("pwsh") or shutil.which("powershell")
        if exe is None:
            if sys.platform != "win32":
                # Non-Windows: pwsh not available, fall back to bash.
                exe = shutil.which("bash") or shutil.which("sh") or "bash"
                return [exe, "-lc", command], False, None
            exe = raw
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command], False, None

    # bash / sh
    if base in {"bash", "bash.exe", "sh", "sh.exe"}:
        exe = raw if Path(raw).is_file() else shutil.which(raw) or raw
        return [exe, "-lc", command], False, None

    # Unknown absolute/custom binary: treat as shell program with shell=True.
    return command, True, raw


def _kill_process_tree(proc: subprocess.Popen[Any] | None) -> None:
    """Kill a process and all its children (best-effort on Windows).

    On Windows, ``proc.kill()`` only terminates the direct process; child
    processes survive and hold pipe handles open, which causes
    ``communicate()`` to hang.  Use ``taskkill /T`` to terminate the whole
    process tree, then drain pipes with a short timeout.
    """
    if proc is None:
        return
    try:
        pid = proc.pid
    except Exception:  # noqa: BLE001
        return
    if pid is None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    # Drain pipes with a short timeout so we don't hang forever if child
    # processes still hold them open.
    try:
        proc.communicate(timeout=3)
    except Exception:  # noqa: BLE001
        pass


def _timeout_msg(effective_timeout: int, per_call_timeout: int | None) -> str:
    if per_call_timeout is not None:
        return (
            f"Error: Command timed out after {effective_timeout} seconds "
            "(custom timeout). The command may be stuck or require more time."
        )
    return (
        f"Error: Command timed out after {effective_timeout} seconds. "
        "For long-running commands, re-run using the timeout parameter."
    )


class CodingLocalShellBackend(LocalShellBackend):
    """Local shell backend with explicit encoding and configurable shell."""

    def __init__(
        self,
        *args: Any,
        shell_executable: str | None = DEFAULT_SHELL_EXECUTABLE,
        shell_encoding: str = "utf-8",
        shell_encoding_errors: str = "replace",
        deny_paths: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._shell_executable = (shell_executable or DEFAULT_SHELL_EXECUTABLE).strip()
        self._effective_shell_executable = self._resolve_effective_shell_executable()
        self._shell_encoding = shell_encoding or "utf-8"
        self._shell_encoding_errors = shell_encoding_errors or "replace"

        # Prefer UTF-8 for Python children; does not fix every native CLI, but helps.
        self._env.setdefault("PYTHONUTF8", "1")
        self._env.setdefault("PYTHONIOENCODING", "utf-8")

        if deny_paths:
            matcher = ToolIgnoreMatcher.from_patterns(deny_paths)
            self._tool_ignore_dedicated = True
        else:
            try:
                root = Path(self.cwd).resolve()
                matcher = ToolIgnoreMatcher.from_workspace(root)
            except Exception:  # noqa: BLE001
                matcher = ToolIgnoreMatcher([])
            self._tool_ignore_dedicated = False
        self._tool_ignore: ToolIgnoreMatcher = matcher

    def _resolve_effective_shell_executable(self) -> str:
        """Resolve the shell program selected at startup, including platform fallbacks."""
        configured = self._shell_executable.strip()
        if configured.lower() in {"", "system", "default"}:
            return os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"
        args, _, executable = resolve_shell_invocation("", configured)
        if isinstance(args, list) and args:
            return str(args[0])
        return executable or configured

    @property
    def shell_executable(self) -> str:
        """Return the effective shell program used by ``execute``."""
        return self._effective_shell_executable

    # --- native filesystem tool overrides ---

    def _resolve_native_search_path(self, path: str | None) -> Path | None:
        """Resolve an agent path before passing it to the native filesystem engine.

        The Rust extension only receives a verified host path. Results are converted
        back to virtual paths below, so it cannot bypass the backend path contract.
        """
        try:
            resolved = self._resolve_path(path or ".")
        except (ValueError, OSError, RuntimeError):
            return None
        try:
            return resolved if resolved.exists() else None
        except OSError:
            return None

    def _native_result_path(self, base_path: Path, relative_path: str) -> str | None:
        candidate = base_path / relative_path
        try:
            resolved = candidate.resolve()
            resolved.relative_to(base_path.resolve())
            resolved.relative_to(Path(self.cwd).resolve())
        except (ValueError, OSError):
            return None
        if self._tool_ignore.is_ignored(relative_to_root(resolved, Path(self.cwd).resolve())):
            return None
        if self.virtual_mode:
            try:
                return self._to_virtual_path(resolved)
            except (ValueError, OSError, RuntimeError):
                return None
        return str(resolved)

    def _grep_glob_candidates(
        self,
        search_core: Any,
        base_path: Path,
        pattern: str,
        glob: str,
        max_results: int,
        context_lines: int,
        case_insensitive: bool,
    ) -> list[dict[str, Any]]:
        """Retry an empty include-glob search against core-enumerated candidate files."""
        candidates = search_core.glob(str(base_path), glob.lstrip("/"))["matches"]
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.get("is_dir") or len(matches) >= max_results:
                continue
            relative_path = str(candidate["path"])
            file_path = base_path / relative_path
            remaining = max_results - len(matches)
            payload = search_core.grep(
                str(file_path),
                pattern,
                max_results=remaining,
                context_lines=context_lines,
                case_insensitive=case_insensitive,
            )
            for item in payload["matches"]:
                matches.append(
                    {"path": relative_path, "line": item["line"], "text": item["text"]}
                )
        return matches

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        max_results: int = 1000,
        context_lines: int = 0,
        case_insensitive: bool = False,
    ) -> Any:
        """Search with the required native Rust engine using regular expressions."""
        import synapse_search_core
        from deepagents.backends.filesystem import GrepResult

        base_path = self._resolve_native_search_path(path)
        if base_path is None:
            return GrepResult(matches=[])
        try:
            payload = synapse_search_core.grep(
                str(base_path), pattern, include_glob=glob, max_results=max_results,
                context_lines=context_lines, case_insensitive=case_insensitive,
            )
            raw_matches = list(payload["matches"])
            if glob and not raw_matches and base_path.is_dir():
                raw_matches = self._grep_glob_candidates(
                    synapse_search_core,
                    base_path,
                    pattern,
                    glob,
                    max_results,
                    context_lines,
                    case_insensitive,
                )
        except (OSError, RuntimeError, ValueError) as exc:
            return GrepResult(error=f"Error searching path '{path or '.'}': {exc}", matches=[])
        matches = []
        for item in raw_matches:
            result_path = self._native_result_path(base_path, item["path"])
            if result_path is not None:
                matches.append({"path": result_path, "line": item["line"], "text": item["text"]})
        return GrepResult(matches=matches)

    def glob(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 1000,
    ) -> Any:
        """Find files with the required native Rust engine."""
        from datetime import datetime

        import synapse_search_core
        from deepagents.backends.filesystem import GlobResult

        base_path = self._resolve_native_search_path(path)
        if base_path is None or not base_path.is_dir():
            return GlobResult(matches=[])
        try:
            payload = synapse_search_core.glob(str(base_path), pattern.lstrip("/"))
        except (OSError, RuntimeError, ValueError) as exc:
            return GlobResult(
                error=f"Error globbing path '{path or '<default>'}': {exc}", matches=[]
            )
        matches = []
        for item in payload["matches"]:
            if len(matches) >= max_results:
                break
            result_path = self._native_result_path(base_path, item["path"])
            if result_path is None:
                continue
            result = {"path": result_path, "is_dir": item["is_dir"]}
            if "size" in item:
                result["size"] = item["size"]
            if "modified_at_unix" in item:
                result["modified_at"] = datetime.fromtimestamp(item["modified_at_unix"]).isoformat()
            matches.append(result)
        matches.sort(key=lambda item: item["path"])
        return GlobResult(matches=matches)

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Run a host shell command with safe text decoding.

        Same contract as ``LocalShellBackend.execute``, but never relies on the
        process locale (e.g. GBK) for stdout/stderr decoding.
        """
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            msg = f"timeout must be positive, got {effective_timeout}"
            raise ValueError(msg)

        args, use_shell, executable = resolve_shell_invocation(
            command, self._shell_executable
        )

        run_kwargs: dict[str, Any] = {
            "args": args,
            "shell": use_shell,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": self._shell_encoding,
            "errors": self._shell_encoding_errors,
            "env": self._env,
            "cwd": str(self.cwd),
        }
        if executable:
            run_kwargs["executable"] = executable

        proc = None
        try:
            # Use Popen directly instead of subprocess.run because run()
            # calls communicate() a second time after TimeoutExpired, which hangs
            # on Windows when the shell spawns child processes that survive the
            # kill and hold pipe handles open.
            proc = subprocess.Popen(**run_kwargs)  # noqa: S602
            stdout, stderr = proc.communicate(timeout=effective_timeout)
            returncode = proc.returncode

        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            msg = _timeout_msg(effective_timeout, timeout)
            return ExecuteResponse(
                output=msg,
                exit_code=124,
                truncated=False,
            )
        except Exception as e:  # noqa: BLE001
            if proc is not None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            return ExecuteResponse(
                output=f"Error executing command ({type(e).__name__}): {e}",
                exit_code=1,
                truncated=False,
            )

        output_parts: list[str] = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            stderr_lines = stderr.strip().split("\n")
            output_parts.extend(f"[stderr] {line}" for line in stderr_lines)

        full_output = "\n".join(output_parts) if output_parts else "<no output>"

        truncated = False
        output = full_output
        if len(output) > self._max_output_bytes:
            output = output[: self._max_output_bytes]
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            truncated = True

        if returncode != 0:
            full_output = f"{full_output.rstrip()}\n\nExit code: {returncode}"
            output = f"{output.rstrip()}\n\nExit code: {returncode}"

        capture_execute_output(
            full_output=full_output,
            displayed_output=output,
            truncated=truncated,
        )
        return ExecuteResponse(
            output=output,
            exit_code=returncode,
            truncated=truncated,
        )


def build_backend(settings: Settings) -> CodingLocalShellBackend:
    """Create a coding shell backend rooted at the workspace.

    Notes
    -----
    - No sandbox isolation (by design for this project).
    - ``inherit_env=True`` so host tools (python/uv/git/node) remain available.
    - ``virtual_mode=True`` improves path semantics for file tools (shell is still unrestricted).
    - Default shell is ``pwsh``; override with ``SHELL_EXECUTABLE`` (cmd/bash/path).
    - Shell output is decoded with ``shell_encoding`` (default UTF-8) to avoid GBK crashes.
    """
    root = Path(settings.workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)

    executable = settings.shell_executable
    if executable is not None:
        executable = executable.strip() or None
    if not executable:
        executable = DEFAULT_SHELL_EXECUTABLE

    return CodingLocalShellBackend(
        root_dir=root,
        virtual_mode=settings.virtual_mode,
        timeout=settings.shell_timeout,
        max_output_bytes=settings.max_output_bytes,
        inherit_env=settings.inherit_env,
        env=None,
        shell_executable=executable,
        shell_encoding=settings.shell_encoding,
        shell_encoding_errors=settings.shell_encoding_errors,
        deny_paths=settings.deny_fs_paths or None,
    )