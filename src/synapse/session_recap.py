"""Session recap (away summary) — one-line progress when idle.

Inspired by Claude Code Session Recap: after a completed turn, if the
session sits idle long enough, surface a single-line summary so the user
can resume without scrolling. No slash command — auto-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_IDLE_SECONDS = 180.0
DEFAULT_MIN_TURNS = 3
MAX_LINE_CHARS = 120
MAX_FRAGMENT_CHARS = 48


def _one_line(text: str, *, limit: int = MAX_FRAGMENT_CHARS) -> str:
    body = " ".join((text or "").split())
    if not body:
        return ""
    if len(body) <= limit:
        return body
    return body[: max(0, limit - 1)] + "…"


def build_recap_line(
    *,
    user_text: str = "",
    tool_summary: str = "",
    answer_excerpt: str = "",
    max_chars: int = MAX_LINE_CHARS,
) -> str:
    """Compose a single dim UI line from the latest turn snapshot."""
    parts: list[str] = []
    user = _one_line(user_text)
    tools = _one_line(tool_summary, limit=40)
    answer = _one_line(answer_excerpt, limit=56)
    if user:
        parts.append(f"任务 {user}")
    if tools:
        parts.append(f"工具 {tools}")
    if answer:
        parts.append(f"进展 {answer}")
    if not parts:
        parts.append("会话空闲；可继续输入")
    body = "；".join(parts)
    line = f"recap: {body}"
    if len(line) > max_chars:
        line = line[: max(0, max_chars - 1)] + "…"
    return line


def snapshot_from_turn(
    *,
    user_text: str = "",
    tool_items: list[Any] | None = None,
    tool_summary: str = "",
    answer_text: str = "",
) -> dict[str, str]:
    """Normalize turn facts into a plain dict for recap generation."""
    summary = (tool_summary or "").strip()
    if not summary and tool_items:
        names: list[str] = []
        for it in tool_items:
            name = getattr(it, "name", None) or getattr(it, "label", None) or ""
            name = str(name).strip()
            if name and name not in names:
                names.append(name)
            if len(names) >= 4:
                break
        if names:
            summary = ", ".join(names)
    return {
        "user_text": (user_text or "").strip(),
        "tool_summary": summary,
        "answer_excerpt": (answer_text or "").strip(),
    }


@dataclass
class SessionRecapController:
    """Track eligibility for an automatic one-line recap after idle."""

    enabled: bool = True
    idle_seconds: float = DEFAULT_IDLE_SECONDS
    min_turns: int = DEFAULT_MIN_TURNS
    turn_count: int = 0
    last_turn_done_at: float | None = None
    last_snapshot: dict[str, str] = field(default_factory=dict)
    # After a recap is shown, require a new completed turn before next fire.
    needs_fresh_turn: bool = False

    def reset(self) -> None:
        self.turn_count = 0
        self.last_turn_done_at = None
        self.last_snapshot = {}
        self.needs_fresh_turn = False

    def note_turn_done(
        self,
        now: float,
        *,
        user_text: str = "",
        tool_summary: str = "",
        tool_items: list[Any] | None = None,
        answer_text: str = "",
        turn_count: int | None = None,
    ) -> None:
        """Record a completed agent turn as the recap source."""
        if turn_count is not None:
            self.turn_count = max(0, int(turn_count))
        else:
            self.turn_count += 1
        self.last_turn_done_at = float(now)
        self.last_snapshot = snapshot_from_turn(
            user_text=user_text,
            tool_items=tool_items,
            tool_summary=tool_summary,
            answer_text=answer_text,
        )
        self.needs_fresh_turn = False

    def eligible(
        self,
        now: float,
        *,
        busy: bool = False,
        draft_nonempty: bool = False,
    ) -> bool:
        """Whether an idle recap should fire now."""
        if not self.enabled:
            return False
        if busy or draft_nonempty:
            return False
        if self.needs_fresh_turn:
            return False
        if self.turn_count < max(1, int(self.min_turns)):
            return False
        if self.last_turn_done_at is None:
            return False
        idle_for = float(now) - float(self.last_turn_done_at)
        if idle_for < max(0.0, float(self.idle_seconds)):
            return False
        return True

    def try_fire(
        self,
        now: float,
        *,
        busy: bool = False,
        draft_nonempty: bool = False,
    ) -> str | None:
        """If eligible, return the recap line and mark as shown.

        Long idle must never re-generate: after a successful fire, arm
        ``needs_fresh_turn`` and clear the idle clock until a new turn.
        """
        if not self.eligible(
            now,
            busy=busy,
            draft_nonempty=draft_nonempty,
        ):
            return None
        line = build_recap_line(
            user_text=self.last_snapshot.get("user_text", ""),
            tool_summary=self.last_snapshot.get("tool_summary", ""),
            answer_excerpt=self.last_snapshot.get("answer_excerpt", ""),
        )
        # One-shot per completed turn. Further idle ticks stay silent.
        self.needs_fresh_turn = True
        self.last_turn_done_at = None
        return line
