"""Model profile add/edit form dialog.

A compact field form (ModalScreen with Input/Select) used by
ModelManagerDialog for both creating and editing profiles. Ctrl+S saves,
Esc cancels. The dialog never writes files itself — it returns
``("add", alias, payload)`` / ``("edit", alias, changes)`` and the manager
persists through ``synapse.models.persist`` so hot-reload handling stays in
one place.
"""

from __future__ import annotations

import json
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Select, Static

from synapse.models.providers import PROVIDER_ORDER, PROVIDERS

_REASONING_OPTIONS = [
    ("inherit / off", ""),
    ("off", "off"),
    ("minimal", "minimal"),
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("max", "max"),
]
_IMAGE_OPTIONS = [
    ("auto", ""),
    ("yes", "true"),
    ("no", "false"),
]

_FORM_CSS = """
ModelFormDialog {
    align: center middle;
    background: transparent;
}
ModelFormDialog > #form-window {
    width: 72;
    height: auto;
    max-height: 30;
    background: $theme-bg;
    border: round $theme-user;
    border-title-color: $theme-fg;
    border-title-background: $theme-top;
    border-title-style: bold;
    border-title-align: left;
}
#form-fields {
    height: auto;
    padding: 0 1;
    overflow-y: auto;
}
.form-section {
    height: 1;
    padding: 1 1 0 1;
    color: $theme-user;
    text-style: bold;
}
.form-row {
    height: 3;
    align: center middle;
}
.form-label {
    width: 18;
    color: $theme-dim;
}
.form-control {
    width: 1fr;
}
#advanced {
    display: none;
}
#form-actions {
    height: 1;
    padding: 0 2;
    color: $theme-dim;
    background: $theme-top;
}
#form-error {
    height: 1;
    padding: 0 2;
    color: $theme-error;
    background: $theme-top;
    display: none;
}
"""

_MODEL_HINTS = {
    "openai": "e.g. gpt-4.1",
    "anthropic": "e.g. claude-sonnet-4-5",
    "openai_oauth": "e.g. gpt-5.2-codex",
}


class ModelFormDialog(ModalScreen[object]):
    """Create (``alias`` editable) or edit (``alias`` fixed) a model profile."""

    DEFAULT_CSS = _FORM_CSS

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("x", "toggle_advanced", "Advanced", show=False),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        settings: Any,
        *,
        alias: str | None = None,
        initial: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._alias = alias  # None => create mode; otherwise edit mode
        self._initial = dict(initial or {})

    @property
    def _editing(self) -> bool:
        return self._alias is not None

    def compose(self) -> ComposeResult:
        options = [
            (PROVIDERS[key].label, key)
            for key in PROVIDER_ORDER
            if key in PROVIDERS
        ]
        current_provider = str(self._initial.get("provider") or "openai")
        if current_provider not in PROVIDERS:
            current_provider = "openai"
        with Vertical(id="form-window"):
            with VerticalScroll(id="form-fields"):
                yield Static("Connection", classes="form-section")
                if not self._editing:
                    yield from self._row("Alias", Input(id="f-alias", placeholder="my-model"))
                yield from self._row(
                    "Provider",
                    Select(
                        options,
                        value=current_provider,
                        allow_blank=False,
                        id="f-provider",
                        compact=True,
                    ),
                )
                yield from self._row(
                    "Model ID",
                    Input(id="f-model", placeholder=_MODEL_HINTS[current_provider]),
                )
                yield from self._row(
                    "API key",
                    Input(id="f-key", placeholder="sk-..."),
                )
                yield from self._row(
                    "Base URL",
                    Input(id="f-base-url", placeholder="https://api.openai.com/v1 (default)"),
                )
                yield Static("Options", classes="form-section")
                yield from self._row(
                    "Thinking",
                    Select(
                        _REASONING_OPTIONS,
                        value=str(self._initial.get("reasoning_effort") or ""),
                        allow_blank=True,
                        id="f-thinking",
                        compact=True,
                    ),
                )
                yield from self._row(
                    "Context window",
                    Input(id="f-context", placeholder="e.g. 200000"),
                )
                yield from self._row(
                    "Image input",
                    Select(
                        _IMAGE_OPTIONS,
                        value=_bool_or_auto(self._initial.get("image_input")),
                        allow_blank=True,
                        id="f-image",
                        compact=True,
                    ),
                )
                with Vertical(id="advanced"):
                    yield from self._row(
                        "Headers JSON",
                        Input(id="f-headers", placeholder='{"X-Tenant": "t1"}'),
                    )
                    yield from self._row(
                        "model_kwargs JSON",
                        Input(id="f-model-kwargs", placeholder='{"default_query": {"p": "1"}}'),
                    )
                    yield from self._row(
                        "extra_body JSON",
                        Input(id="f-extra-body", placeholder='{"thinking": {"type": "enabled"}}'),
                    )
            yield Static("", id="form-error")
            yield Static(
                "x advanced · Ctrl+S save · Esc cancel",
                id="form-actions",
            )

    @staticmethod
    def _row(label: str, widget: Any) -> ComposeResult:
        with Horizontal(classes="form-row"):
            yield Label(label, classes="form-label")
            yield widget  # type: ignore[misc]

    def on_mount(self) -> None:
        window = self.query_one("#form-window")
        window.border_title = (
            f"◈ Edit model  {self._alias}" if self._editing else "◈ Add model"
        )
        if not self._editing:
            self.query_one("#f-alias", Input).focus()
        else:
            self.query_one("#f-model", Input).focus()
        self._prefill()
        self._sync_placeholders()
        if any(
            self._initial.get(key)
            for key in ("headers", "model_kwargs", "extra_body")
        ):
            self.action_toggle_advanced()

    def _prefill(self) -> None:
        initial = self._initial
        if self._editing:
            model_id = str(initial.get("model") or "").split(":", 1)[-1]
            self.query_one("#f-model", Input).value = model_id
            self.query_one("#f-key", Input).value = str(
            initial.get("api_key") or initial.get("api_key_env") or ""
        )
            self.query_one("#f-base-url", Input).value = str(initial.get("base_url") or "")
            self.query_one("#f-context", Input).value = str(initial.get("context_window") or "")
            for field, target in (
                ("headers", "#f-headers"),
                ("model_kwargs", "#f-model-kwargs"),
                ("extra_body", "#f-extra-body"),
            ):
                value = initial.get(field)
                if value:
                    try:
                        self.query_one(target, Input).value = json.dumps(
                            value, ensure_ascii=False
                        )
                    except (TypeError, ValueError):
                        pass
            self._prefill_provider(initial)

    def _prefill_provider(self, initial: dict[str, Any]) -> None:
        proto = initial.get("model") or ""
        if ":" in proto:
            provider = str(proto).split(":", 1)[0]
            if provider in PROVIDERS and provider != "openai":
                try:
                    self.query_one("#f-provider", Select).value = provider
                except Exception:  # noqa: BLE001
                    pass

    def _sync_placeholders(self) -> None:
        """Show provider defaults as placeholders without overwriting input."""
        try:
            provider = str(self.query_one("#f-provider", Select).value or "openai")
        except Exception:  # noqa: BLE001
            provider = "openai"
        spec = PROVIDERS.get(provider)
        if spec is None:
            return
        model = self.query_one("#f-model", Input)
        if not model.value:
            model.placeholder = _MODEL_HINTS.get(provider, "e.g. gpt-4.1")
        base = self.query_one("#f-base-url", Input)
        if not base.value:
            base.placeholder = f"{spec.default_base_url} (default)"

    def on_select_changed(self, event: Any) -> None:
        try:
            if event.select is self.query_one("#f-provider", Select):
                self._sync_placeholders()
        except Exception:  # noqa: BLE001
            pass

    def action_toggle_advanced(self) -> None:
        advanced = self.query_one("#advanced")
        advanced.styles.display = "none" if advanced.styles.display != "none" else "block"

    def _set_error(self, message: str) -> None:
        error = self.query_one("#form-error", Static)
        if message:
            error.update(message)
            error.styles.display = "block"
        else:
            error.update("")
            error.styles.display = "none"

    def _read_json_field(self, field_id: str) -> dict[str, Any] | None:
        raw = (self.query_one(field_id, Input).value or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_id}: invalid JSON ({exc})") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{field_id}: must be a JSON object")
        return parsed

    def _read_provider(self) -> str:
        try:
            provider = str(self.query_one("#f-provider", Select).value or "openai").strip()
        except Exception:  # noqa: BLE001
            provider = "openai"
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider: {provider}")
        return provider

    def action_save(self) -> None:
        try:
            self._do_save()
        except ValueError as exc:
            self._set_error(str(exc))

    def _do_save(self) -> None:
        model_input = (self.query_one("#f-model", Input).value or "").strip()
        if not model_input:
            raise ValueError("model ID is required")

        alias = self._alias
        if not self._editing:
            alias = (self.query_one("#f-alias", Input).value or "").strip()
            if not alias:
                raise ValueError("alias is required")

        # An explicit provider prefix in the model id wins over the dropdown.
        provider = self._read_provider()
        if ":" in model_input:
            prefix = model_input.split(":", 1)[0].strip().casefold()
            if prefix in PROVIDERS:
                provider = prefix

        model = model_input if ":" in model_input else f"{provider}:{model_input}"

        headers = self._read_json_field("#f-headers")
        model_kwargs = self._read_json_field("#f-model-kwargs")
        extra_body = self._read_json_field("#f-extra-body")

        context_raw = (self.query_one("#f-context", Input).value or "").strip()
        context_window: int | None = None
        if context_raw:
            try:
                context_window = int(context_raw)
            except ValueError as exc:
                raise ValueError(
                    f"context window must be an integer: {context_raw}"
                ) from exc
            if context_window <= 0:
                raise ValueError("context window must be positive")

        base_url = (self.query_one("#f-base-url", Input).value or "").strip() or None
        api_key = (self.query_one("#f-key", Input).value or "").strip() or None
        thinking = str(self.query_one("#f-thinking", Select).value or "").strip() or None
        image_raw = str(self.query_one("#f-image", Select).value or "").strip() or None

        values: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "reasoning_effort": thinking,
            "image_input": image_raw,
            "context_window": context_window,
            "headers": headers,
            "model_kwargs": model_kwargs,
            "extra_body": extra_body,
        }

        if self._editing:
            # ``None`` means "clear this field" for update_profile.
            self.dismiss(("edit", alias, values))
        else:
            payload = {k: v for k, v in values.items() if v not in (None, "", {}, [])}
            self.dismiss(("add", alias, payload))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _bool_or_auto(value: Any) -> str:
    if value is None or value == "":
        return ""
    return "true" if str(value).casefold() in {"1", "true", "yes"} else "false"