"""Cursor-style tool timeline pure model (no Textual dependency).

Used by TUI (and optionally stream) to:
- classify tools
- build single-item labels (``Read README.md``)
- aggregate group headers (``Read 22 files, Searched 1 pattern``)
- truncate tool-result previews
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

# Default preview budget (matches docs/tui-cursor-refactor.md).
DEFAULT_PREVIEW_CHARS = 2000
DEFAULT_PREVIEW_LINES = 40

_PATH_KEYS = (
    "path",
    "file_path",
    "filename",
    "file",
    "target",
    "target_file",
    "source",
    "src",
    "directory",
    "dir",
    "glob",
)

_CMD_KEYS = ("command", "cmd", "code", "script")
_PATTERN_KEYS = ("pattern", "query", "regex", "needle")
_INTENT_KEYS = ("intent", "purpose", "reason")
_TODO_TOOL_NAMES = frozenset({"write_todos", "todo_write", "todos"})
_TODO_DONE = frozenset({"completed", "done", "complete", "finished"})
_TODO_ACTIVE = frozenset({"in_progress", "in-progress", "running", "active", "doing"})


@dataclass
class ToolItem:
    id: str
    name: str
    category: str
    label: str
    path: str | None = None
    status: str = "running"
    preview: str | None = None
    error: bool = False
    sub: bool = False


@dataclass
class ToolGroup:
    id: str
    summary: str
    items: list[ToolItem] = field(default_factory=list)
    collapsed: bool = False
    running: bool = True


@dataclass
class ThoughtBlock:
    elapsed_s: float
    body: str
    collapsed: bool = False


def tool_category(name: str) -> str:
    n = (name or "").lower()
    if n in {"read_file", "read", "read_file_lines"}:
        return "read"
    if n in {"write_file", "edit_file", "write", "edit", "create_file"}:
        return "edit"
    if n in {"ls", "list_dir", "list_directory"}:
        return "list"
    if n in {"glob", "find"}:
        return "glob"
    if n in {"grep", "search", "rg"}:
        return "search"
    if n in {"execute", "run", "shell", "bash"}:
        return "run"
    if n in {"task", "subagent"}:
        return "task"
    return "other"


def _basename(path: str | None) -> str | None:
    if not path:
        return None
    s = str(path).strip().replace("\\", "/")
    if not s:
        return None
    # Keep trailing slash dirs readable.
    if s.endswith("/") and s != "/":
        s = s.rstrip("/")
        base = PurePosixPath(s).name
        return f"{base}/" if base else s
    name = PurePosixPath(s).name
    if not name:
        # Windows drive edge
        name = PureWindowsPath(path).name
    return name or s


def extract_path(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in _PATH_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract_command(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in _CMD_KEYS:
        val = args.get(key)
        if val is None:
            continue
        s = str(val).replace("\n", " ").strip()
        if s:
            return s if len(s) <= 72 else s[:71] + "…"
    return None


def extract_pattern(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in _PATTERN_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            s = val.strip()
            return s if len(s) <= 40 else s[:39] + "…"
    return None



def extract_intent(args: Any, *, limit: int = 96) -> str | None:
    """Return the model-provided user-facing intent string, if any."""
    if not isinstance(args, dict):
        return None
    for key in _INTENT_KEYS:
        val = args.get(key)
        if not isinstance(val, str):
            continue
        s = " ".join(val.split()).strip()
        if not s:
            continue
        return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
    return None


def is_todo_tool(name: str | None) -> bool:
    return (name or "").lower() in _TODO_TOOL_NAMES


def _todo_status_key(raw: Any) -> str:
    return str(raw or "pending").strip().casefold().replace(" ", "_").replace("-", "_")


def extract_todos(args: Any) -> list[dict[str, Any]]:
    """Normalize ``write_todos`` args into ``{content,status}`` dicts."""
    if not isinstance(args, dict):
        return []
    todos = args.get("todos")
    if not isinstance(todos, list):
        return []
    out: list[dict[str, Any]] = []
    for t in todos:
        if isinstance(t, dict):
            content = str(
                t.get("content") or t.get("text") or t.get("title") or ""
            ).strip()
            status = _todo_status_key(t.get("status")) or "pending"
            out.append({"content": content, "status": status})
        elif isinstance(t, str) and t.strip():
            out.append({"content": t.strip(), "status": "pending"})
    return out


def todo_counts(todos: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Return ``(done, active, pending)`` counts."""
    done = active = pending = 0
    for t in todos:
        st = _todo_status_key(t.get("status"))
        if st in _TODO_DONE:
            done += 1
        elif st in _TODO_ACTIVE:
            active += 1
        else:
            pending += 1
    return done, active, pending


def summarize_todos(todos: list[dict[str, Any]] | None) -> str | None:
    """One-line label: ``Todos 1/4 · in progress: explore agent``."""
    if not todos:
        return None
    done, active, _pending = todo_counts(todos)
    total = len(todos)
    label = f"Todos {done}/{total}"
    if active:
        current = next(
            (
                str(t.get("content") or "").strip()
                for t in todos
                if _todo_status_key(t.get("status")) in _TODO_ACTIVE
            ),
            "",
        )
        if current:
            if len(current) > 42:
                current = current[:41].rstrip() + "…"
            return f"{label} · in progress: {current}"
        return f"{label} · {active} in progress"
    if done == total and total:
        return f"{label} · all done"
    return label


# Status marks for plain-text preview + TUI checklist widget.
# Prefer common Unicode (not emoji): ✓ done, ● active, ○ pending.
TODO_MARK_DONE = "✓"
TODO_MARK_ACTIVE = "●"
TODO_MARK_PENDING = "○"
_TODO_MARK_BY_KIND = {
    "done": TODO_MARK_DONE,
    "active": TODO_MARK_ACTIVE,
    "pending": TODO_MARK_PENDING,
}
# Accept older ASCII marks when re-parsing stored previews.
_TODO_KIND_BY_MARK = {
    TODO_MARK_DONE: "done",
    TODO_MARK_ACTIVE: "active",
    TODO_MARK_PENDING: "pending",
    "x": "done",
    "X": "done",
    "~": "active",
    "…": "active",
    "·": "pending",
    " ": "pending",
    "-": "pending",
}


def todo_status_kind(status: Any) -> str:
    """Map raw status to ``done`` / ``active`` / ``pending``."""
    st = _todo_status_key(status)
    if st in _TODO_DONE:
        return "done"
    if st in _TODO_ACTIVE:
        return "active"
    return "pending"


def todo_mark(kind_or_status: Any) -> str:
    """Return the display mark for a kind or raw status string."""
    kind = str(kind_or_status or "").strip().casefold()
    if kind in _TODO_MARK_BY_KIND:
        return _TODO_MARK_BY_KIND[kind]
    return _TODO_MARK_BY_KIND[todo_status_kind(kind_or_status)]


@dataclass(frozen=True)
class TodoRow:
    """One checklist row for rendering."""

    content: str
    status: str  # raw / normalized status text
    kind: str  # done | active | pending
    mark: str

    @property
    def line(self) -> str:
        return f"{self.mark} {self.content}"


def iter_todo_rows(
    todos: list[dict[str, Any]] | None,
    *,
    max_items: int | None = None,
) -> list[TodoRow]:
    """Build structured checklist rows from normalized todos."""
    if not todos:
        return []
    rows: list[TodoRow] = []
    items = todos if max_items is None else todos[:max_items]
    for t in items:
        content = str(t.get("content") or "").strip() or "(empty)"
        status = _todo_status_key(t.get("status")) or "pending"
        kind = todo_status_kind(status)
        rows.append(
            TodoRow(
                content=content,
                status=status,
                kind=kind,
                mark=todo_mark(kind),
            )
        )
    return rows


def format_todos_preview(
    todos: list[dict[str, Any]] | None,
    *,
    max_items: int = 16,
) -> str | None:
    """Multi-line checklist: ``✓`` done / ``●`` active / ``○`` pending."""
    if not todos:
        return None
    rows = iter_todo_rows(todos, max_items=max_items)
    lines = [r.line for r in rows]
    if len(todos) > max_items:
        lines.append(f"… +{len(todos) - max_items} more")
    done, active, pending = todo_counts(todos)
    lines.append(f"— done {done} · doing {active} · todo {pending}")
    return "\n".join(lines)


def parse_todo_preview_lines(preview: str | None) -> list[TodoRow]:
    """Best-effort parse of a stored checklist preview back into rows."""
    if not preview or not str(preview).strip():
        return []
    rows: list[TodoRow] = []
    for raw in str(preview).splitlines():
        line = raw.strip()
        if not line or line.startswith("—") or line.startswith("…"):
            continue
        # New form: "✓ content" / "● content" / "○ content"
        if line[0] in _TODO_KIND_BY_MARK and (len(line) == 1 or line[1] == " "):
            mark = line[0]
            kind = _TODO_KIND_BY_MARK[mark]
            content = line[1:].strip() or "(empty)"
            rows.append(TodoRow(content=content, status=kind, kind=kind, mark=todo_mark(kind)))
            continue
        # Legacy form: "[x] content" / "[~] content" / "[ ] content"
        if line.startswith("[") and "]" in line[:4]:
            close = line.find("]")
            mark_raw = line[1:close]
            kind = _TODO_KIND_BY_MARK.get(mark_raw, "pending")
            content = line[close + 1 :].strip() or "(empty)"
            rows.append(
                TodoRow(
                    content=content,
                    status=kind,
                    kind=kind,
                    mark=todo_mark(kind),
                )
            )
    return rows


def category_phrase(cat: str, n: int) -> str:
    if cat == "read":
        return f"Read {n} file{'s' if n != 1 else ''}"
    if cat == "edit":
        return f"Edited {n} file{'s' if n != 1 else ''}"
    if cat == "list":
        return f"Listed {n} dir{'s' if n != 1 else ''}"
    if cat == "glob":
        return f"Matched {n} glob{'s' if n != 1 else ''}"
    if cat == "search":
        return f"Searched {n} pattern{'s' if n != 1 else ''}"
    if cat == "run":
        return f"Ran {n} command{'s' if n != 1 else ''}"
    if cat == "task":
        return f"Launched {n} subagent{'s' if n != 1 else ''}"
    return f"{n} tools"


def item_label(name: str, args: Any = None) -> str:
    """Single-line Cursor label, e.g. ``Read README.md`` / ``Run pytest``."""
    n = (name or "").lower()

    # Todos: prefer checklist state over generic intent.
    if is_todo_tool(n):
        summary = summarize_todos(extract_todos(args))
        if summary:
            return summary
        intent = extract_intent(args)
        if intent:
            return intent
        return "Updated todos"

    intent = extract_intent(args)
    if intent:
        return intent

    cat = tool_category(name)
    path = extract_path(args)
    base = _basename(path)

    if cat == "task" and isinstance(args, dict):
        desc = args.get("description") or args.get("prompt")
        if isinstance(desc, str) and desc.strip():
            s = " ".join(desc.split()).strip()
            return s if len(s) <= 96 else s[:95].rstrip() + "…"

    if cat == "read":
        return f"Read {base}" if base else "Read file"
    if cat == "edit":
        return f"Edited {base}" if base else "Edited file"
    if cat == "list":
        target = base or (path or "dir")
        return f"Listed {target}"
    if cat == "glob":
        return f"Matched {base or path or 'glob'}"
    if cat == "search":
        pat = extract_pattern(args)
        return f"Searched {pat}" if pat else "Searched pattern"
    if cat == "run":
        cmd = extract_command(args)
        return f"Run {cmd}" if cmd else f"Run {name}"
    if cat == "task":
        return "Launched subagent"
    return name or "tool"


def summarize_categories(
    names: list[str],
    *,
    running: bool = False,
) -> str:
    """Group header: ``Read 22 files, Searched 1 pattern``."""
    order: OrderedDict[str, int] = OrderedDict()
    for name in names:
        cat = tool_category(name)
        order[cat] = order.get(cat, 0) + 1

    parts: list[str] = []
    others: list[str] = []
    for cat, n in order.items():
        if cat == "other":
            continue
        parts.append(category_phrase(cat, n))
    for name in names:
        if tool_category(name) == "other" and name not in others:
            others.append(name)
    if others:
        shown = ", ".join(others[:3])
        if len(others) > 3:
            shown += "…"
        parts.append(shown)

    body = ", ".join(parts) if parts else f"{len(names)} tools"
    return f"Running {body}" if running else body


def summarize_items(items: list[ToolItem], *, running: bool = False) -> str:
    return summarize_categories([it.name for it in items], running=running)


_WS = re.compile(r"\s+")


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        return bytes(content).decode("utf-8", errors="replace")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text" and part.get("text"):
                    parts.append(str(part["text"]))
                elif "text" in part:
                    parts.append(str(part["text"]))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def is_error_status(status: str, content: str = "") -> bool:
    s = (status or "").lower()
    c = (content or "").lower()
    if s.startswith("error"):
        return True
    if c.startswith("error") or "traceback" in c or "not supported" in c:
        return True
    return False


def truncate_preview(
    content: Any,
    *,
    max_chars: int = DEFAULT_PREVIEW_CHARS,
    max_lines: int = DEFAULT_PREVIEW_LINES,
) -> str | None:
    """Truncate tool result body for UI preview. None if empty."""
    text = content_to_text(content)
    if not text or not text.strip():
        return None
    # Drop NULs / control noise lightly.
    text = text.replace("\x00", "")
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[: max_chars - 1].rstrip() + "…"
        truncated = True
    elif truncated:
        body = body.rstrip() + "\n…"
    return body


def format_preview_with_lines(preview: str, *, max_lines: int = 12) -> str:
    """Render a small line-numbered preview block."""
    lines = preview.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines[:max_lines], start=1):
        out.append(f"{i:>3}  {line}")
    if len(lines) > max_lines:
        out.append("  …")
    return "\n".join(out)


def build_tool_item(
    call: Any,
    *,
    item_id: str,
    index: int = 0,
    sub: bool = False,
) -> ToolItem:
    """Build a ToolItem from a LangChain-style tool call object/dict."""
    if isinstance(call, dict):
        name = str(call.get("name") or "?")
        args = call.get("args")
    else:
        name = str(getattr(call, "name", "?") or "?")
        args = getattr(call, "args", {}) or {}

    cat = tool_category(name)
    path = extract_path(args)
    label = item_label(name, args)
    preview = None
    if is_todo_tool(name):
        preview = format_todos_preview(extract_todos(args))
    return ToolItem(
        id=item_id or f"{name}-{index}",
        name=name,
        category=cat,
        label=label,
        path=path,
        status="running",
        preview=preview,
        error=False,
        sub=sub,
    )


def match_tool_result(
    items: list[ToolItem],
    name: str,
) -> ToolItem | None:
    """Pick the first unfinished item with matching tool name.

    Do **not** fall back to an arbitrary running item.  Nested subagent tool
    results (``read_file`` while the parent only has ``task`` pending) would
    otherwise finish the parent task early and leave a trail of empty groups.
    """
    want = (name or "").strip()
    for it in items:
        if it.status != "running":
            continue
        if it.name == want:
            return it
    # Only tolerate a missing/empty tool name when exactly one item is running.
    if not want:
        running = [it for it in items if it.status == "running"]
        if len(running) == 1:
            return running[0]
    return None
