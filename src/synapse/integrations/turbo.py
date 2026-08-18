"""headroom-turbo sidecar binary management.

Turbo mode routes model traffic through a local ``headroom-turbo`` proxy
binary. This module pins a compatible release version and downloads the
platform asset from GitHub Releases on first use (or when the installed
version drifts).

Layout::

    ~/.synapse/bin/
      headroom-turbo(.exe)     # the sidecar binary
      headroom-turbo.version   # installed turbo release version
"""

from __future__ import annotations

import platform
import shutil
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from synapse.settings.config_paths import user_config_dir

# Pinned compatible turbo release. Bump together with the headroom-turbo fork
# tag (turbo-v{version}) when the sidecar contract changes.
TURBO_VERSION = "0.1.0"
TURBO_RELEASE_REPO = "alex8224/headroom"

_BIN_DIRNAME = "bin"
_VERSION_FILENAME = "headroom-turbo.version"
_RELEASE_BASE = f"https://github.com/{TURBO_RELEASE_REPO}/releases/download"


def _asset_name() -> str | None:
    """Return the release asset name for the current platform, or None."""
    machine = platform.machine().lower()
    if sys.platform.startswith("win") and machine in {"amd64", "x86_64"}:
        return "headroom-turbo-windows-x86_64.exe"
    if sys.platform.startswith("linux") and machine in {"amd64", "x86_64"}:
        return "headroom-turbo-linux-x86_64"
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        return "headroom-turbo-macos-arm64"
    return None


def turbo_bin_dir() -> Path:
    """Directory holding the sidecar binary and its version marker."""
    return user_config_dir() / _BIN_DIRNAME


def turbo_binary_path() -> Path:
    """Path to the sidecar binary for the current platform."""
    asset = _asset_name() or "headroom-turbo"
    if sys.platform.startswith("win") and not asset.endswith(".exe"):
        asset += ".exe"
    return turbo_bin_dir() / asset


def turbo_version_path() -> Path:
    """Path to the installed-version marker file."""
    return turbo_bin_dir() / _VERSION_FILENAME


def installed_turbo_version() -> str | None:
    """Return the installed turbo version, or None when not installed."""
    try:
        value = turbo_version_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def ensure_turbo_binary(
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Ensure the pinned headroom-turbo binary is present and current.

    Downloads from GitHub Releases when missing or when the installed version
    does not match :data:`TURBO_VERSION`. Returns the binary path.
    """
    installed = installed_turbo_version()
    if installed == TURBO_VERSION and turbo_binary_path().is_file():
        return turbo_binary_path()

    asset = _asset_name()
    if asset is None:
        raise RuntimeError(
            "no headroom-turbo binary for "
            f"{sys.platform}/{platform.machine()}; install it manually into "
            f"{turbo_bin_dir()}"
        )

    url = f"{_RELEASE_BASE}/turbo-v{TURBO_VERSION}/{asset}"
    dest = turbo_binary_path()
    if progress is not None:
        progress(f"downloading headroom-turbo v{TURBO_VERSION}")
    _download(url, dest)
    turbo_version_path().write_text(TURBO_VERSION, encoding="utf-8")
    if progress is not None:
        progress(f"headroom-turbo v{TURBO_VERSION} ready")
    return dest


def _download(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` atomically (temp file + rename)."""
    import os

    turbo_bin_dir().mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "synapse-turbo"})
    fd, tmp = tempfile.mkstemp(prefix=".turbo-", dir=turbo_bin_dir())
    os.close(fd)
    try:
        with urllib.request.urlopen(request, timeout=600) as resp, open(
            tmp, "wb"
        ) as out:
            shutil.copyfileobj(resp, out, length=1024 * 256)
        os.replace(tmp, dest)
        if not sys.platform.startswith("win"):
            dest.chmod(0o755)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
