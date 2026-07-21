"""Patch TUI chrome toward Grok-style top/status bars."""
from __future__ import annotations

from pathlib import Path

p = Path("src/coding_agent/ui/tui.py")
text = p.read_text(encoding="utf-8")

old_head = '''"""Textual TUI — Cursor-style agent transcript with tool timeline.

Layout:
  top:     ≡ path · model/tokens
  user:    gray bar  › prompt · time
  thought: ◆ Thought for Xs  (Ctrl+E expand)
  tools:   ▾ group header + ◆ per-item labels + optional preview
  answer:  clean Markdown
  input:   › Build anything
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
'''

new_head = '''"""Textual TUI — Cursor-style agent transcript with tool timeline.

Layout (Grok/Cursor chrome):
  top:     ≡ [branch] path ·················  used
  user:    gray bar  › prompt · time
  thought: ◆ Thought for Xs  (Ctrl+E expand)
  tools:   ▾ group header + ◆ per-item labels
  answer:  clean Markdown
  footer:  Worked for Xs.
  status:  spinner while busy; model · mode when idle
  input:   › Build anything
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
'''

if old_head not in text:
    raise SystemExit("head not found")
text = text.replace(old_head, new_head, 1)

stamp_fn = '''def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


'''
helpers = '''def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


_FINISHED_RE = re.compile(r"^finished in ([\\d.]+)s\\b", re.I)


def format_token_count(n: int) -> str:
    """Compact token count for chrome (14K, 1.2M)."""
    n = max(0, int(n or 0))
    if n < 1000:
        return str(n)
    if n < 10_000:
        s = f"{n / 1000:.1f}K"
        return s.replace(".0K", "K")
    if n < 1_000_000:
        return f"{(n + 500) // 1000}K"
    if n < 10_000_000:
        s = f"{n / 1_000_000:.1f}M"
        return s.replace(".0M", "M")
    return f"{(n + 500_000) // 1_000_000}M"


def short_model_name(model: str) -> str:
    text = (model or "").strip()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text or "model"


def short_workspace_label(path: Path | str, *, max_len: int = 42) -> str:
    """Prefer last two path segments; ellipsize long absolute paths."""
    pth = Path(path)
    parts = [x for x in pth.parts if x not in {"/", "\\\\"}]
    if len(parts) >= 2:
        label = f"{parts[-2]}/{parts[-1]}"
    else:
        label = pth.name or str(pth)
    if len(label) <= max_len:
        return label
    return "…" + label[-(max_len - 1):]


def soften_turn_footer(message: str) -> str:
    """CLI dump → Grok-style soft footer for the transcript."""
    text = (message or "").strip()
    m = _FINISHED_RE.match(text)
    if m:
        return f"Worked for {m.group(1)}s."
    return text


def _git_branch(cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    branch = (proc.stdout or "").strip()
    if not branch or branch == "HEAD":
        return None
    return branch


'''
if stamp_fn not in text:
    raise SystemExit("stamp not found")
text = text.replace(stamp_fn, helpers, 1)

old_init_tail = '''        self._live_tool_block: ToolGroupBlock | None = None
        self._in_tool_rail = False
        self.title = "coding-agent"
        self.sub_title = str(settings.model)
'''
new_init_tail = '''        self._live_tool_block: ToolGroupBlock | None = None
        self._in_tool_rail = False
        self._context_tokens = 0
        self._last_out_tokens = 0
        ws = Path(getattr(settings, "workspace", Path.cwd()) or Path.cwd())
        self._git_branch = _git_branch(ws)
        self.title = "coding-agent"
        self.sub_title = short_model_name(str(settings.model))
'''
if old_init_tail not in text:
    raise SystemExit("init tail not found")
text = text.replace(old_init_tail, new_init_tail, 1)

old_top = '''    def _refresh_topbar(self, tokens: str | None = None) -> None:
        ws = str(self.settings.workspace)
        model = str(self.settings.model)
        right = tokens or model
        line = Text.assemble(
            ("  ≡  ", _C_MUTED),
            (ws, _C_DIM),
            ("    ·    ", _C_MUTED),
            (right, _C_MUTED),
        )
        self.query_one("#topbar", Static).update(line)

    # -- status ----------------------------------------------------------

    def set_activity(self, phase: str, detail: str = "", reset_timer: bool = False) -> None:
        detail = detail or ""
        if reset_timer or phase != self._phase:
            self._activity_started = time.monotonic()
        self._phase = phase or "idle"
        self._detail = detail
        busy = self._phase not in {"idle", "ready", ""}
        self.query_one("#status", Static).set_class(busy, "busy")
        self.sub_title = f"{self.settings.model} · {self._phase}"
        self._render_status()

    def _render_status(self) -> None:
        elapsed = max(0.0, time.monotonic() - self._activity_started)
        busy = self._phase not in {"idle", "ready", ""}
        status = self.query_one("#status", Static)
        if not busy:
            status.update("")
            return
        spin = _SPINNER[self._spin_i % len(_SPINNER)]
        detail = f"  {self._detail}" if self._detail else ""
        status.update(f"  {spin}  {self._phase}{detail}  ·  {elapsed:.1f}s")

    def _tick_status(self) -> None:
        if self._phase not in {"idle", "ready", ""}:
            self._spin_i += 1
            self._render_status()
'''

new_top = '''    def _usage_right_label(self) -> str:
        """Right chrome: compact context usage (Grok-style 14K, not raw dumps)."""
        if self._context_tokens > 0:
            return format_token_count(self._context_tokens)
        return short_model_name(str(self.settings.model))

    def _refresh_topbar(self, tokens: str | None = None) -> None:
        del tokens  # legacy arg; usage is tracked on the app, not free-form strings
        width = max(int(getattr(self.size, "width", 0) or 0), 48)
        left_parts = ["≡"]
        if self._git_branch:
            left_parts.append(self._git_branch)
        left_parts.append(short_workspace_label(self.settings.workspace))
        left = "  ".join(left_parts)
        right = self._usage_right_label()
        # Keep one cell of padding on each side of the bar.
        pad = max(1, width - len(left) - len(right) - 2)
        line = Text()
        line.append(" " + left, style=_C_DIM)
        line.append(" " * pad, style=_C_MUTED)
        line.append(right + " ", style=_C_MUTED)
        self.query_one("#topbar", Static).update(line)

    def on_resize(self, event: object) -> None:  # noqa: ANN001
        del event
        self._refresh_topbar()
        self._render_status()

    # -- status ----------------------------------------------------------

    def set_activity(self, phase: str, detail: str = "", reset_timer: bool = False) -> None:
        detail = detail or ""
        if reset_timer or phase != self._phase:
            self._activity_started = time.monotonic()
        self._phase = phase or "idle"
        self._detail = detail
        busy = self._phase not in {"idle", "ready", ""}
        self.query_one("#status", Static).set_class(busy, "busy")
        self.sub_title = f"{short_model_name(str(self.settings.model))} · {self._phase}"
        self._render_status()

    def _idle_status_label(self) -> str:
        model = short_model_name(str(self.settings.model))
        # Project default is auto-approve / no human gate.
        return f"{model} · always-approve"

    def _render_status(self) -> None:
        elapsed = max(0.0, time.monotonic() - self._activity_started)
        busy = self._phase not in {"idle", "ready", ""}
        status = self.query_one("#status", Static)
        width = max(int(getattr(self.size, "width", 0) or 0), 48)
        if not busy:
            right = self._idle_status_label()
            pad = max(1, width - len(right) - 1)
            status.update(Text((" " * pad) + right, style=_C_MUTED))
            return
        spin = _SPINNER[self._spin_i % len(_SPINNER)]
        detail = f"  {self._detail}" if self._detail else ""
        left = f"  {spin}  {self._phase}{detail}  ·  {elapsed:.1f}s"
        status.update(Text(left, style=_C_GREEN))

    def _tick_status(self) -> None:
        if self._phase not in {"idle", "ready", ""}:
            self._spin_i += 1
            self._render_status()
'''
if old_top not in text:
    raise SystemExit("top/status block not found")
text = text.replace(old_top, new_top, 1)

old_meta = '''    def append_meta(self, message: str) -> None:
        self._commit_live_tools_to_log()
        self._mount_block(Static(Text(f"  {message}", style=_C_MUTED)))
'''
new_meta = '''    def append_meta(self, message: str) -> None:
        self._commit_live_tools_to_log()
        body = soften_turn_footer(message)
        self._mount_block(Static(Text(f"  {body}", style=_C_MUTED)))
'''
if old_meta not in text:
    raise SystemExit("append_meta not found")
text = text.replace(old_meta, new_meta, 1)

old_tok = '''            if result.total_tokens:
                label = (
                    f"{result.total_tokens} tok"
                    f"  (in={result.input_tokens} out={result.output_tokens})"
                )
                self.call_from_thread(self._refresh_topbar, label)
'''
new_tok = '''            # Context-ish usage for chrome: prefer last-turn prompt tokens.
            if result.input_tokens or result.total_tokens:
                self._context_tokens = int(result.input_tokens or result.total_tokens)
                self._last_out_tokens = int(result.output_tokens or 0)
                self.call_from_thread(self._refresh_topbar)
'''
if old_tok not in text:
    raise SystemExit("token update not found")
text = text.replace(old_tok, new_tok, 1)

if not text.endswith("\n"):
    text += "\n"

compile(text, str(p), "exec")
p.write_text(text, encoding="utf-8", newline="\n")
print("patched ok", len(text.splitlines()))
