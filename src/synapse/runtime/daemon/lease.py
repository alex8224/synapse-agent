"""Cross-platform held lock and ownership-safe discovery metadata."""

from __future__ import annotations

import errno
import json
import os
import stat
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synapse.runtime.daemon.config import ensure_directory


class DaemonAlreadyRunningError(RuntimeError):
    """Another daemon owns the state directory lock."""


def _lock_posix(handle: Any) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_posix(handle: Any) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_windows(handle: Any) -> None:
    import msvcrt

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock_windows(handle: Any) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class DaemonLease:
    """Own the daemon lock for the lifetime of one process instance."""

    def __init__(
        self,
        state_dir: Path,
        *,
        lock_fn: Callable[[Any], None] | None = None,
        unlock_fn: Callable[[Any], None] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.lock_path = self.state_dir / "daemon.lock"
        self.metadata_path = self.state_dir / "daemon.json"
        self.instance_id = uuid.uuid4().hex
        self._handle: Any | None = None
        self._released = False
        self._metadata_published = False
        self._lock_fn = lock_fn
        self._unlock_fn = unlock_fn
        self._platform_name = platform_name

    @property
    def _is_windows(self) -> bool:
        """Permit in-process tests to exercise both native lock branches."""
        return (self._platform_name or os.name) == "nt"

    def acquire(self) -> None:
        ensure_directory(self.state_dir)
        if self._is_windows:
            self._acquire_windows()
        else:
            self._acquire_posix()

    def _acquire_posix(self) -> None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        handle: Any | None = None
        try:
            handle = os.fdopen(os.open(self.lock_path, flags, 0o600), "r+b")
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("daemon lock must be a regular file")
            if status.st_mode & 0o077:
                raise ValueError("daemon lock permissions are too broad")
            (self._lock_fn or _lock_posix)(handle)
        except BlockingIOError:
            if handle is not None:
                handle.close()
            raise DaemonAlreadyRunningError("runtime daemon is already running") from None
        except (OSError, ValueError) as exc:
            if handle is not None:
                handle.close()
            if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DaemonAlreadyRunningError("runtime daemon is already running") from None
            raise ValueError("daemon lock could not be acquired") from None
        self._handle = handle

    def _acquire_windows(self) -> None:
        handle: Any | None = None
        try:
            handle = self.lock_path.open("a+b")
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("daemon lock must be a regular file")
            (self._lock_fn or _lock_windows)(handle)
        except (OSError, ValueError):
            if handle is not None:
                handle.close()
            raise DaemonAlreadyRunningError("runtime daemon is already running") from None
        self._handle = handle

    @staticmethod
    def _chmod_private(path: Path, mode: int) -> None:
        if os.name != "nt":
            path.chmod(mode)

    @property
    def acquired(self) -> bool:
        return self._handle is not None and not self._released

    def publish(self, *, host: str, port: int) -> dict[str, Any]:
        if not self.acquired:
            raise RuntimeError("daemon lease is not acquired")
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "pid": os.getpid(),
            "host": host,
            "port": port,
            "started_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "instance_id": self.instance_id,
        }
        self._atomic_write(metadata)
        self._metadata_published = True
        return {key: value for key, value in metadata.items() if key != "instance_id"}

    def _atomic_write(self, metadata: dict[str, Any]) -> None:
        if self.metadata_path.is_symlink() or (
            self.metadata_path.exists() and not self.metadata_path.is_file()
        ):
            raise ValueError("daemon metadata must be a regular file")
        temp = self.metadata_path.with_name(f".{self.metadata_path.name}.{self.instance_id}.tmp")
        created_identity: tuple[int, int] | None = None
        open_fd: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temp, flags, 0o600)
            open_fd = fd
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("daemon metadata must be a regular file")
            created_identity = (status.st_dev, status.st_ino)
            stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            open_fd = None
            with stream:
                json.dump(metadata, stream, separators=(",", ":"), ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.metadata_path)
            self._chmod_private(self.metadata_path, 0o600)
        except BaseException:
            try:
                if created_identity is not None:
                    current = temp.stat()
                    if (current.st_dev, current.st_ino) != created_identity:
                        raise OSError("metadata temporary path was replaced")
                temp.unlink()
            except OSError:
                pass
            if open_fd is not None:
                try:
                    os.close(open_fd)
                except OSError:
                    pass
            raise

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        handle = self._handle
        self._handle = None
        try:
            if self._metadata_published and self._metadata_matches_owner():
                try:
                    self.metadata_path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            if handle is not None:
                if self._is_windows:
                    try:
                        (self._unlock_fn or _unlock_windows)(handle)
                    except BaseException:
                        pass
                else:
                    try:
                        (self._unlock_fn or _unlock_posix)(handle)
                    except BaseException:
                        pass
                handle.close()

    def _metadata_matches_owner(self) -> bool:
        fd: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.metadata_path, flags)
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or status.st_size > 4096:
                return False
            with os.fdopen(fd, "rb") as stream:
                fd = None
                value = json.loads(stream.read(4097))
            return isinstance(value, dict) and value.get("instance_id") == self.instance_id
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def __enter__(self) -> DaemonLease:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
