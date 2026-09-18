"""Configuration for the foreground runtime daemon."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from synapse.settings.config_paths import user_config_dir

_DEFAULT_STATE_DIR = user_config_dir() / "runtime"


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Validated command-line configuration."""

    host: str = "127.0.0.1"
    port: int = 0
    state_dir: Path = _DEFAULT_STATE_DIR
    token_file: Path | None = None

    def __post_init__(self) -> None:
        if type(self.host) is not str or not self.host or "\x00" in self.host:
            raise ValueError("host must be a non-empty string")
        try:
            if len(self.host.encode("utf-8")) > 255:
                raise ValueError("host exceeds the length limit")
        except UnicodeEncodeError:
            raise ValueError("host must be a non-empty string") from None
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        state_dir = Path(self.state_dir).expanduser()
        token_file = (
            Path(self.token_file).expanduser()
            if self.token_file is not None
            else state_dir / "token"
        )
        object.__setattr__(self, "state_dir", state_dir)
        object.__setattr__(self, "token_file", token_file)

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "daemon.lock"

    @property
    def metadata_file(self) -> Path:
        return self.state_dir / "daemon.json"


def ensure_directory(path: Path) -> None:
    """Create or validate a private state directory without following symlinks."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    if before is not None and (
        stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode)
    ):
        raise ValueError("state directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True)
    try:
        status = os.lstat(path)
    except OSError:
        raise ValueError("state directory must be a directory") from None
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError("state directory must be a directory")
    if os.name != "nt":
        if before is None:
            path.chmod(0o700)
            status = os.lstat(path)
        euid = getattr(os, "geteuid", lambda: os.getuid())()
        if status.st_uid != euid or status.st_mode & 0o077:
            raise ValueError("state directory must be private and owned by the current user")
