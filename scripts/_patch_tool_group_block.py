"""Patch ToolGroupBlock and close_tool_group in tui.py."""
from pathlib import Path

path = Path(r"F:\project\agent\autoagents\py-agent\src\coding_agent\ui\tui.py")
text = path.read_text(encoding="utf-8")

old_block = '''class ToolGroupBlock(Static):
    """A timeline tool group with in-place collapse and preview updates."""

    def __init__(self, summary: str = "tools") -> None:
        self.summary = summary or "tools"
        self.items: list[ToolItem] = []
        # Tool payloads are intentionally hidden by default.  The timeline
        # should show what happened, not dump file contents into the main
        # reading flow.
        self.collapsed = True
        super().__init__()
        self._render_block()

    def _render_block(self) -> None:
        mark = "▸" if self.collapsed else "▾"
        lines: list[Text] = [
            Text(f"  {mark}  {self.summary}", style=f"{_C_DIM} on {_C_BAR}")
        ]
        if not self.collapsed:
            for item in self.items:
                style = "red" if item.error else (
                    _C_GREEN if item.category == "run" else _C_DIM
                )
                label = item.label or item.name
                if " " in label and item.category in {"read", "edit"}:
                    head, tail = label.split(" ", 1)
                    row = Text(f"  ◆  {head} ", style=style)
                    row.append(tail, style=_C_ORANGE)
                    lines.append(row)
                else:
                    lines.append(Text(f"  ◆  {label}", style=style))
        lines.append(Text(""))
        self.update(Group(*lines))

    def set_summary(self, summary: str) -> None:
        self.summary = summary or "tools"
        self._render_block()

    def add_item(self, item: ToolItem) -> None:
        if not any(existing.id == item.id for existing in self.items):
            self.items.append(item)
        self._render_block()

    def update_preview(self, item_id: str, preview: str, *, error: bool = False) -> None:
        # Kept as an API no-op so the stream protocol remains compatible.
        # TUI tool output will be revisited separately from the timeline UX.
        del item_id, preview, error

    def toggle(self) -> None:
        self.collapsed = not self.collapsed
        self._render_block()

    def on_click(self, event: Click) -> None:
        event.stop()
        self.toggle()
'''

new_block = '''class ToolGroupBlock(Static):
    """A timeline tool group with in-place collapse and preview updates."""

    # Expanded lists past this size become noise; keep the rest behind a count.
    _MAX_EXPANDED_ROWS = 12

    def __init__(self, summary: str = "tools") -> None:
        self.summary = summary or "tools"
        self.items: list[ToolItem] = []
        # Tool payloads are intentionally hidden by default.  The timeline
        # should show what happened, not dump file contents into the main
        # reading flow.
        self.collapsed = True
        super().__init__()
        self._render_block()

    def _sync_summary_from_items(self, *, running: bool | None = None) -> None:
        """Keep the group header honest as items accumulate."""
        if not self.items:
            return
        if running is None:
            running = any(it.status == "running" for it in self.items)
        self.summary = summarize_items(self.items, running=running)

    def _render_block(self) -> None:
        mark = "▸" if self.collapsed else "▾"
        lines: list[Text] = [
            Text(f"  {mark}  {self.summary}", style=f"{_C_DIM} on {_C_BAR}")
        ]
        if not self.collapsed:
            visible = self.items
            overflow = 0
            if len(self.items) > self._MAX_EXPANDED_ROWS:
                visible = self.items[: self._MAX_EXPANDED_ROWS]
                overflow = len(self.items) - self._MAX_EXPANDED_ROWS
            for item in visible:
                style = "red" if item.error else (
                    _C_GREEN if item.category == "run" else _C_DIM
                )
                label = item.label or item.name
                if " " in label and item.category in {"read", "edit", "list"}:
                    head, tail = label.split(" ", 1)
                    row = Text(f"  ◆  {head} ", style=style)
                    row.append(tail, style=_C_ORANGE)
                    lines.append(row)
                else:
                    lines.append(Text(f"  ◆  {label}", style=style))
            if overflow:
                lines.append(Text(f"  … and {overflow} more", style=_C_MUTED))
        lines.append(Text(""))
        self.update(Group(*lines))

    def set_summary(self, summary: str) -> None:
        self.summary = summary or "tools"
        self._render_block()

    def add_item(self, item: ToolItem) -> None:
        if not any(existing.id == item.id for existing in self.items):
            self.items.append(item)
        # Never leave a stale header like "Read 4 files" after more tools land.
        self._sync_summary_from_items()
        self._render_block()

    def update_preview(self, item_id: str, preview: str, *, error: bool = False) -> None:
        # Kept as an API no-op so the stream protocol remains compatible.
        # TUI tool output will be revisited separately from the timeline UX.
        del item_id, preview, error

    def toggle(self) -> None:
        self.collapsed = not self.collapsed
        self._render_block()

    def on_click(self, event: Click) -> None:
        event.stop()
        self.toggle()
'''

if old_block not in text:
    raise SystemExit("ToolGroupBlock block not found")
text = text.replace(old_block, new_block, 1)

# Ensure close_tool_group commits the live block
old_close = '''    def close_tool_group(self) -> None:
        """Freeze the live tool block so the next batch creates a new group."""
        if self._live_tool_block is not None:
            # Final header from items, not a stale early partial summary.
            self._live_tool_block._sync_summary_from_items(running=False)
            self._live_tool_summary = self._live_tool_block.summary
            self._last_tool_summary = self._live_tool_block.summary
            self._live_tool_block._render_block()
        self._commit_live_tools_to_log()
'''

simple_close = '''    def close_tool_group(self) -> None:
        self._render_live_tools()
'''

new_close = '''    def close_tool_group(self) -> None:
        """Freeze the live tool block so the next batch creates a new group."""
        if self._live_tool_block is not None:
            # Final header from items, not a stale early partial summary.
            self._live_tool_block._sync_summary_from_items(running=False)
            self._live_tool_summary = self._live_tool_block.summary
            self._last_tool_summary = self._live_tool_block.summary
            self._live_tool_block._render_block()
        self._commit_live_tools_to_log()
'''

if old_close in text:
    pass  # already good
elif simple_close in text:
    text = text.replace(simple_close, new_close, 1)
elif "def close_tool_group(self) -> None:" in text:
    # replace whatever close_tool_group is
    import re
    text2, n = re.subn(
        r"    def close_tool_group\(self\) -> None:\n(?:.*\n)*?(?=    def |\Z)",
        new_close + "\n",
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit(f"close_tool_group replace failed n={n}")
    text = text2
else:
    raise SystemExit("close_tool_group not found")

# write_tool_item should pick up block summary
old_item = '''    def write_tool_item(self, item: ToolItem) -> None:
        if self._live_tool_block is None:
            self.write_tool_group_header("tools")
        assert self._live_tool_block is not None
        self._live_tool_block.add_item(item)
        self._live_tool_items = list(self._live_tool_block.items)
        self._last_tool_items = list(self._live_tool_items)
'''
new_item = '''    def write_tool_item(self, item: ToolItem) -> None:
        if self._live_tool_block is None:
            self.write_tool_group_header("tools")
        assert self._live_tool_block is not None
        self._live_tool_block.add_item(item)
        # Prefer the block's self-derived summary (always matches items).
        self._live_tool_summary = self._live_tool_block.summary
        self._last_tool_summary = self._live_tool_block.summary
        self._live_tool_items = list(self._live_tool_block.items)
        self._last_tool_items = list(self._live_tool_items)
'''
if old_item in text:
    text = text.replace(old_item, new_item, 1)
elif "Prefer the block's self-derived summary" not in text:
    # maybe already partially different - skip if already has prefer
    if "self._live_tool_block.add_item(item)" in text and "Prefer the block" not in text:
        text = text.replace(
            '''        self._live_tool_block.add_item(item)
        self._live_tool_items = list(self._live_tool_block.items)
        self._last_tool_items = list(self._live_tool_items)
''',
            '''        self._live_tool_block.add_item(item)
        # Prefer the block's self-derived summary (always matches items).
        self._live_tool_summary = self._live_tool_block.summary
        self._last_tool_summary = self._live_tool_block.summary
        self._live_tool_items = list(self._live_tool_block.items)
        self._last_tool_items = list(self._live_tool_items)
''',
            1,
        )

# fix corrupted run_tui
marker = "def run_tui("
idx = text.rfind(marker)
if idx >= 0:
    text = text[:idx] + '''def run_tui(
    *,
    settings: Any,
    thread_id: str | None = None,
    env_path: Path | None = None,
    project_root: Path | None = None,
) -> None:
    """Build agent and launch the Textual app."""
    root = project_root or Path.cwd()
    agent = build_coding_agent(settings, project_root=root)
    tid = thread_id or default_thread_id()
    app = CodingAgentApp(
        agent=agent,
        settings=settings,
        thread_id=tid,
        env_path=env_path,
    )
    app.run()
'''

path.write_text(text, encoding="utf-8", newline="\n")
compile(text, str(path), "exec")
print("ok", len(text.splitlines()))
