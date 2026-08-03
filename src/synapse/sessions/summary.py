"""Deterministic local session summaries (no model call).

The ``sessions.summary`` column exists but had no writer; this module fills
that gap with a token-free digest built from per-turn facts (user task, tool
usage, answer excerpt). Each turn appends one bounded entry; the digest keeps
the most recent entries up to a character budget.

Format (one entry per line)::

    - 任务 <task>；工具 <tools>；进展 <answer excerpt>

Merge rules:

- Entries are trimmed from the oldest end when the total exceeds
  ``max_chars`` or ``max_entries``;
- A turn whose task is identical to the latest entry updates that entry
  instead of appending (avoids noise from follow-up steering turns);
- Sensitive content never enters summaries: tool output is summarized by
  tool-name only and the answer excerpt is a short head-only snippet.
"""

from __future__ import annotations

import re
from typing import Any

_ENTRY_PREFIX = "- "
_MAX_TASK_CHARS = 80
_MAX_TOOLS_CHARS = 60
_MAX_ANSWER_CHARS = 100
_MAX_ENTRIES = 12

_WHITESPACE = re.compile(r"\s+")


def _squash(text: str | None) -> str:
    if not text:
        return ""
    return _WHITESPACE.sub(" ", text).strip()


def _excerpt(text: str | None, max_len: int) -> str:
    one = _squash(text)
    if not one:
        return ""
    if len(one) <= max_len:
        return one
    return one[: max_len - 1].rstrip() + "…"


def build_turn_entry(
    *,
    user_text: str | None = None,
    tool_summary: str | None = None,
    answer_excerpt: str | None = None,
    max_task_chars: int = _MAX_TASK_CHARS,
) -> str:
    """One digest line for a completed turn."""
    parts: list[str] = []
    task = _excerpt(user_text, max_task_chars)
    if task:
        parts.append(f"任务 {task}")
    tools = _excerpt(tool_summary, _MAX_TOOLS_CHARS)
    if tools:
        parts.append(f"工具 {tools}")
    progress = _excerpt(answer_excerpt, _MAX_ANSWER_CHARS)
    if progress:
        parts.append(f"进展 {progress}")
    if not parts:
        return ""
    return _ENTRY_PREFIX + "；".join(parts)


def parse_entries(summary: str | None) -> list[str]:
    """Split a stored summary back into entry lines (oldest first)."""
    if not summary:
        return []
    out: list[str] = []
    for line in str(summary).splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(line if line.startswith(_ENTRY_PREFIX) else _ENTRY_PREFIX + line)
    return out


def _join(entries: list[str], max_chars: int) -> str:
    total = "\n".join(entries)
    if len(total) <= max_chars or len(entries) <= 1:
        return total
    # Drop oldest entries until the digest fits the budget.
    kept = entries[-1:]
    for entry in reversed(entries[:-1]):
        candidate = entry + "\n" + "\n".join(kept)
        if len(candidate) > max_chars:
            break
        kept.insert(0, entry)
    return "\n".join(kept)


def merge_turn_summary(
    existing: str | None,
    *,
    user_text: str | None = None,
    tool_summary: str | None = None,
    answer_excerpt: str | None = None,
    max_chars: int = 600,
    max_entries: int = _MAX_ENTRIES,
) -> str:
    """Merge one turn into an existing digest, oldest-first bounded."""
    max_chars = max(80, int(max_chars))
    max_entries = max(1, min(int(max_entries), 50))
    entry = build_turn_entry(user_text=user_text, tool_summary=tool_summary,
                             answer_excerpt=answer_excerpt)
    if not entry:
        return existing or ""
    entries = parse_entries(existing)
    # Update the latest entry when the task repeats (e.g. steer follow-ups).
    if entries and entries[-1] == entry:
        return "\n".join(entries)
    entries.append(entry)
    entries = entries[-max_entries:]
    return _join(entries, max_chars)


def persist_local_summary(
    store: Any,
    thread_id: str,
    *,
    user_text: str | None = None,
    tool_summary: str | None = None,
    answer_text: str | None = None,
    max_chars: int = 600,
) -> str | None:
    """Merge the latest turn into the stored summary and persist it.

    Returns the new summary (or ``None`` when nothing changed). Uses the
    existing row's summary as the merge base; callers may pass a base via
    ``store.get(thread_id).summary`` transparently.
    """
    info = store.get(thread_id)
    base = info.summary if info is not None else None
    merged = merge_turn_summary(
        base,
        user_text=user_text,
        tool_summary=tool_summary,
        answer_excerpt=answer_text,
        max_chars=max_chars,
    )
    if merged == base:
        return None
    if merged and store.set_summary(thread_id, merged):
        return merged
    return None
