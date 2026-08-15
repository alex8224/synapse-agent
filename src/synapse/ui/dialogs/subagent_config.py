"""TUI dialogs for configuring subagent models and reasoning effort."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding

from synapse.runtime.subagent_specs import REASONING_EFFORT_LEVELS
from synapse.ui.dialogs.base import DialogBase, DialogBody, OptionItem

THINKING_LEVELS = REASONING_EFFORT_LEVELS
_INHERIT_REASONING_KEY = "reasoning:inherit"
_GLOBAL_KEY = "__global__"
_INHERIT_MODEL_KEY = "model:inherit"


def _inherit_to_none(value: str | None) -> str | None:
    """Normalize ``"inherit"`` to ``None`` (both mean "this layer unset")."""
    return None if value == "inherit" else value


def _override_model(config: dict[str, Any], name: str) -> str | None:
    return (config.get("model_overrides") or {}).get(name)


def _override_reasoning(config: dict[str, Any], name: str) -> str | None:
    return (config.get("reasoning_effort_overrides") or {}).get(name)


def _config_value(config: dict[str, Any], key: str, name: str) -> str | None:
    if key == "model":
        return _override_model(config, name) or config.get("default_model")
    return _override_reasoning(config, name) or config.get(
        "default_reasoning_effort"
    )


def _set_config_value(config: dict[str, Any], key: str, name: str, value: str | None) -> None:
    """Write a config value, routing the global key to the default fields."""
    if name == _GLOBAL_KEY:
        if key == "model":
            config["default_model"] = value
        else:
            config["default_reasoning_effort"] = value
        return
    bucket = config.setdefault(
        "model_overrides" if key == "model" else "reasoning_effort_overrides", {}
    )
    if value:
        bucket[name] = value
    else:
        bucket.pop(name, None)


class SubagentModelsDialog(DialogBase):
    """List global defaults and all configured subagent roles."""

    BINDINGS = [
        *DialogBase.BINDINGS,
        Binding("r", "reset_selected", "Reset", show=False),
        Binding("s", "save_config", "Save", show=False),
    ]
    _title_keys = "\u2191\u2193 enter edit \u00b7 r reset \u00b7 s save \u00b7 esc"
    _title_icon = "\u25c6"

    def __init__(self, settings: Any, *, registry: Any | None = None) -> None:
        super().__init__(width=78)
        self._settings = settings
        self._registry = registry
        from synapse.runtime.subagent_config_persist import load_subagent_config

        self._config = load_subagent_config(settings)
        self._names = self._role_names()

    @property
    def title_text(self) -> str:
        return "Subagent Models"

    def compose_body(self) -> ComposeResult:
        return
        yield  # pragma: no cover

    def _role_names(self) -> list[str]:
        builtin = {"planner", "researcher", "reviewer", "tester"}
        disabled = set(
            getattr(self._settings, "disable_builtin_subagents", None) or []
        )
        names = builtin - disabled
        try:
            from synapse.runtime.subagent_specs import SubagentRegistry

            registry = SubagentRegistry.load(
                getattr(self._settings, "workspace", None),
                extra_dirs=getattr(self._settings, "custom_agents_dirs", []) or [],
            )
            names.update(d.name for d in registry.items() if d.enabled)
        except Exception:  # noqa: BLE001 - diagnostics UI must degrade gracefully
            pass
        return sorted(names, key=lambda value: (value not in builtin, value))

    def _model_label(self, value: str | None) -> str:
        if not value:
            return "inherit"
        try:
            profile = self._registry.get(value) if self._registry is not None else None
            return str(getattr(profile, "name", None) or value)
        except Exception:  # noqa: BLE001
            return value

    def _summary(self, name: str) -> str:
        model = _config_value(self._config, "model", name)
        effort = _config_value(self._config, "reasoning", name)
        if not model and not effort:
            return "inherit both"
        return f"{self._model_label(model)} \u00b7 {effort or 'inherit'}"

    def _refresh(self) -> None:
        body = self.query_one("#dialog-body", DialogBody)
        selected = body.selected_key
        items = [
            OptionItem(
                key=_GLOBAL_KEY,
                label="(Global defaults)",
                meta=self._summary(_GLOBAL_KEY),
            )
        ]
        for name in self._names:
            items.append(OptionItem(key=name, label=name, meta=self._summary(name)))
        body.set_options(items, mark="  ")
        if selected in body._option_keys:
            body._selected_idx = body._option_keys.index(selected)
            body._sync_hover()

    def on_mount(self) -> None:
        super().on_mount()
        self._refresh()

    def _on_selected(self, key: str | None) -> None:
        if key:
            self.dismiss(("edit", key))
        else:
            self.dismiss(None)

    def action_reset_selected(self) -> None:
        body = self.query_one("#dialog-body", DialogBody)
        key = body.selected_key
        if not key:
            return
        if key == _GLOBAL_KEY:
            self._config["default_model"] = None
            self._config["default_reasoning_effort"] = None
        else:
            (self._config.get("model_overrides") or {}).pop(key, None)
            (self._config.get("reasoning_effort_overrides") or {}).pop(key, None)
        self._refresh()

    def action_save_config(self) -> None:
        self.dismiss(("save", self._config))


class SubagentEditDialog(DialogBase):
    """Choose one model and one reasoning level for a subagent role."""

    _title_keys = "\u2191\u2193 enter \u00b7 esc"
    _title_icon = "\u25c6"

    def __init__(self, settings: Any, name: str, config: dict[str, Any], registry: Any) -> None:
        super().__init__(width=78)
        self._settings = settings
        self._name = name
        self._config = config
        self._registry = registry
        # Start from the *explicit* value so editing a role that only inherits
        # a global default never writes the fallback back as an override.
        # ``None`` renders as "inherit" (keep the low-priority chain). The
        # global row reads its own default fields instead of the per-name
        # overrides so existing defaults stay visible when edited.
        if name == _GLOBAL_KEY:
            self._selected_model = _inherit_to_none(config.get("default_model"))
            self._selected_reasoning = _inherit_to_none(
                config.get("default_reasoning_effort")
            )
        else:
            self._selected_model = _inherit_to_none(_override_model(config, name))
            self._selected_reasoning = _inherit_to_none(
                _override_reasoning(config, name)
            )
        self._model_touched = False

    @property
    def title_text(self) -> str:
        display = "(Global defaults)" if self._name == _GLOBAL_KEY else self._name
        return f"Edit {display}"

    def compose_body(self) -> ComposeResult:
        return
        yield  # pragma: no cover

    def _model_items(self) -> list[OptionItem]:
        items: list[OptionItem] = [
            OptionItem(
                key=_INHERIT_MODEL_KEY,
                label="(inherit: use definition/global default)",
                selected=self._selected_model is None,
                label_style="dim",
            )
        ]
        names: list[str] = []
        if self._registry is not None:
            try:
                names = list(self._registry.list_names())
            except Exception:  # noqa: BLE001
                names = []
        for alias in names:
            detail = ""
            try:
                profile = self._registry.get(alias)
                detail = str(getattr(profile, "model", None) or "")
            except Exception:  # noqa: BLE001
                detail = ""
            items.append(
                OptionItem(
                    key=f"model:{alias}",
                    label=alias,
                    meta=detail,
                    selected=alias == self._selected_model,
                )
            )
        # The existing value may be an ad-hoc "provider:model" id that is not a
        # registry alias (e.g. a hand-edited settings.json). Show it as its own
        # option so the current configuration stays visible and selectable.
        if (
            self._selected_model is not None
            and self._selected_model != "inherit"
            and self._selected_model not in names
        ):
            items.append(
                OptionItem(
                    key=f"model:{self._selected_model}",
                    label=self._selected_model,
                    meta="(current)",
                    selected=True,
                )
            )
        return items

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body", DialogBody)
        body.set_options(self._model_items(), mark="  ")
        body.append_section("Reasoning")
        reasoning_items = [
            OptionItem(
                key=_INHERIT_REASONING_KEY,
                label="inherit",
                selected=self._selected_reasoning is None,
                label_style="dim",
            )
        ]
        reasoning_items.extend(
            OptionItem(
                key=f"reasoning:{level}",
                label=level,
                selected=level == self._selected_reasoning,
            )
            for level in THINKING_LEVELS
        )
        body.append_options(reasoning_items, mark="  ")

    def _on_selected(self, key: str | None) -> None:
        """Enter on a model records it; Enter on reasoning saves and closes."""
        if not key:
            self.dismiss(None)
            return
        kind, value = key.split(":", 1)
        if kind == "model":
            self._selected_model = None if value == "inherit" else value
            self._model_touched = True
            return
        self._selected_reasoning = None if value == "inherit" else value
        if self._model_touched:
            _set_config_value(
                self._config,
                "model",
                self._name,
                "inherit" if self._selected_model is None else self._selected_model,
            )
        _set_config_value(
            self._config,
            "reasoning",
            self._name,
            "inherit" if self._selected_reasoning is None else self._selected_reasoning,
        )
        self.dismiss(("edited", self._config))
