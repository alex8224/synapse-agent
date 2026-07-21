"""Harden TUI against empty / premature tool groups during subagent runs."""
from pathlib import Path

p = Path("src/coding_agent/ui/tui.py")
text = p.read_text(encoding="utf-8")

replacements = [
    (
        """    def write_reasoning(self, text: str) -> None:
        if not text:
            return
        # New thought after a tool batch must not append into the live tools panel.
        if self._group_open and self._group_header_written:
            self._finalize_open_group()
""",
        """    def write_reasoning(self, text: str) -> None:
        if not text:
            return
        # New thought after a completed tool batch must not append into tools.
        # Never seal a still-running group (e.g. parent task/subagent).
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
""",
    ),
    (
        """        self.close_reasoning()
        if self._group_open and self._group_header_written:
            self._finalize_open_group()
        self.streamed_answer = True
""",
        """        self.close_reasoning()
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
        self.streamed_answer = True
""",
    ),
    (
        """        self.close_reasoning()
        if self._group_open and self._group_header_written:
            self._finalize_open_group()
        if msg_id:
""",
        """        self.close_reasoning()
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
        if msg_id:
""",
    ),
    (
        """    def _finalize_open_group(self) -> None:
        \"\"\"Seal the current visual tool group and release sink state.\"\"\"
        if not self._group_open:
            return
        if self._group_items:
""",
        """    def _finalize_open_group(self, *, force: bool = False) -> None:
        \"\"\"Seal the current visual tool group and release sink state.\"\"\"
        if not self._group_open:
            return
        if not force and any(it.status == "running" for it in self._group_items):
            return
        if self._group_items:
""",
    ),
    (
        """    def tool_group_closed(self, group_id: str) -> None:
        \"\"\"Close one stream tool batch as its own visual group.\"\"\"
        del group_id
        self._finalize_open_group()

    def turn_finished(self) -> None:
        \"\"\"Seal any leftover open group at end of one turn.\"\"\"
        self._finalize_open_group()
""",
        """    def tool_group_closed(self, group_id: str) -> None:
        \"\"\"Close one stream tool batch as its own visual group.\"\"\"
        del group_id
        self._finalize_open_group(force=True)

    def turn_finished(self) -> None:
        \"\"\"Seal any leftover open group at end of one turn.\"\"\"
        self._finalize_open_group(force=True)
""",
    ),
    (
        """    def write_tool_group_header(self, summary: str, collapsed: bool = True) -> None:
        # A sealed previous group must leave _live_tool_block as None so the
        # next batch always creates a fresh block (never reuses a frozen one).
        if self._live_tool_block is None:
            block = ToolGroupBlock(summary)
""",
        """    def write_tool_group_header(self, summary: str, collapsed: bool = True) -> None:
        # Never paint empty placeholder groups (\"0 tools\").
        if (summary or \"\").strip() in {\"\", \"0 tools\", \"tools\", \"Running 0 tools\"}:
            if self._live_tool_block is None or not self._live_tool_block.items:
                return
        # A sealed previous group must leave _live_tool_block as None so the
        # next batch always creates a fresh block (never reuses a frozen one).
        if self._live_tool_block is None:
            block = ToolGroupBlock(summary)
""",
    ),
]

for i, (old, new) in enumerate(replacements):
    if old not in text:
        raise SystemExit(f"replacement {i} not found")
    text = text.replace(old, new, 1)

compile(text, str(p), "exec")
p.write_text(text, encoding="utf-8", newline="\n")
print("tui hardened ok")
