"""Apply TUI large-stream hang fixes to tui.py in one shot."""
from __future__ import annotations

from pathlib import Path

path = Path(r"F:\project\agent\autoagents\py-agent\src\coding_agent\ui\tui.py")
text = path.read_text(encoding="utf-8")

old_header = '''_WS_RE = re.compile(r"\\s+")
_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

_C_FG = "#e8eaed"
_C_DIM = "#9aa0a6"
_C_MUTED = "#5f6368"
_C_GREEN = "#81c995"
_C_ORANGE = "#f4b183"
_C_BAR = "#2b2d31"
_C_BG = "#1a1b1e"
_C_TOP = "#121316"


def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")
'''

new_header = '''_WS_RE = re.compile(r"\\s+")
_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

_C_FG = "#e8eaed"
_C_DIM = "#9aa0a6"
_C_MUTED = "#5f6368"
_C_GREEN = "#81c995"
_C_ORANGE = "#f4b183"
_C_BAR = "#2b2d31"
_C_BG = "#1a1b1e"
_C_TOP = "#121316"

# Live stream must stay cheap: full-body Text/Markdown re-layout freezes the
# Textual event loop (status can still tick, transcript becomes unusable).
_STREAM_TAIL_LINES = 28
_STREAM_TAIL_CHARS = 3500
_MARKDOWN_MAX_CHARS = 24_000
_STREAM_INTERVAL_SMALL = 0.12
_STREAM_INTERVAL_MED = 0.25
_STREAM_INTERVAL_LARGE = 0.40


def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


def stream_tail_preview(
    body: str,
    *,
    max_lines: int = _STREAM_TAIL_LINES,
    max_chars: int = _STREAM_TAIL_CHARS,
) -> str:
    """Return only the newest tail of a growing answer for live preview.

    Rendering the full cumulative body on every token is O(n) layout work and
    freezes the main pane long before the final Markdown commit.
    """
    if not body:
        return ""
    text = body
    truncated = False
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\\n".join(lines[-max_lines:])
        truncated = True
    if len(text) > max_chars:
        text = text[-max_chars:]
        # Drop a likely partial first line after hard char cut.
        nl = text.find("\\n")
        if 0 <= nl < 120:
            text = text[nl + 1 :]
        truncated = True
    if truncated:
        return "…\\n" + text.lstrip("\\n")
    return text
'''

if old_header not in text:
    raise SystemExit("header block not found")
text = text.replace(old_header, new_header, 1)

old_init_push = '''    def __init__(self, app: CodingAgentApp) -> None:
        self._app = app
        self.streamed_answer = False
        self.streamed_reasoning = False
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self._open_answer: list[str] = []
        self._open_reasoning: list[str] = []
        self._reasoning_open = False
        self._reasoning_started = 0.0
        self._complete_ids: set[str] = set()
        self._complete_texts: set[str] = set()
        self._last_stream_push = 0.0
        self._min_stream_interval = 0.12
        self._last_activity_push = 0.0
        self._min_activity_interval = 0.10
        # Enhanced tool group state.
        self._group_items: list[ToolItem] = []
        self._group_open = False
        self._group_header_written = False
        self._expanded_item_id: str | None = None
        # Legacy fallback counters.
        self._legacy_pending = 0
        self._legacy_names: list[str] = []
        self._legacy_failed = 0

    def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        fn = getattr(self._app, method)
        try:
            self._app.call_from_thread(fn, *args, **kwargs)
        except RuntimeError:
            fn(*args, **kwargs)

    @staticmethod
    def _norm(text: str) -> str:
        return _WS_RE.sub(" ", (text or "").strip())

    def _push_stream(self, kind: str, body: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_stream_push) < self._min_stream_interval:
            return
        self._last_stream_push = now
        self._call("set_stream", kind, body)
'''

new_init_push = '''    def __init__(self, app: CodingAgentApp) -> None:
        self._app = app
        self.streamed_answer = False
        self.streamed_reasoning = False
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self._open_answer: list[str] = []
        self._open_answer_chars = 0
        self._open_reasoning: list[str] = []
        self._reasoning_open = False
        self._reasoning_started = 0.0
        self._complete_ids: set[str] = set()
        self._complete_texts: set[str] = set()
        self._last_stream_push = 0.0
        # Base interval; adaptive growth is applied in _stream_interval().
        self._min_stream_interval = _STREAM_INTERVAL_SMALL
        self._last_activity_push = 0.0
        self._min_activity_interval = 0.10
        # Enhanced tool group state.
        self._group_items: list[ToolItem] = []
        self._group_open = False
        self._group_header_written = False
        self._expanded_item_id: str | None = None
        # Legacy fallback counters.
        self._legacy_pending = 0
        self._legacy_names: list[str] = []
        self._legacy_failed = 0

    def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        fn = getattr(self._app, method)
        try:
            self._app.call_from_thread(fn, *args, **kwargs)
        except RuntimeError:
            fn(*args, **kwargs)

    @staticmethod
    def _norm(text: str) -> str:
        return _WS_RE.sub(" ", (text or "").strip())

    def _stream_interval(self) -> float:
        """Slow down UI pushes as the answer grows to protect the event loop."""
        base = self._min_stream_interval
        n = self._open_answer_chars
        if n >= 12_000:
            return max(base, _STREAM_INTERVAL_LARGE)
        if n >= 3_000:
            return max(base, _STREAM_INTERVAL_MED)
        return base

    def _push_stream(self, kind: str, body: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_stream_push) < self._stream_interval():
            return
        self._last_stream_push = now
        # Always hand the UI a bounded preview, even on force flushes.
        self._call("set_stream", kind, stream_tail_preview(body))
'''

if old_init_push not in text:
    raise SystemExit("init/push block not found")
text = text.replace(old_init_push, new_init_push, 1)

# Ensure write_answer_token block is the intended version (already partly there)
old_write = '''    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        if not text:
            return
        if msg_id and msg_id in self._complete_ids:
            return
        self.streamed_answer = True
        self._open_answer.append(text)
        self._open_answer_chars += len(text)
        # Join only when the rate limiter actually allows a UI push.  Building
        # the full string on every token is O(n^2) CPU before layout even runs.
        now = time.monotonic()
        if (now - self._last_stream_push) >= self._stream_interval():
            body = "".join(self._open_answer)
            self._push_stream("answer", body, force=True)
        self._push_activity("writing", f"{self._open_answer_chars}c")
'''
if old_write not in text:
    # try legacy full-join version
    legacy_write = '''    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        if not text:
            return
        if msg_id and msg_id in self._complete_ids:
            return
        self.streamed_answer = True
        self._open_answer.append(text)
        body = "".join(self._open_answer)
        self._push_stream("answer", body)
        self._push_activity("writing", f"{len(body)}c")
'''
    if legacy_write not in text:
        raise SystemExit("write_answer_token block not found")
    text = text.replace(legacy_write, old_write, 1)

# ensure complete/finalize reset chars
if "self._open_answer_chars = 0" not in text:
    text = text.replace(
        "        self._open_answer.clear()\n        self.answer_buf.append(body)\n",
        "        self._open_answer.clear()\n        self._open_answer_chars = 0\n        self.answer_buf.append(body)\n",
        1,
    )
    text = text.replace(
        "            self._open_answer.clear()\n            self._call(\"clear_stream\")\n",
        "            self._open_answer.clear()\n            self._open_answer_chars = 0\n            self._call(\"clear_stream\")\n",
        1,
    )

old_css = '''    #stream {{
        height: auto;
        max-height: 40%;
        padding: 0 0 1 2;
        display: none;
        overflow-y: auto;
        background: {_C_BG};
        color: {_C_FG};
    }}
'''
new_css = '''    #stream {{
        /* Fixed region: auto-height growth reflows #log on every token. */
        height: 12;
        max-height: 12;
        padding: 0 0 1 2;
        display: none;
        overflow-y: hidden;
        background: {_C_BG};
        color: {_C_FG};
    }}
'''
if old_css not in text:
    raise SystemExit("css stream block not found")
text = text.replace(old_css, new_css, 1)

old_set = '''    def set_stream(self, kind: str, body: str) -> None:
        stream = self.query_one("#stream", Static)
        if not (body or "").strip():
            self.clear_stream()
            return
        if kind == "reasoning":
            # Reasoning is committed as a historical ThoughtBlock only after
            # the reasoning segment closes.  Never grow a live text widget.
            renderable = Text("  ◆  Thinking…", style=f"italic {_C_DIM}")
        else:
            # Markdown is intentionally rendered once, on completion.  A
            # plain Text preview keeps token streaming cheap and responsive.
            renderable = Text(body, style=_C_FG)
        stream.update(renderable)
        stream.add_class("active")
'''
new_set = '''    def set_stream(self, kind: str, body: str) -> None:
        stream = self.query_one("#stream", Static)
        if not (body or "").strip():
            self.clear_stream()
            return
        if kind == "reasoning":
            # Reasoning is committed as a historical ThoughtBlock only after
            # the reasoning segment closes.  Never grow a live text widget.
            renderable = Text("  ◆  Thinking…", style=f"italic {_C_DIM}")
        else:
            # Markdown is intentionally rendered once, on completion.  Live
            # preview is plain Text of a bounded tail (see stream_tail_preview).
            preview = stream_tail_preview(body)
            renderable = Text(preview, style=_C_FG)
        stream.update(renderable)
        stream.add_class("active")
'''
if old_set not in text:
    raise SystemExit("set_stream block not found")
text = text.replace(old_set, new_set, 1)

old_commit = '''    def commit_answer(self, text: str) -> None:
        body = (text or "").strip()
        if not body:
            return
        self._commit_live_tools_to_log()
        self._mount_block(Static(Group(Markdown(body, code_theme="monokai"), Text(""))))
'''
new_commit = '''    def commit_answer(self, text: str) -> None:
        body = (text or "").strip()
        if not body:
            return
        self._commit_live_tools_to_log()
        # Huge Markdown trees can stall the terminal for seconds.  Prefer
        # plain text once past the soft limit; normal answers stay Markdown.
        if len(body) > _MARKDOWN_MAX_CHARS:
            renderable: Any = Text(body, style=_C_FG)
        else:
            renderable = Markdown(body, code_theme="monokai")
        self._mount_block(Static(Group(renderable, Text(""))))
'''
if old_commit not in text:
    raise SystemExit("commit_answer block not found")
text = text.replace(old_commit, new_commit, 1)

# fix corrupted run_tui if needed
marker = "def run_tui("
idx = text.rfind(marker)
if idx < 0:
    raise SystemExit("run_tui missing")
prefix = text[:idx]
text = prefix + '''def run_tui(
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
print("patched ok", len(text.splitlines()))
