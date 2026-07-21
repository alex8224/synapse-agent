"""Project-scoped input history for chat CLI / TUI (up/down navigation)."""

from __future__ import annotations

from pathlib import Path


def _read_text_lossy(path: Path) -> str:
    """Read history file, tolerating legacy Windows encodings (GBK etc.)."""
    data = path.read_bytes()
    if not data:
        return ""
    # UTF-8 BOM
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    # UTF-16 LE/BE BOM
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le")
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be")
    for enc in ("utf-8", "gbk", "cp936", "gb18030", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class InputHistory:
    """Line-based history stored under the project ``.synapse/`` dir.

    Shares the same default file as the readline chat CLI
    (``.synapse/history``) so entries accumulate across UIs.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = 1000,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self.entries: list[str] = []
        self._index: int | None = None
        self._draft: str = ""
        self.load()

    @classmethod
    def for_project(cls, project_root: Path | None = None, **kwargs) -> InputHistory:
        from synapse.config_paths import SYNAPSE_DIRNAME

        root = Path(project_root or Path.cwd()).expanduser().resolve()
        return cls(root / SYNAPSE_DIRNAME / "history", **kwargs)

    def load(self) -> None:
        self.entries = []
        try:
            raw = _read_text_lossy(self.path)
        except (FileNotFoundError, OSError):
            return
        for line in raw.splitlines():
            text = line.rstrip("\n\r")
            if not text or text.startswith("_HiStOrY_"):
                continue
            # readline may escape leading spaces; keep content as-is after strip of
            # trailing junk only. Preserve intentional leading spaces rarely used.
            self.entries.append(text)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries :]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            body = "\n".join(self.entries[-self.max_entries :])
            if body:
                body += "\n"
            self.path.write_text(body, encoding="utf-8")
        except OSError:
            pass

    def add(self, text: str) -> None:
        line = (text or "").strip()
        if not line:
            self.reset_cursor()
            return
        if self.entries and self.entries[-1] == line:
            self.reset_cursor()
            return
        self.entries.append(line)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries :]
        self.save()
        self.reset_cursor()

    def reset_cursor(self) -> None:
        self._index = None
        self._draft = ""

    def up(self, current: str) -> str | None:
        """Move to older entry. Returns text to put in the prompt, or None."""
        if not self.entries:
            return None
        if self._index is None:
            self._draft = current or ""
            self._index = len(self.entries) - 1
        elif self._index > 0:
            self._index -= 1
        return self.entries[self._index]

    def down(self, current: str) -> str | None:  # noqa: ARG002
        """Move to newer entry, or restore the draft when leaving history."""
        if self._index is None:
            return None
        if self._index < len(self.entries) - 1:
            self._index += 1
            return self.entries[self._index]
        # Past the newest → restore live draft
        self._index = None
        draft = self._draft
        self._draft = ""
        return draft
