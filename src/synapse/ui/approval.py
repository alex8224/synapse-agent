"""Interactive HITL approval block rendered in the transcript timeline.

When a tool call is paused for human approval (LangGraph interrupt), the TUI
mounts one :class:`ApprovalBlock` instead of dumping the raw interrupt JSON
into the transcript. The block shows a friendly summary of every pending
action and offers two clickable / keyboard-selectable decisions:

- **通过 (approve)**: resume the graph allowing the tool call(s).
- **拒绝 (reject)**: resume the graph refusing the tool call(s).

Decisions are dispatched through an ``on_decide(action, message)`` callback.
The callback returns True when the resume was accepted; a False return
re-enables the buttons so a click that raced the session settling into
``WAITING_APPROVAL`` can be retried instead of leaving a dead widget.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, Static

from synapse.runtime.hitl import PendingAction, PendingInterrupt

_DEFAULT_DIM = "#9aa0a6"
_DEFAULT_FG = "#e8eaed"
_DEFAULT_ORANGE = "#f4b183"
_DEFAULT_MUTED = "#5f6368"
_DEFAULT_BAR = "#2b2d31"

#: Cap for the JSON-formatted argument preview per action.
_MAX_ARGS_CHARS = 200
#: Cap for the description line.
_MAX_DESC_CHARS = 240


def _theme_color(attribute: str, fallback: str) -> str:
    try:
        from synapse.ui.theme import get_theme

        return str(getattr(get_theme(), attribute, fallback))
    except Exception:  # noqa: BLE001
        return fallback


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _action_args_preview(args: dict[str, Any] | None) -> str:
    """JSON-pretty one action's arguments, bounded and collapsed."""
    if not args:
        return "{}"
    try:
        rendered = json.dumps(args, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        rendered = str(args)
    if len(rendered) > _MAX_ARGS_CHARS:
        rendered = f"{rendered[: _MAX_ARGS_CHARS - 1]}…"
    return rendered


def _action_args_compact(args: dict[str, Any] | None) -> str:
    """One-line bounded args preview for the action row."""
    if not args:
        return "{}"
    try:
        rendered = json.dumps(args, ensure_ascii=False, separators=(",", ": "))
    except (TypeError, ValueError):
        rendered = str(args)
    return _clip(rendered, 120)


class ApprovalBlock(Container):
    """One friendly approval request with 通过 / 拒绝 decisions."""

    def __init__(
        self,
        pending: PendingInterrupt,
        *,
        on_decide: Callable[[str, str | None], bool],
    ) -> None:
        super().__init__()
        self._pending = pending
        self._on_decide_cb = on_decide
        self._resolved = False
        self._decision: str | None = None
        self._result_line: Static | None = None

    def _allowed_decisions(self) -> set[str]:
        """Intersection of every action's allowed decisions (fallback both)."""
        allowed: set[str] | None = None
        for act in self._pending.actions or []:
            kinds = set(getattr(act, "allowed_decisions", None) or ["approve", "reject"])
            allowed = kinds if allowed is None else (allowed & kinds)
        return allowed if allowed else {"approve", "reject"}

    def compose(self) -> ComposeResult:
        dim = _theme_color("dim", _DEFAULT_DIM)
        fg = _theme_color("fg", _DEFAULT_FG)
        bar = _theme_color("bar", _DEFAULT_BAR)
        muted = _theme_color("muted", _DEFAULT_MUTED)
        orange = _theme_color("orange", _DEFAULT_ORANGE)

        actions = list(self._pending.actions or [])
        header = Text(
            f" 需要审批 — {len(actions)} 个工具调用" if actions else " 需要审批",
            style=f"{fg} on {bar}",
        )
        yield Static(header)
        if not actions:
            yield Static(Text("    (无法解析的审批请求，请检查 agent 状态)", style=muted))
        for act in actions:
            yield Static(self._action_row(act, indent="    ", muted=muted, orange=orange))
            args_preview = _action_args_preview(act.args)
            yield Static(Text(f"      参数: {args_preview}", style=dim))
            if act.description:
                desc = _clip(act.description, _MAX_DESC_CHARS)
                yield Static(Text(f"      说明: {desc}", style=dim))
        hint = Text("    ", style=muted)
        hint.append("Tab/方向键选择，Enter 确认，或直接点击", style=dim)
        yield Static(hint)
        allowed = self._allowed_decisions()
        with Horizontal(classes="approval-actions"):
            if "approve" in allowed:
                yield Button("✓ 通过", id="approval-approve", variant="success")
            if "reject" in allowed:
                yield Button("✗ 拒绝", id="approval-reject", variant="error")

    @staticmethod
    def _action_row(act: PendingAction, *, indent: str, muted: str, orange: str) -> Text:
        name = act.name or "?"
        args = _action_args_compact(act.args)
        row = Text(indent, style=muted)
        row.append(f"{name}  ", style=orange)
        row.append(args, style=muted)
        return row

    @on(Button.Pressed, "#approval-approve")
    def _on_approve(self) -> None:
        self._decide("approve")

    @on(Button.Pressed, "#approval-reject")
    def _on_reject(self) -> None:
        self._decide("reject")

    def _decide(self, action: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        self._decision = action
        self._set_buttons_disabled(True)
        try:
            self._append_result_line("已通过" if action == "approve" else "已拒绝")
        except Exception:  # noqa: BLE001 - cosmetic
            pass
        accepted = bool(self._on_decide_cb(action, None))
        if not accepted:
            # The resume was refused (session still settling). Re-enable the
            # buttons so the user can retry once WAITING_APPROVAL is reached.
            self._resolved = False
            self._decision = None
            self._set_buttons_disabled(False)
            try:
                self._remove_result_line()
            except Exception:  # noqa: BLE001 - cosmetic
                pass

    def _set_buttons_disabled(self, disabled: bool) -> None:
        for button_id in ("#approval-approve", "#approval-reject"):
            try:
                button = self.query_one(button_id, Button)
            except Exception:  # noqa: BLE001 - button may be absent
                continue
            button.disabled = disabled

    def _append_result_line(self, label: str) -> None:
        fg = _theme_color("fg", _DEFAULT_FG)
        bar = _theme_color("bar", _DEFAULT_BAR)
        line = Static(Text(f" {label} — 正在恢复任务…", style=f"{fg} on {bar}"))
        self.mount(line)
        self._result_line = line

    def _remove_result_line(self) -> None:
        line = getattr(self, "_result_line", None)
        if line is not None:
            if getattr(line, "is_attached", False):
                line.remove()
            self._result_line = None

    @property
    def decision(self) -> str | None:
        return self._decision
