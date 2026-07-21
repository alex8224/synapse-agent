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

import shutil
import subprocess
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from synapse.config import Settings

# Default shell for this project (PowerShell 7+). Falls back if not on PATH.
DEFAULT_SHELL_EXECUTABLE = "pwsh"


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
            exe = raw
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command], False, None

    # bash / sh
    if base in {"bash", "bash.exe", "sh", "sh.exe"}:
        exe = raw if Path(raw).is_file() else shutil.which(raw) or raw
        return [exe, "-lc", command], False, None

    # Unknown absolute/custom binary: treat as shell program with shell=True.
    return command, True, raw


class CodingLocalShellBackend(LocalShellBackend):
    """Local shell backend with explicit encoding and configurable shell."""

    def __init__(
        self,
        *args: Any,
        shell_executable: str | None = DEFAULT_SHELL_EXECUTABLE,
        shell_encoding: str = "utf-8",
        shell_encoding_errors: str = "replace",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._shell_executable = (shell_executable or DEFAULT_SHELL_EXECUTABLE).strip()
        self._shell_encoding = shell_encoding or "utf-8"
        self._shell_encoding_errors = shell_encoding_errors or "replace"

        # Prefer UTF-8 for Python children; does not fix every native CLI, but helps.
        self._env.setdefault("PYTHONUTF8", "1")
        self._env.setdefault("PYTHONIOENCODING", "utf-8")

    def _ripgrep_search(
        self,
        pattern: str,
        base_full: Path,
        include_glob: str | None,
    ) -> dict[str, list[tuple[int, str]]] | None:
        """UTF-8-safe ripgrep search (avoids Windows GBK decode crashes).

        Upstream ``FilesystemBackend._ripgrep_search`` uses ``text=True`` without
        ``encoding``, which on Chinese Windows can raise / leave ``stdout=None``
        and then crash on ``stdout.splitlines()``.
        """
        import json

        import deepagents.backends.filesystem as fs

        rg_path = fs._resolve_ripgrep_path()
        if rg_path is None:
            return None

        cmd = [rg_path, "--json", "-F"]
        if include_glob:
            cmd.extend(["--glob", include_glob])

        rg_cwd: str | None = None
        if base_full.is_dir():
            cmd.extend(["--", pattern, "."])
            rg_cwd = str(base_full)
        else:
            cmd.extend(["--", pattern, str(base_full)])

        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                encoding=self._shell_encoding,
                errors=self._shell_encoding_errors,
                timeout=fs.DEFAULT_GREP_TIMEOUT,
                check=False,
                cwd=rg_cwd,
            )
        except subprocess.TimeoutExpired:
            fs.logger.warning(
                "ripgrep timed out after %ds; using Python grep fallback",
                fs.DEFAULT_GREP_TIMEOUT,
            )
            return None
        except (FileNotFoundError, PermissionError, NotADirectoryError, OSError) as e:
            fs.logger.warning(
                "ripgrep subprocess failed (%s: %s); using Python grep fallback",
                type(e).__name__,
                e,
            )
            try:
                fs._resolve_ripgrep_path.cache_clear()
            except Exception:  # noqa: BLE001
                pass
            return None
        except UnicodeDecodeError as e:
            # Should be unreachable with errors=replace; keep fallback safety.
            fs.logger.warning(
                "ripgrep decode failed (%s); using Python grep fallback", e
            )
            return None

        if proc.returncode not in (0, 1):
            stderr = (proc.stderr or "").strip()[:500]
            fs.logger.warning(
                "ripgrep exited %d (stderr=%r); using Python grep fallback",
                proc.returncode,
                stderr,
            )
            return None

        stdout = proc.stdout or ""
        results: dict[str, list[tuple[int, str]]] = {}
        try:
            base_resolved = base_full.resolve()
        except OSError:
            base_resolved = base_full

        for line in stdout.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            data_type = data.get("type")
            if data_type == "error":
                continue
            if data_type != "match":
                continue
            pdata = data.get("data", {}) or {}
            ftext = (pdata.get("path") or {}).get("text")
            if not ftext:
                continue
            raw = Path(ftext)
            p = raw if raw.is_absolute() else (base_full / raw)
            try:
                p.resolve().relative_to(base_resolved)
            except (ValueError, OSError):
                continue
            if self.virtual_mode:
                try:
                    virt = self._to_virtual_path(p)
                except (ValueError, OSError, RuntimeError):
                    continue
            else:
                virt = str(p)
            ln = pdata.get("line_number")
            lt = (pdata.get("lines") or {}).get("text", "").rstrip("\n")
            if ln is None:
                continue
            results.setdefault(virt, []).append((int(ln), lt))

        return results

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
            "check": False,
            "shell": use_shell,
            "capture_output": True,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": self._shell_encoding,
            "errors": self._shell_encoding_errors,
            "timeout": effective_timeout,
            "env": self._env,
            "cwd": str(self.cwd),
        }
        if executable:
            run_kwargs["executable"] = executable

        try:
            result = subprocess.run(**run_kwargs)  # noqa: S602

            output_parts: list[str] = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                stderr_lines = result.stderr.strip().split("\n")
                output_parts.extend(f"[stderr] {line}" for line in stderr_lines)

            output = "\n".join(output_parts) if output_parts else "<no output>"

            truncated = False
            if len(output) > self._max_output_bytes:
                output = output[: self._max_output_bytes]
                output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
                truncated = True

            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"

            return ExecuteResponse(
                output=output,
                exit_code=result.returncode,
                truncated=truncated,
            )

        except subprocess.TimeoutExpired:
            if timeout is not None:
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds "
                    "(custom timeout). The command may be stuck or require more time."
                )
            else:
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds. "
                    "For long-running commands, re-run using the timeout parameter."
                )
            return ExecuteResponse(
                output=msg,
                exit_code=124,
                truncated=False,
            )
        except Exception as e:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(e).__name__}): {e}",
                exit_code=1,
                truncated=False,
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
    )
