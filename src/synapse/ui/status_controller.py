"""Status bar rendering: activity/notice, spinner, steer badge, prompt copy.

Owns the #status phase/notice state and rendering that used to live directly on
``CodingAgentApp``. The Textual host keeps the interval timer and forwards here.
"""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual.widgets import Input, Static

import synapse.ui.tui_styles as _styles
from synapse.ui.formatters import model_status_label
from synapse.ui.steer_widget import SteerQueueWidget
from synapse.ui.topbar import truncate_to_width
from synapse.ui.tui_styles import _MARK_INPUT, _SPINNER


class StatusController:
    """Activity/notice rendering for the #status bar and prompt copy."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._phase = "idle"
        self._detail = "ready"
        self._activity_started = time.monotonic()
        self._spin_i = 0
        self._steer_items: list[str] = []
        self._steer_last_count = 0
        self._status_notice: str = ""
        self._status_notice_style: str = "dim"
        self._status_notice_until: float = 0.0
        self._status_notice_timer = None

    # -- activity ----------------------------------------------------------

    def set_activity(self, phase: str, detail: str = "", reset_timer: bool = False) -> None:
        app = self._app
        detail = detail or ""
        if reset_timer or phase != self._phase:
            self._activity_started = time.monotonic()
        self._phase = phase or "idle"
        self._detail = detail
        busy = self._phase not in {"idle", "ready", ""}
        app.query_one("#status", Static).set_class(busy, "busy")
        if busy:
            app.sub_title = f"{model_status_label(app.settings)} · {self._phase}"
        else:
            app.sub_title = model_status_label(app.settings)
        self.render_status()

    # -- notice ------------------------------------------------------------

    def flash_status(
        self,
        message: str,
        style: str = "dim",
        *,
        ttl: float = 4.0,
    ) -> None:
        """Show a short notice in #status left activity slot (not transcript)."""
        msg = (message or "").strip()
        if not msg:
            return
        # Keep single-line chrome; collapse whitespace.
        msg = " ".join(msg.split())
        self._status_notice = msg
        self._status_notice_style = (style or "dim").strip() or "dim"
        self._status_notice_until = time.monotonic() + max(0.5, float(ttl or 0))
        if self._status_notice_timer is not None:
            try:
                self._status_notice_timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._status_notice_timer = None
        # Only schedule auto-clear when the app loop is actually running.
        if bool(getattr(self._app, "is_running", False)):
            try:
                self._status_notice_timer = self._app.set_timer(
                    max(0.5, float(ttl or 0)), self.clear_status_notice
                )
            except Exception:  # noqa: BLE001
                self._status_notice_timer = None
        try:
            self.render_status()
        except Exception:  # noqa: BLE001
            pass

    def clear_status_notice(self) -> None:
        self._status_notice_timer = None
        self._status_notice = ""
        self._status_notice_style = "dim"
        self._status_notice_until = 0.0
        try:
            self.render_status()
        except Exception:  # noqa: BLE001
            pass

    def _active_status_notice(self) -> str:
        msg = (self._status_notice or "").strip()
        if not msg:
            return ""
        if time.monotonic() >= float(self._status_notice_until or 0):
            self._status_notice = ""
            self._status_notice_until = 0.0
            return ""
        return msg

    def _status_notice_style_token(self) -> str:
        """Map notice style name to a palette color for the left activity slot."""
        key = (self._status_notice_style or "dim").lower()
        if "red" in key or "error" in key:
            return _styles._C_ERROR
        if "yellow" in key or "warn" in key or "orange" in key:
            return _styles._C_ORANGE
        return _styles._C_DIM

    # -- render ------------------------------------------------------------

    def _compose_status_left(
        self,
        *,
        busy: bool,
        elapsed: float,
        steer_n: int,
        left_budget: int,
    ) -> tuple[str, str]:
        """Build activity/notice text for #status (full width).

        Layout target (above the prompt)::

            [ left activity / notice ]

        Model · thinking · mcp moved to the bottombar under the prompt.
        """
        notice = self._active_status_notice()
        if notice:
            # Prefer the ephemeral confirm while it is live, even mid-run.
            return (
                truncate_to_width(notice, left_budget),
                self._status_notice_style_token(),
            )
        if not busy:
            return "", _styles._C_MUTED
        spin = _SPINNER[self._spin_i % len(_SPINNER)]
        detail = f" {self._detail}" if self._detail else ""
        steer_badge = f" · queue×{steer_n}" if steer_n else ""
        left = f"{spin} {self._phase}{detail}{steer_badge} · {elapsed:.1f}s"
        return truncate_to_width(left, left_budget), _styles._C_ORANGE

    def render_status(self) -> None:
        """Paint #status activity/notice only; model/mcp live on #bottombar."""
        app = self._app
        elapsed = max(0.0, time.monotonic() - self._activity_started)
        busy = self._phase not in {"idle", "ready", ""}
        status = app.query_one("#status", Static)
        width = max(int(getattr(app.size, "width", 0) or 0), 48)
        # Account for CSS padding (0 2).
        usable = max(16, width - 4)
        steer_n = len(self._steer_items)
        left, left_style = self._compose_status_left(
            busy=busy,
            elapsed=elapsed,
            steer_n=steer_n,
            left_budget=usable,
        )
        if not left:
            status.update("")
        else:
            status.update(Text(left, style=left_style))
        app._refresh_bottombar()

    def tick(self) -> bool:
        """Advance the spinner/notice clock; True while a phase is active."""
        if self._phase not in {"idle", "ready", ""}:
            self._spin_i += 1
            self.render_status()
            return True
        # Drop expired status notices without waiting for the timer edge case.
        if self._status_notice and time.monotonic() >= float(
            self._status_notice_until or 0
        ):
            self.clear_status_notice()
        elif self._status_notice:
            self.render_status()
        return False

    # -- steer badge / prompt copy ----------------------------------------

    def sync_prompt_placeholder(self) -> None:
        """Prompt copy guides mode: normal vs mid-run queue."""
        try:
            prompt = self._app.query_one("#prompt", Input)
        except Exception:  # noqa: BLE001
            return
        if self._app._busy:
            n = len(self._steer_items)
            if n:
                prompt.placeholder = f"{_MARK_INPUT}  Add guidance ({n} queued)…"
            else:
                prompt.placeholder = (
                    f"{_MARK_INPUT}  Enter guidance, takes effect next turn…"
                )
        else:
            prompt.placeholder = (
                f"{_MARK_INPUT}  Build anything  (/ for commands, Tab complete)"
            )

    def on_steer_items_changed(self, items: list[str]) -> None:
        from synapse.goals.steering import GOAL_STEER_PREFIX

        self._steer_items = [
            str(item).strip()
            for item in items
            if str(item).strip()
            and not str(item).strip().startswith(GOAL_STEER_PREFIX)
        ]
        self._steer_last_count = len(self._steer_items)
        try:
            self._app.query_one("#steer-queue", SteerQueueWidget).set_items(
                self._steer_items
            )
        except Exception:  # noqa: BLE001
            pass
        self.render_status()
        self.sync_prompt_placeholder()
