"""Small cross-platform clipboard helpers used by Textual views."""

from __future__ import annotations

import shutil
import subprocess
import sys


def _ps_escape(text: str) -> str:
    """Escape text for a PowerShell single-quoted string."""
    return "'" + text.replace("'", "''") + "'"


def copy_to_clipboard(text: str) -> bool:
    """Copy *text* to the system clipboard, returning whether it was accepted."""
    if not text:
        return False
    try:
        import pyperclip  # type: ignore[import-untyped]

        pyperclip.copy(text)
        return True
    except ImportError:
        pass

    if sys.platform == "win32":
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Set-Clipboard -Value {_ps_escape(text)}",
                ],
                check=False,
                timeout=5,
            )
            return True
        except Exception:  # noqa: BLE001
            pass
    elif sys.platform == "darwin":
        try:
            subprocess.run(
                ["pbcopy"],
                input=text,
                text=True,
                check=False,
                timeout=5,
            )
            return True
        except Exception:  # noqa: BLE001
            pass
    else:
        for command in (("xclip", "-selection", "clipboard"), ("wl-copy",)):
            if shutil.which(command[0]):
                try:
                    subprocess.run(
                        list(command),
                        input=text,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                    return True
                except Exception:  # noqa: BLE001
                    pass
    return False


__all__ = ["copy_to_clipboard"]
