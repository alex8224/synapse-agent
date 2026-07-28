"""Compression decision diagnostics for the active session."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.widgets import Static

from synapse.tool_output import ToolOutputRepository
from synapse.ui.dialogs.base import DialogBase, OptionItem, SectionHeader


def _fmt_tokens(value: Any) -> str:
    amount = max(0, int(value or 0))
    if amount >= 1_000_000:
        return f"~{amount / 1_000_000:.1f}M tok"
    if amount >= 1_000:
        return f"~{amount / 1_000:.1f}K tok"
    return f"~{amount} tok"


def _fmt_bytes(value: Any) -> str:
    amount = max(0, int(value or 0))
    if amount >= 1024**2:
        return f"{amount / 1024**2:.1f} MiB"
    if amount >= 1024:
        return f"{amount / 1024:.1f} KiB"
    return f"{amount} B"


class CompressionDiagnosticsDialog(DialogBase):
    """Show aggregate compression outcomes and recent per-tool decisions."""

    _title_icon = "◇"
    _title_keys = "↑↓ · esc"

    def __init__(
        self,
        repository: ToolOutputRepository,
        thread_id: str,
        *,
        limit: int = 30,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._thread_id = thread_id
        self._limit = max(1, min(100, int(limit)))
        self._stats = repository.stats(thread_id=thread_id)
        self._events = repository.events(thread_id=thread_id, limit=self._limit)
        self._request_events = repository.model_request_events(
            thread_id=thread_id, limit=self._limit
        )
        self._items = self._build_items()

    @property
    def title_text(self) -> str:
        return "Compression Diagnostics"

    def _build_items(self) -> list[OptionItem]:
        items: list[OptionItem] = []
        reasons = self._stats.get("reasons") or {}
        tokens_by_reason = self._stats.get("tokens_by_reason") or {}
        for reason, count in sorted(
            reasons.items(),
            key=lambda item: int(tokens_by_reason.get(item[0], 0) or 0),
            reverse=True,
        ):
            items.append(
                OptionItem(
                    key=f"reason:{reason}",
                    label=str(reason),
                    detail=f"{count} event(s)",
                    meta=_fmt_tokens(tokens_by_reason.get(reason, 0)),
                    show_bullet=False,
                )
            )
        for request in self._request_events:
            before = _fmt_tokens(request.get("input_tokens_before", 0))
            after = _fmt_tokens(request.get("input_tokens_after", 0))
            provider = str(request.get("provider") or "unknown")
            api_style = str(request.get("api_style") or "unknown")
            request_id = str(request.get("request_id") or "-")
            protected = request.get("protected_tokens_by_reason") or {}
            items.append(
                OptionItem(
                    key=f"request:{request_id}",
                    label=f"model request · {provider}/{api_style}",
                    detail=f"{request_id} · {before} → {after} · protected {protected}",
                    meta=_fmt_tokens(request.get("total_saved_tokens", 0)),
                    show_bullet=False,
                )
            )
        for event in self._events:
            decision = str(
                event.get("decision")
                or ("transformed" if event.get("outcome") == "transformed" else "fallback")
            )
            reason = str(event.get("reason_code") or "legacy_passthrough")
            tool = str(event.get("tool_name") or "tool")
            call_id = str(event.get("tool_call_id") or "-")
            content_type = str(event.get("content_type") or "unknown")
            transformer = str(event.get("transformer") or "none")
            original = _fmt_bytes(event.get("original_bytes", 0))
            visible = _fmt_bytes(event.get("visible_bytes", 0))
            items.append(
                OptionItem(
                    key=f"event:{event.get('id', call_id)}",
                    label=f"{tool} · {decision} · {reason}",
                    detail=(
                        f"{call_id} · {content_type} · {transformer} · "
                        f"{original} → {visible}"
                    ),
                    meta=_fmt_tokens(event.get("estimated_saved_tokens", 0)),
                    show_bullet=False,
                )
            )
        if not items:
            items.append(
                OptionItem(
                    key="empty",
                    label="No compression decisions recorded",
                    show_bullet=False,
                )
            )
        return items

    def compose_body(self) -> ComposeResult:
        stats = self._stats
        yield SectionHeader("Session summary")
        yield Static(
            f"  {stats.get('outputs_considered', 0)} candidates · "
            f"{stats.get('transformed', 0)} transformed · "
            f"{stats.get('skipped', 0)} skipped · "
            f"{stats.get('fallback', 0)} fallback\n"
            f"  {_fmt_bytes(stats.get('original_bytes', 0))} original → "
            f"{_fmt_bytes(stats.get('visible_bytes', 0))} model-visible\n"
            f"  saved {_fmt_tokens(stats.get('estimated_saved_tokens', 0))} · "
            f"reused {_fmt_tokens(stats.get('estimated_reused_tokens', 0))}\n"
            f"  {stats.get('model_requests', 0)} model requests · "
            f"whole {stats.get('whole_request_savings_ratio', 0.0):.1%} · "
            f"new input {stats.get('new_input_savings_ratio', 0.0):.1%}"
        )
        yield SectionHeader("Reasons and recent decisions")

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        body.set_options(self._items, mark="  ")

    def _on_selected(self, key: str | None) -> None:
        self.dismiss(None)
