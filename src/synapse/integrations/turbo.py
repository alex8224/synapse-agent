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

import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from synapse.settings.config_paths import user_config_dir

# Pinned compatible turbo release. Bump together with the headroom-turbo fork
# tag (turbo-v{version}) when the sidecar contract changes.
TURBO_VERSION = "0.1.0"
TURBO_RELEASE_REPO = "alex8224/headroom"
DEFAULT_PORT = 8787

_BIN_DIRNAME = "bin"
_VERSION_FILENAME = "headroom-turbo.version"
_RELEASE_BASE = f"https://github.com/{TURBO_RELEASE_REPO}/releases/download"

# Runtime availability state, set by ensure_turbo_running(). Models only route
# through the proxy when it is actually healthy; otherwise turbo degrades to a
# direct connection for the current process.
_proxy_ready = False


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


def proxy_ready() -> bool:
    """Return True once the local turbo proxy has passed a health check."""
    return _proxy_ready


def _health_ok(url: str) -> bool:
    """True when the proxy /health endpoint reports healthy."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return str(data.get("status", "")).lower() == "healthy"
    except Exception:  # noqa: BLE001 - any failure means not ready yet
        return False


def _start_proxy(port: int) -> None:
    """Launch the sidecar proxy detached from the current process."""
    binary = turbo_binary_path()
    if not binary.is_file():
        raise RuntimeError(f"headroom-turbo binary missing: {binary}")
    cmd = [
        str(binary),
        "proxy",
        "--port",
        str(port),
        "--disable-kompress",
        "--host",
        "127.0.0.1",
    ]
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)  # noqa: S603 - fixed args, no shell


def ensure_turbo_running(
    progress: Callable[[str], None] | None = None,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 30.0,
) -> bool:
    """Start the sidecar proxy (if needed) and wait until it is healthy.

    Reuses an already-running healthy proxy on the same port. Returns True when
    the proxy is ready; False means turbo mode should degrade to direct
    connections for this process.
    """
    global _proxy_ready
    url = f"http://127.0.0.1:{port}/health"
    if _health_ok(url):
        _proxy_ready = True
        return True

    if progress is not None:
        progress("starting headroom-turbo proxy")
    try:
        _start_proxy(port)
    except Exception:  # noqa: BLE001 - startup failure degrades, never blocks
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok(url):
            _proxy_ready = True
            return True
        time.sleep(0.5)
    return False
