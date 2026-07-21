"""MCP server toggle panel — invoked by /mcp."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding

from synapse.ui.dialogs.base import DialogBase, OptionItem, SectionHeader


class McpPanelDialog(DialogBase):
    """List MCP servers, toggle enable/disable, reload."""

    BINDINGS = [
        *DialogBase.BINDINGS,
        Binding("r", "reload", "Reload", show=False, priority=True),
    ]
    _title_icon = "⬡"
    _title_keys = "↑↓ enter toggle · r reload · esc"

    def __init__(self, settings: Any, *, project_root: Any = None) -> None:
        super().__init__()
        self._settings = settings
        self._project_root = project_root
        try:
            from synapse.mcp_client import load_mcp_server_configs

            servers = load_mcp_server_configs(
                path=getattr(settings, "mcp_config_path", None),
                json_blob=getattr(settings, "mcp_servers_json", None),
            )
        except Exception:  # noqa: BLE001
            servers = []
        self._servers = list(servers)

    @property
    def title_text(self) -> str:
        return "MCP Servers"

    def compose_body(self) -> ComposeResult:
        yield SectionHeader("Server")
        items: list[OptionItem] = []
        if not self._servers:
            items.append(OptionItem(key="none", label="(no servers configured)"))
        else:
            for s in self._servers:
                status = "enabled" if s.enabled else "disabled"
                items.append(
                    OptionItem(
                        key=s.name,
                        label=s.name,
                        meta=f"{s.transport} · {status}",
                    )
                )
        self._items = items

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        body.set_options(self._items, mark="  ")

    def action_reload(self) -> None:
        self.dismiss(("mcp-reload",))

    def _on_selected(self, key: str | None) -> None:
        if key and key != "none":
            self.dismiss(("mcp-toggle", key))
        else:
            self.dismiss(None)